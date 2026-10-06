"""ASGI response completion vs disconnect ordering, with an explicit native stub."""
import json
from pathlib import Path

import pytest

from gateway.config import load_policy
from gateway.context import current_request
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["complete", "truncated", "refused"])
@pytest.mark.parametrize("disconnect_after_final", [True, False])
async def test_disconnect_cannot_rewrite_already_sent_terminal(tmp_path, terminal, disconnect_after_final):
    state = MemoryState()
    policy = load_policy(Path(__file__).parents[1] / "config/policy.mock.yaml")
    async def native(scope, receive, send):
        await receive()  # replayed request body
        ctx = current_request.get()
        ctx.terminal = terminal
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"fixture-event", "more_body": True})
        if disconnect_after_final:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        assert (await receive())["type"] == "http.disconnect"
    app = GatewayIngress(native, policy, state, tmp_path, "synthetic-administrator")
    body = json.dumps({"model": "general-free", "messages": [{"role": "user", "content": "fixture"}], "stream": True}).encode()
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}
    sent = []
    async def send(message):
        sent.append(message)
    await app({"type": "http", "method": "POST", "path": "/v1/chat/completions", "query_string": b"",
               "headers": [(b"content-type", b"application/json")]}, receive, send)
    finished = [event for event in state.events if event["event"] == "request_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == (terminal if disconnect_after_final else "cancelled")
    assert app.active_requests == 0
