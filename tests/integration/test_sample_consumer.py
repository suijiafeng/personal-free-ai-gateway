"""The included personal-app client through native Proxy + PostgreSQL + mock.

This is real consumer/gateway protocol integration with synthetic upstreams,
not a real-provider account qualification or a third-party IDE compatibility claim.
"""
import os

import pytest

from examples.client import consume, make_client
from tests.integration.test_proxy_boundaries import native_boundary, wait_idle


pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("GATEWAY_TEST_DATABASE_URL"),
                       reason="PostgreSQL fixture URL not set; not a consumer integration pass"),
]


def test_reference_app_nonstream_through_original_native_adapter(native_boundary, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    with native_boundary() as (proxy, upstream, key, revision):
        with make_client(base_url=str(proxy.client.base_url).rstrip("/") + "/v1",
                         api_key=key, timeout=5) as client:
            result = consume(client, stream=False)
        wait_idle(proxy, upstream)
        assert result.status == "complete" and result.text
        assert result.request_id and result.response_id
        assert upstream.snapshot()["counts"] == {"mock-primary": 1}


def test_reference_app_sdk_normal_stream_error_and_cancel_through_native_gateway(native_boundary, monkeypatch):
    # Match this disposable loopback fixture; never inherit an external proxy
    # route for the synthetic consumer's restricted key.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    with native_boundary(policy_filename="policy.sdk.mock.yaml") as (proxy, upstream, key, revision):
        with make_client(base_url=str(proxy.client.base_url).rstrip("/") + "/v1",
                         api_key=key, timeout=5) as client:
            assert client.max_retries == 0
            ordinary = consume(client, stream=False)
            wait_idle(proxy, upstream)
            assert ordinary.status == "complete"
            assert ordinary.text and ordinary.response_id and ordinary.request_id

            streamed = consume(client, stream=True)
            wait_idle(proxy, upstream)
            assert streamed.status == "complete"
            assert streamed.text and streamed.response_id and streamed.request_id
            assert streamed.usage is None and streamed.usage_source == "unknown"

            before = dict(upstream.snapshot()["counts"])
            unsupported = consume(client, model="unknown-alias", stream=False)
            assert unsupported.status == "error"
            assert unsupported.error_type == "NotFoundError"
            assert upstream.snapshot()["counts"] == before

            upstream.set_behavior("mock-primary", "slow_stream", chunk_delay=.1)
            def cancel_after_text(text):
                raise KeyboardInterrupt
            cancelled = consume(client, stream=True, on_text=cancel_after_text)
            wait_idle(proxy, upstream)
            assert cancelled.status == "cancelled"
            assert cancelled.text  # Keep the partial answer, never replay it.
            assert upstream.snapshot()["counts"].get("mock-secondary", 0) == 0

            for result in (ordinary, streamed):
                traces = proxy.request("GET", "/gateway/traces?request_id=" + result.request_id).json()["data"]
                finished = [event for event in traces if event["event"] == "request_finished"]
                assert len(finished) == 1
                assert finished[0]["status"] == "complete"
                assert finished[0]["config_revision"] == revision
