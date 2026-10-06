"""Real native process/DB/TCP boundary tests, with synthetic upstreams only.

These do not exercise Nginx, Docker, a real provider, or the user's consumer.
The fixture is independent from the in-process Proxy test class: no inherited
test methods or duplicated acceptance counts.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch
import os
from pathlib import Path
import time

import psycopg
import pytest
import yaml

from gateway.config import load_policy
from tests.mock_upstream import MockState, serve_mock
from tests import mock_upstream as mock_protocol
from tests.verify_backup_restore import _source_port
from tests.verify_native_recovery import NativeProcess


ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("GATEWAY_TEST_DATABASE_URL"),
                       reason="PostgreSQL fixture URL not set; not a DB test pass"),
]

HISTORY = [
    {"role": "system", "content": "Synthetic instruction: preserve all history."},
    {"role": "user", "content": "Synthetic first question: alpha."},
    {"role": "assistant", "content": "Synthetic prior answer: beta."},
    {"role": "user", "content": "Synthetic follow-up: gamma."},
]


def wait_idle(proxy, upstream, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = proxy.request("GET", "/gateway/status")
        if (response.status_code == 200
                and response.json().get("active_requests") == 0
                and upstream.snapshot()["active_requests"] == 0):
            return
        time.sleep(.02)
    pytest.fail("Synthetic native/TCP request did not release its active resources")


@pytest.fixture
def native_boundary(tmp_path):
    dsn = os.environ["GATEWAY_TEST_DATABASE_URL"]
    # Match the disposable runner's strict password-free loopback URI. Never
    # target a generic database even when this file is executed by itself.
    _source_port(dsn)

    @contextmanager
    def start(*, total_timeout=4, first_event_timeout=1, stream_idle_timeout=1,
              policy_filename="policy.mock.yaml"):
        # A prior synthetic module can leave cooling observations. Clearing
        # only these fixture tables is not evidence of production recovery.
        with psycopg.connect(dsn) as connection:
            connection.execute("CREATE SCHEMA IF NOT EXISTS gateway_ext")
            for table in ("pools", "observations"):
                exists = connection.execute("SELECT to_regclass(%s)",
                                            ("gateway_ext." + table,)).fetchone()[0]
                if exists:
                    connection.execute("DELETE FROM gateway_ext." + table)
        upstream = MockState(capture_payloads=True)
        with serve_mock(upstream) as mock:
            data = yaml.safe_load((ROOT / "config" / policy_filename).read_text())
            for deployment in data["deployments"]:
                deployment["api_base"] = mock.base_url
            data["limits"] = {"total_timeout": total_timeout,
                              "first_event_timeout": first_event_timeout,
                              "stream_idle_timeout": stream_idle_timeout,
                              "cooldown_seconds": 1}
            path = tmp_path / "policy.yaml"
            path.write_text(yaml.safe_dump(data))
            with NativeProcess(path, dsn, tmp_path / "runtime", tmp_path / "state") as proxy:
                key = proxy.create_key("boundary-fixture")
                try:
                    # Native adapters perform lazy imports on their first actual
                    # request, and streaming has its own lazy imports. Establish
                    # one complete baseline per mode, then reset only
                    # synthetic counters before timing the intended fault. A cold
                    # start can exhaust these deliberately sub-second budgets.
                    for streaming in (False, True):
                        for _ in range(3):
                            warmup = proxy.request("POST", "/v1/chat/completions", key, {
                                "model":"general-free", "messages":[{"role":"user","content":"Synthetic warmup"}],
                                "stream":streaming})
                            wait_idle(proxy, upstream)
                            if warmup.status_code == 200 and (not streaming or "data: [DONE]" in warmup.text):
                                break
                            assert warmup.status_code in (200,504)
                        else:
                            pytest.fail("Native synthetic adapter warmup never completed")
                    upstream.reset()
                    yield proxy, upstream, key, load_policy(path).revision
                finally:
                    proxy.revoke(key)
    return start


def test_native_fallback_preserves_full_ordered_history_and_output_limit(native_boundary):
    with native_boundary() as (proxy, upstream, key, revision):
        upstream.set_behavior("mock-primary", "http_429", retry_after="1")
        response = proxy.request("POST", "/v1/chat/completions", key, {
            "model": "general-free", "messages": HISTORY,
            "max_completion_tokens": 37,
        })
        assert response.status_code == 200
        assert response.headers["x-config-revision"] == revision
        assert response.headers["x-actual-deployment"] == "mock-secondary"
        wait_idle(proxy, upstream)
        snapshot = upstream.snapshot()
        assert snapshot["counts"] == {"mock-primary": 1, "mock-secondary": 1}
        assert len(snapshot["requests"]) == 2
        for request in snapshot["requests"]:
            assert request["messages"] == HISTORY
            assert request["max_completion_tokens"] == 37
        traces = proxy.request("GET", "/gateway/traces?request_id="
                               + response.headers["x-request-id"]).json()["data"]
        attempts = sorted((event for event in traces if event["event"] == "attempt_started"),
                          key=lambda event: event["attempt"])
        assert [event["deployment_id"] for event in attempts] == ["mock-primary", "mock-secondary"]
        assert {event["config_revision"] for event in attempts} == {revision}


def test_native_total_deadline_bounds_two_slow_upstream_attempts(native_boundary):
    with native_boundary(total_timeout=1.2, first_event_timeout=.8) as (proxy, upstream, key, _):
        for model in ("mock-primary", "mock-secondary"):
            upstream.set_behavior(model, "timeout", timeout_seconds=10)
        started = time.monotonic()
        response = proxy.generate(key)
        elapsed = time.monotonic() - started
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "deadline_exceeded"
        # One shared 1.2s request budget, not two 0.8s provider timeouts.
        # The allowance covers HTTP and audit scheduling on a shared runner.
        assert .8 <= elapsed < 2.0
        wait_idle(proxy, upstream)
        snapshot = upstream.snapshot()
        assert snapshot["counts"] == {"mock-primary": 1, "mock-secondary": 1}
        assert snapshot["cancelled"] == {"mock-primary": 1, "mock-secondary": 1}
        upstream.reset()
        assert proxy.generate(key).status_code == 200


def test_native_total_deadline_ends_active_sse_without_success_or_fallback(native_boundary):
    with native_boundary(total_timeout=2, first_event_timeout=1.5,
                         stream_idle_timeout=.6) as (proxy, upstream, key, _):
        # This is an active-stream deadline test, not a sub-second cold-start
        # benchmark. First event already carries real text; every later chunk
        # arrives inside idle timeout and a genuine terminal is >4s away.
        original = deepcopy(mock_protocol.STREAM_EVENTS)
        first = deepcopy(original[0])
        first["choices"][0]["delta"]["content"] = "Hello"
        ongoing = deepcopy(original[2])
        events = [first] + [deepcopy(ongoing) for _ in range(20)] + original[-2:]
        upstream.set_behavior("mock-primary", "slow_stream", chunk_delay=.2)
        started = time.monotonic()
        with patch.object(mock_protocol, "STREAM_EVENTS", events):
            response = proxy.request("POST", "/v1/chat/completions", key, {
                "model": "general-free", "messages": HISTORY, "stream": True,
            })
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        # Exact configured 2s budget + the same fixed 450ms transport/cleanup
        # allowance as before; production 90s and retry limits are unchanged.
        assert 1.8 <= elapsed < 2.45
        assert "Hello" in response.text
        assert "data: [DONE]" not in response.text
        assert '"finish_reason":"stop"' not in response.text.replace(" ", "")
        wait_idle(proxy, upstream)
        snapshot = upstream.snapshot()
        assert snapshot["counts"] == {"mock-primary": 1}
        assert snapshot["cancelled"] == {"mock-primary": 1}
        traces = proxy.request("GET", "/gateway/traces?request_id="
                               + response.headers["x-request-id"]).json()["data"]
        finished = [event for event in traces if event["event"] == "request_finished"]
        assert len(finished) == 1
        assert finished[0]["status"] == "stream_interrupted"
        assert 0 <= finished[0]["first_event_ms"] < 2000
        assert finished[0]["error_code"] == "deadline_exceeded"
        assert not any(event.get("status") == "complete" for event in traces)
        upstream.reset()
        assert proxy.generate(key).status_code == 200


def test_known_native_gap_refusal_after_text_is_lost_on_wire_and_in_diagnostics(native_boundary):
    """Reproduce a release blocker, NOT an acceptance pass for stream refusal.

    The locked native parser drops a separate refusal-only delta after text.
    Reviewed hooks cannot recover that lost source signal. Mock streaming stays
    available for fault experiments; bundled production streaming stays disabled.
    If a supported upstream upgrade fixes this gap, this assertion must change
    together with its certification and documented public capability.
    """
    production = load_policy(ROOT / "config/policy.yaml")
    assert all(not deployment.capability.streaming for deployment in production.deployments)
    with native_boundary() as (proxy, upstream, key, _):
        upstream.set_behavior("mock-primary", "refusal_after_content")
        response = proxy.request("POST", "/v1/chat/completions", key, {
            "model": "general-free", "messages": HISTORY, "stream": True,
        })
        assert response.status_code == 200
        assert "Hello" in response.text
        assert "Synthetic policy refusal." not in response.text
        assert "data: [DONE]" in response.text
        assert '"finish_reason":"stop"' in response.text.replace(" ", "")
        wait_idle(proxy, upstream)
        assert upstream.snapshot()["counts"] == {"mock-primary": 1}
        traces = proxy.request("GET", "/gateway/traces?request_id="
                               + response.headers["x-request-id"]).json()["data"]
        finished = [event for event in traces if event["event"] == "request_finished"]
        assert len(finished) == 1
        assert finished[0]["status"] == "complete"
        assert not any(event.get("status") == "refused" for event in traces)
        assert "Synthetic policy refusal." not in str(traces)


def test_completed_stream_wire_and_final_audit_agree(native_boundary):
    """A normal ASGI response-close signal must not reclassify completed SSE."""
    with native_boundary() as (proxy, upstream, key, _):
        for behavior, reason, terminal in (("ok", "stop", "complete"),
                                          ("length", "length", "truncated"),
                                          ("refusal_with_content", "stop", "refused")):
            upstream.set_behavior("mock-primary", behavior)
            response = proxy.request("POST", "/v1/chat/completions", key, {
                "model": "general-free", "messages": HISTORY, "stream": True,
            })
            assert response.status_code == 200, behavior
            assert "data: [DONE]" in response.text, behavior
            assert '"finish_reason":"' + reason + '"' in response.text.replace(" ", ""), behavior
            wait_idle(proxy, upstream)
            assert upstream.snapshot()["counts"] == {"mock-primary": 1}, behavior
            traces = proxy.request("GET", "/gateway/traces?request_id="
                                   + response.headers["x-request-id"]).json()["data"]
            for kind in ("attempt_finished", "request_finished"):
                finished = [event for event in traces if event["event"] == kind]
                assert len(finished) == 1, behavior
                assert finished[0]["status"] == terminal, (behavior, kind)
            assert "Synthetic policy refusal." not in str(traces)
            upstream.reset()
