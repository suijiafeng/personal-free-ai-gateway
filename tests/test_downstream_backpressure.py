"""Bounded cleanup under real ASGI send backpressure, without provider calls."""
import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import load_policy
from gateway.context import RequestContext, current_request
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState


def policy():
    value = load_policy(Path(__file__).parents[1] / "config/policy.mock.yaml")
    value.limits.total_timeout = .03
    return value


SCOPE = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
         "query_string": b"", "headers": [(b"content-type", b"application/json")]}
BODY = json.dumps({"model": "general-free", "messages": [
    {"role": "user", "content": "synthetic slow-reader fixture"}], "stream": True}).encode()


async def receive():
    return {"type": "http.request", "body": BODY, "more_body": False}


@pytest.mark.asyncio
async def test_blocked_stream_send_releases_slot_after_total_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.ingress.FINAL_SEND_TIMEOUT_SECONDS", .03)
    state = MemoryState()
    sends = []

    async def native(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"synthetic chunk", "more_body": True})

    async def blocked_send(message):
        sends.append(message)
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    app = GatewayIngress(native, policy(), state, tmp_path, "synthetic-admin")
    started = time.monotonic()
    await asyncio.wait_for(app(SCOPE, receive, blocked_send), .3)
    assert time.monotonic() - started < .25
    assert app.active_requests == 0
    assert state.healthy
    assert len(sends) == 3  # start, interrupted body, bounded empty EOF
    assert not any(b"[DONE]" in event.get("body", b"") for event in sends)
    assert state.events[-1]["status"] == "stream_interrupted"


@pytest.mark.asyncio
async def test_pre_response_error_cannot_block_a_slot_on_unread_error(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.ingress.FINAL_SEND_TIMEOUT_SECONDS", .03)
    state = MemoryState()

    async def native(scope, receive, send):
        await receive()
        raise RuntimeError("synthetic failure")

    async def blocked_send(message):
        await asyncio.Event().wait()

    app = GatewayIngress(native, policy(), state, tmp_path, "synthetic-admin")
    await asyncio.wait_for(app(SCOPE, receive, blocked_send), .3)
    assert app.active_requests == 0
    assert state.events[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_hung_audit_is_fail_closed_but_releases_local_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.ingress.AUDIT_CLEANUP_TIMEOUT_SECONDS", .03)
    state = MemoryState()

    async def hang_record(event):
        await asyncio.Event().wait()
    state.record = hang_record

    async def native(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        pass

    app = GatewayIngress(native, policy(), state, tmp_path, "synthetic-admin")
    await asyncio.wait_for(app(SCOPE, receive, send), .3)
    assert app.active_requests == 0
    assert not state.healthy
    assert state.events == []  # No fabricated audit success.


@pytest.mark.asyncio
async def test_hung_upstream_close_does_not_hold_completed_stream(monkeypatch):
    from gateway.hooks import GatewayGuard
    monkeypatch.setattr("gateway.hooks.STREAM_CLOSE_TIMEOUT_SECONDS", .03)
    state = MemoryState()
    guard = GatewayGuard().configure(policy(), state)
    ctx = RequestContext(genuine_terminal=True)

    class Stream:
        def __aiter__(self):
            return self.items()

        async def items(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="synthetic", refusal=None), finish_reason="stop")], usage=None)

        async def aclose(self):
            await asyncio.Event().wait()

    token = current_request.set(ctx)
    try:
        async def consume():
            return [part async for part in guard.async_post_call_streaming_iterator_hook(None, Stream(), {})]
        result = await asyncio.wait_for(consume(), .3)
    finally:
        current_request.reset(token)
    assert len(result) == 1
    assert ctx.terminal == "complete"
    assert state.healthy


def test_unread_tcp_response_releases_slot_before_peer_disconnect(tmp_path):
    """Real Uvicorn/socket flow control; native/provider/Nginx are not involved."""
    import uvicorn

    p = policy()
    p.limits.total_timeout = .15
    state = MemoryState()
    generation_started = threading.Event()
    native_exited = threading.Event()
    attempted = []

    async def native(scope, receive, send):
        await receive()
        generation_started.set()
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            # Much larger than the tiny socket buffers, to force actual write
            # backpressure. This is synthetic data, not a generated model reply.
            while True:
                attempted.append(1)
                await send({"type": "http.response.body", "body": b"x" * 262144, "more_body": True})
        finally:
            native_exited.set()

    app = GatewayIngress(native, p, state, tmp_path, "synthetic-admin")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            with socket.socket() as peer:
                peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                peer.connect(listener.getsockname())
                peer.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Type: application/json\r\nContent-Length: "
                             + str(len(BODY)).encode() + b"\r\n\r\n" + BODY)
                assert generation_started.wait(2)
                # Intentionally do not call recv() or disconnect. The server
                # must cancel blocked generation and free its slot on its own.
                deadline = time.monotonic() + 1.5
                while app.active_requests and time.monotonic() < deadline:
                    time.sleep(.01)
                assert app.active_requests == 0
                assert native_exited.is_set()
                assert state.events[-1]["status"] == "stream_interrupted"
                assert len(attempted) < 100  # Genuine blocking, not an unbounded emitter.
        finally:
            server.should_exit = True
            thread.join(3)
            assert not thread.is_alive()
