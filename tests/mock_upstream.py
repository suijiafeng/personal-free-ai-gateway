"""Deterministic, loopback-only OpenAI-compatible upstream for synthetic tests.

This is a fixture emitter, not a production provider adapter or SSE parser.
No real credentials are needed or inspected. Request bodies are retained only
when a test explicitly enables ``MockState(capture_payloads=True)``. Headers,
including Authorization, are never retained. Do not send real user data here.

Use ``serve_mock(state)`` to obtain a real ephemeral HTTP server. ASGITransport
buffers responses and cannot adequately test TCP interruption or cancellation.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any, Iterator, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

FIXTURES = Path(__file__).with_name("fixtures")
COMPLETION = json.loads((FIXTURES / "completion.json").read_text())
STREAM_EVENTS = json.loads((FIXTURES / "stream_events.json").read_text())
MODELS = ("mock-primary", "mock-secondary")
BEHAVIORS = frozenset({
    "ok", "http_429", "http_401", "http_403", "http_500", "http_503",
    "timeout", "disconnect_before_role", "disconnect_after_role",
    "disconnect_after_content", "refusal", "length", "missing_usage",
    "slow_stream", "eof_without_finish", "eof_after_role", "eof_after_content",
    "refusal_after_content", "refusal_with_content", "content_filter",
    "unexpected_finish", "zero_usage", "partial_usage", "null_usage",
})


@dataclass(frozen=True)
class MockBehavior:
    kind: str = "ok"
    retry_after: str | None = "1"
    first_event_delay: float = 0.0
    chunk_delay: float = 0.01
    timeout_seconds: float = 60.0
    headers: dict[str, str] = field(default_factory=dict)
    error_message: str = "Synthetic upstream fault"
    release_event: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.kind not in BEHAVIORS:
            raise ValueError(f"Unknown mock behavior: {self.kind}")
        if min(self.first_event_delay, self.chunk_delay, self.timeout_seconds) < 0:
            raise ValueError("Mock delays cannot be negative")


def _behavior(value: str | MockBehavior | dict[str, Any]) -> MockBehavior:
    if isinstance(value, MockBehavior):
        return value
    if isinstance(value, str):
        return MockBehavior(kind=value)
    return MockBehavior(**value)


class MockState:
    """Thread-safe controls and counters for one synthetic test run.

    ``set_behavior`` sets the steady-state behavior and clears its sequence.
    ``set_sequence`` consumes one behavior per HTTP generation request, then
    returns to the steady-state behavior (``ok`` unless changed explicitly).
    HTTP status faults count as attempts; listing models never does.
    """

    def __init__(self, *, capture_payloads: bool = False,
                 models: Sequence[str] = MODELS) -> None:
        self.capture_payloads = capture_payloads
        self.models = tuple(models)
        self.counts: Counter[str] = Counter()
        self.cancelled: Counter[str] = Counter()
        self.completed: Counter[str] = Counter()
        self.faulted: Counter[str] = Counter()
        self.emitted_events: Counter[str] = Counter()
        self.requests: list[dict[str, Any]] = []
        self.active_requests = 0
        self._defaults: dict[str, MockBehavior] = {}
        self._sequences: dict[str, deque[MockBehavior]] = {}
        self._lock = threading.Lock()

    def set_behavior(self, model: str, behavior: str | MockBehavior = "ok",
                     **options: Any) -> None:
        value = MockBehavior(kind=behavior, **options) if isinstance(behavior, str) else behavior
        if options and not isinstance(behavior, str):
            raise ValueError("Pass either a MockBehavior or keyword options")
        with self._lock:
            self._defaults[model] = value
            self._sequences.pop(model, None)

    def set_sequence(self, model: str,
                     behaviors: Sequence[str | MockBehavior | dict[str, Any]]) -> None:
        values = deque(_behavior(item) for item in behaviors)
        with self._lock:
            self._sequences[model] = values

    def reset(self) -> None:
        """Reset an idle harness, rejecting reset during active requests."""
        with self._lock:
            if self.active_requests:
                raise RuntimeError("Cannot reset while mock requests are active")
            self.counts.clear()
            self.cancelled.clear()
            self.completed.clear()
            self.faulted.clear()
            self.emitted_events.clear()
            self.requests.clear()
            self._defaults.clear()
            self._sequences.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = {name: dict(getattr(self, name)) for name in (
                "counts", "cancelled", "completed", "faulted", "emitted_events")}
            result["active_requests"] = self.active_requests
            if self.capture_payloads:
                result["requests"] = deepcopy(self.requests)
            return result

    def _begin(self, model: str, payload: dict[str, Any]) -> tuple[MockBehavior, str]:
        with self._lock:
            self.counts[model] += 1
            self.active_requests += 1
            request_id = f"chatcmpl-{model}-{self.counts[model]:04d}"
            if self.capture_payloads:
                self.requests.append(deepcopy(payload))
            queue = self._sequences.get(model)
            behavior = queue.popleft() if queue else self._defaults.get(model, MockBehavior())
            return behavior, request_id

    def _finish(self, model: str, outcome: str) -> None:
        with self._lock:
            getattr(self, outcome)[model] += 1
            self.active_requests -= 1

    def _emitted(self, model: str) -> None:
        with self._lock:
            self.emitted_events[model] += 1


class InjectedDisconnect(RuntimeError):
    """Intentional body failure makes Uvicorn close an incomplete HTTP stream."""


async def _pause(request: Request, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await request.is_disconnected():
            raise asyncio.CancelledError()
        await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _error(status: int, code: str, *, headers: dict[str, str] | None = None, message: str = "Synthetic upstream fault") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code,
                           "param": None, "code": code}},
        headers=headers,
    )


def create_mock_app(state: MockState | None = None) -> FastAPI:
    state = state if state is not None else MockState()
    app = FastAPI(title="Synthetic OpenAI upstream", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.mock = state

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "scope": "synthetic-only"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [
            {"id": model, "object": "model", "created": 1700000000, "owned_by": "mock"}
            for model in state.models
        ]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _error(400, "invalid_request_error")
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return _error(400, "invalid_request_error")
        model = payload["model"]
        behavior, request_id = state._begin(model, payload)
        headers = {"x-request-id": request_id, **behavior.headers}
        if model not in state.models:
            state._finish(model, "faulted")
            return _error(404, "model_not_found", headers=headers)
        if behavior.kind.startswith("http_"):
            status = int(behavior.kind.removeprefix("http_"))
            code = {401: "invalid_api_key", 403: "permission_denied",
                    429: "rate_limit_exceeded"}.get(status, "server_error")
            if status == 429 and behavior.retry_after is not None:
                headers["retry-after"] = behavior.retry_after
            state._finish(model, "faulted")
            return _error(status, code, headers=headers, message=behavior.error_message)

        if not payload.get("stream", False):
            outcome = "completed"
            try:
                delay = behavior.timeout_seconds if behavior.kind == "timeout" else behavior.first_event_delay
                await _pause(request, delay)
                while behavior.release_event is not None and not behavior.release_event.is_set():
                    await _pause(request,0.01)
                response = deepcopy(COMPLETION)
                response.update(id=request_id, model=model)
                if behavior.kind == "missing_usage":
                    response.pop("usage", None)
                if behavior.kind == "length":
                    response["choices"][0]["finish_reason"] = "length"
                if behavior.kind == "refusal":
                    response["choices"][0].update(
                        message={"role": "assistant", "content": None,
                                 "refusal": "Synthetic policy refusal."},
                        finish_reason="stop",
                    )
                return JSONResponse(response, headers=headers)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                state._finish(model, outcome)

        async def stream():
            outcome = "faulted"
            try:
                delay = behavior.timeout_seconds if behavior.kind == "timeout" else behavior.first_event_delay
                await _pause(request, delay)
                if behavior.kind == "disconnect_before_role":
                    raise InjectedDisconnect("Injected fault before first SSE event")
                events = deepcopy(STREAM_EVENTS)
                if behavior.kind == "refusal":
                    events = [events[0], {
                        "choices": [{"index": 0, "delta": {"refusal": "Synthetic policy refusal."},
                                     "finish_reason": None}]
                    }, events[-2], events[-1]]
                if behavior.kind == "refusal_after_content":
                    events.insert(-2, {"choices": [{"index": 0,
                        "delta": {"refusal": "Synthetic policy refusal."}, "finish_reason": None}]})
                if behavior.kind == "refusal_with_content":
                    events[2]["choices"][0]["delta"]["refusal"] = "Synthetic policy refusal."
                if behavior.kind in {"length", "content_filter", "unexpected_finish"}:
                    events[-2]["choices"][0]["finish_reason"] = (
                        "tool_calls" if behavior.kind == "unexpected_finish" else behavior.kind)
                if behavior.kind == "zero_usage":
                    events[-1]["usage"] = dict.fromkeys(COMPLETION["usage"], 0)
                if behavior.kind == "partial_usage":
                    events[-1]["usage"] = {"prompt_tokens": 8}
                if behavior.kind == "null_usage":
                    events[-1]["usage"] = None
                include_usage = payload.get("stream_options", {}).get("include_usage", False)
                if behavior.kind == "missing_usage" or not include_usage:
                    events = [event for event in events if "usage" not in event]
                for index, event in enumerate(events):
                    if index:
                        chunk_delay = max(behavior.chunk_delay, 0.2) if behavior.kind == "slow_stream" else behavior.chunk_delay
                        await _pause(request, chunk_delay)
                    event.update(id=request_id, model=model, created=1700000000,
                                 object="chat.completion.chunk")
                    state._emitted(model)
                    yield "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                    if behavior.kind == "disconnect_after_role" and index == 0:
                        raise InjectedDisconnect("Injected fault after role SSE event")
                    if behavior.kind == "disconnect_after_content" and index == 2:
                        raise InjectedDisconnect("Injected fault after first content SSE event")
                    if (behavior.kind == "eof_after_role" and index == 0) or (
                        behavior.kind in {"eof_without_finish", "eof_after_content"} and index == 2
                    ):
                        return  # Clean HTTP EOF is still an incomplete answer.
                outcome = "completed"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                state._finish(model, outcome)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    return app


@dataclass(frozen=True)
class RunningMock:
    base_url: str
    state: MockState
    app: FastAPI

    @property
    def url(self) -> str:
        return self.base_url.removesuffix("/v1")


@contextmanager
def serve_mock(state: MockState | None = None, *, port: int = 0) -> Iterator[RunningMock]:
    """Run a real HTTP fixture server on 127.0.0.1, with deterministic teardown."""
    state = state if state is not None else MockState()
    app = create_mock_app(state)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    actual_port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", access_log=False,
                            lifespan="off", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Mock HTTP server failed to start")
            time.sleep(0.005)
        yield RunningMock(f"http://127.0.0.1:{actual_port}/v1", state, app)
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Mock HTTP server did not shut down")


app = create_mock_app()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--behavior", choices=sorted(BEHAVIORS), default="ok")
    args = parser.parse_args()
    state = MockState()
    for model in MODELS:
        state.set_behavior(model, args.behavior)
    uvicorn.run(create_mock_app(state), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
