"""Metadata provenance, including pinned native LiteLLM over real loopback TCP."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from gateway.metadata import (
    ALLOWED_HEADERS,
    extract_response_headers,
    extract_response_metadata,
    filter_response_headers,
)
from tests.mock_upstream import MockState, serve_mock


OBSERVED = {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14}
HEADERS = {
    "x-ratelimit-limit-requests": "30",
    "x-ratelimit-remaining-requests": "2",
    "x-ratelimit-reset-requests": "6s",
    "x-ratelimit-limit-tokens": "1000",
    "x-ratelimit-remaining-tokens": "800",
    "x-ratelimit-reset-tokens": "1m2s",
    "retry-after": "7",
}


@pytest.mark.parametrize("representation", [dict, json.dumps, lambda v: json.dumps(v).encode()])
def test_only_explicit_complete_numeric_usage_is_observed(representation):
    body = {"usage": {**OBSERVED, "private": "DO_NOT_RETAIN"}, "choices": [{"text": "BODY_SENTINEL"}]}
    metadata = extract_response_metadata(representation(body), {**HEADERS, "authorization": "SECRET_SENTINEL"})
    assert metadata.usage == OBSERVED
    assert metadata.usage_source == "upstream_observed"
    assert metadata.headers == HEADERS
    assert "SENTINEL" not in repr(metadata)
    assert "DO_NOT_RETAIN" not in repr(metadata)
    body["usage"]["prompt_tokens"] = 99
    assert metadata.usage["prompt_tokens"] == 8


@pytest.mark.parametrize("body", [
    {}, {"usage": None}, {"usage": {}}, {"usage": {"prompt_tokens": 1}},
    {"usage": {**OBSERVED, "prompt_tokens": -1}},
    {"usage": {**OBSERVED, "prompt_tokens": True}},
    {"usage": {**OBSERVED, "completion_tokens": "6"}},
    {"usage": {**OBSERVED, "total_tokens": 14.0}},
    {"usage": {**OBSERVED, "total_tokens": None}},
    {"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 6, "totalTokenCount": 14}},
    {"estimated_usage": OBSERVED},
    "not-json", "data: {\"usage\": {}}\n\n", "[1, 2]", b"\xff",
    '{"usage":{"prompt_tokens":8,"completion_tokens":6,"total_tokens":14},"usage":null}',
    SimpleNamespace(usage=OBSERVED),
])
def test_absent_invalid_unsupported_and_normalized_values_stay_unknown(body):
    metadata = extract_response_metadata(body)
    assert metadata.usage is None
    assert metadata.usage_source == "unknown"


def test_zero_must_be_present_not_fabricated():
    explicit_zero = dict.fromkeys(OBSERVED, 0)
    assert extract_response_metadata({"usage": explicit_zero}).usage == explicit_zero
    assert extract_response_metadata({"usage": {}}).usage is None


def test_stream_usage_is_unknown_even_if_typed_or_complete():
    result = extract_response_metadata({"usage": OBSERVED}, HEADERS, streaming=True)
    assert result.usage is None
    assert result.usage_source == "unknown"
    assert result.headers == HEADERS


def test_header_allowlist_case_bounds_ambiguity_and_no_credentials():
    supplied = {name.upper(): value for name, value in HEADERS.items()}
    supplied.update({"Authorization": "SECRET", "set-cookie": "SECRET", "x-request-id": "private-id", "x-litellm-cost": "0"})
    assert filter_response_headers(supplied) == HEADERS
    assert filter_response_headers(None) == {}
    assert filter_response_headers({"retry-after": "9\r\nprivate-header: SECRET"}) == {}
    assert filter_response_headers({"retry-after": "x" * 257}) == {}
    assert filter_response_headers({"retry-after": 12}) == {}
    assert filter_response_headers({"retry-after": "4", "Retry-After": "9"}) == {}


def test_native_header_sources_are_narrow_and_raw_has_precedence():
    wrapper = SimpleNamespace(_hidden_params={"additional_headers": {
        **{"llm_provider-" + key: value for key, value in HEADERS.items()},
        "retry-after": "999", "llm_provider-authorization": "SECRET",
    }})
    assert extract_response_headers(wrapper) == HEADERS
    wrapper._hidden_params["headers"] = {"retry-after": "2", "authorization": "SECRET"}
    assert extract_response_headers(wrapper) == {"retry-after": "2"}
    wrapper._hidden_params["headers"] = {}
    assert extract_response_headers(wrapper) == {}
    assert extract_response_headers(object()) == {}


@pytest.fixture
def native_probe(monkeypatch):
    # Pinned native SDK, no install, remote cost-map lookup, real provider, or credential.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_TELEMETRY", "False")
    monkeypatch.setenv("LITELLM_LOG", "CRITICAL")
    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    assert importlib.metadata.version("litellm") == "1.104.0"
    marker = ContextVar("provenance_test_request", default=None)

    class Probe(CustomLogger):
        def __init__(self):
            super().__init__()
            self.posts = []
            self.successes = []
            self.streams = []
            self.stream_signals = []

        def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
            metadata = extract_response_metadata(
                kwargs.get("original_response"), kwargs.get("response_headers"),
                streaming=bool(kwargs.get("stream")),
            )
            self.posts.append((marker.get(), metadata))

        async def async_post_call_success_deployment_hook(self, request_data, response, call_type):
            self.successes.append((marker.get(), extract_response_headers(response)))
            return response

        async def async_post_call_streaming_deployment_hook(self, request_data, response_chunk, call_type):
            # The supported typed deployment hook has transport headers, but no
            # raw-usage presence indicator. Never inspect private chunk buffers.
            self.stream_signals.append(tuple(
                (choice.finish_reason,
                 bool(getattr(choice.delta, "refusal", None) or
                      (getattr(choice.delta, "provider_specific_fields", None) or {}).get("refusal")))
                for choice in getattr(response_chunk, "choices", []) or []
            ))
            self.streams.append((marker.get(),
                filter_response_headers(request_data.get("response_headers")) or extract_response_headers(response_chunk),
                getattr(response_chunk, "usage", None) is not None,
            ))
            return response_chunk

    probe = Probe()
    # Isolate officially supported callback registrations from proxy-suite hooks.
    for name in ("callbacks", "input_callback", "success_callback", "failure_callback",
                 "_async_input_callback", "_async_success_callback", "_async_failure_callback"):
        monkeypatch.setattr(litellm, name, [probe] if name == "callbacks" else [])
    monkeypatch.setattr(litellm, "turn_off_message_logging", True)
    return litellm, probe, marker


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["ok", "missing_usage"])
@pytest.mark.parametrize("provider", ["openai", "groq"])
async def test_native_nonstream_raw_callback_precedes_normalization(native_probe, behavior, provider, monkeypatch):
    litellm, probe, marker = native_probe
    if provider == "groq":
        # The native Groq transformation requires the provider's service_tier
        # response member. This changes the synthetic envelope, not the adapter.
        from tests.mock_upstream import COMPLETION
        monkeypatch.setitem(COMPLETION, "service_tier", None)
    state = MockState()
    state.set_behavior("mock-primary", behavior, headers={**HEADERS, "x-secret": "SECRET_SENTINEL"})
    token = marker.set("same-request")
    try:
        with serve_mock(state) as server:
            response = await litellm.acompletion(
                model=provider + "/mock-primary", api_base=server.base_url, api_key="synthetic-only",
                messages=[{"role": "user", "content": "BODY_SENTINEL"}], max_retries=0,
            )
            await asyncio.sleep(0)  # Allow native bounded logging tasks to complete.
        assert len(probe.posts) == 1
        context, metadata = probe.posts[0]
        assert context == "same-request"
        if behavior == "ok":
            assert metadata.usage == OBSERVED
            assert metadata.usage_source == "upstream_observed"
        else:
            assert metadata.usage is None
            assert metadata.usage_source == "unknown"
            # This demonstrates why normalized usage is never provenance.
            if provider == "openai":
                assert response.usage.model_dump(include=set(OBSERVED)) == dict.fromkeys(OBSERVED, 0)
            else:
                assert getattr(response, "usage", None) is None
        # Async native OpenAI records headers after log_post_api_call. They are
        # available synchronously to the awaited success deployment callback.
        assert metadata.headers == {}
        # Groq's native OpenAI-like adapter drops nonstream headers entirely in
        # this pin. Unknown is correct; do not claim OpenAI coverage proves Groq.
        expected_headers = HEADERS if provider == "openai" else {}
        assert probe.successes == [("same-request", expected_headers)]
        assert extract_response_headers(response) == expected_headers
        assert "SENTINEL" not in json.dumps(asdict(metadata))
        assert state.snapshot()["counts"] == {"mock-primary": 1}
    finally:
        marker.reset(token)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["ok", "missing_usage", "zero_usage", "partial_usage", "null_usage"])
@pytest.mark.parametrize("provider", ["openai", "groq"])
async def test_native_streaming_hook_cannot_prove_usage_presence(native_probe, behavior, provider):
    litellm, probe, marker = native_probe
    state = MockState()
    state.set_behavior("mock-primary", behavior, headers=HEADERS)
    token = marker.set("stream-request")
    try:
        with serve_mock(state) as server:
            stream = await litellm.acompletion(
                model=provider + "/mock-primary", api_base=server.base_url, api_key="synthetic-only",
                messages=[{"role": "user", "content": "BODY_SENTINEL"}], max_retries=0,
                stream=True, stream_options={"include_usage": True},
            )
            assert extract_response_headers(stream) == HEADERS
            normalized_usage_seen = False
            async for chunk in stream:
                normalized_usage_seen |= getattr(chunk, "usage", None) is not None
            await asyncio.sleep(0)
        assert normalized_usage_seen  # True even for missing upstream usage.
        assert probe.posts
        assert all(context == "stream-request" and metadata.usage is None
                   and metadata.usage_source == "unknown" for context, metadata in probe.posts)
        assert probe.streams
        assert all(context == "stream-request" and headers == HEADERS and not usage_present
                   for context, headers, usage_present in probe.streams)
        assert all(set(headers) <= ALLOWED_HEADERS for _, headers, _ in probe.streams)
    finally:
        marker.reset(token)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_parallel_callbacks_remain_request_local(native_probe):
    litellm, probe, marker = native_probe
    state = MockState()
    state.set_behavior("mock-primary", "ok", first_event_delay=.03)
    state.set_behavior("mock-secondary", "missing_usage")

    async def invoke(url, model):
        token = marker.set(model)
        try:
            return await litellm.acompletion(
                model="openai/" + model, api_base=url, api_key="synthetic-only",
                messages=[{"role": "user", "content": "synthetic"}], max_retries=0,
            )
        finally:
            marker.reset(token)

    with serve_mock(state) as server:
        await asyncio.gather(invoke(server.base_url, "mock-primary"), invoke(server.base_url, "mock-secondary"))
        await asyncio.sleep(0)
    observed = {context: metadata.usage for context, metadata in probe.posts}
    assert observed == {"mock-primary": OBSERVED, "mock-secondary": None}


def test_late_other_attempt_callback_cannot_overwrite_current_usage():
    from pathlib import Path
    from gateway.config import load_policy
    from gateway.context import RequestContext,current_request
    from gateway.hooks import GatewayGuard
    from gateway.state import MemoryState
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    guard=GatewayGuard().configure(policy,MemoryState())
    ctx=RequestContext(current_deployment='mock-secondary')
    token=current_request.set(ctx)
    try:
        value={'original_response':'{"usage":{"prompt_tokens":8,"completion_tokens":6,"total_tokens":14}}',
               'litellm_params':{'model_info':{'id':'mock-primary'}}}
        guard.log_post_api_call(value,None,None,None)
        assert ctx.observed_usage is None
        value['litellm_params']['model_info']['id']='mock-secondary'
        guard.log_post_api_call(value,None,None,None)
        assert ctx.observed_usage['total_tokens']==14
    finally:
        current_request.reset(token)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("behavior,refusal_survives", [
    ("refusal", False),
    ("refusal_after_content", False),
    ("refusal_with_content", True),
])
@pytest.mark.parametrize("provider", ["openai", "groq"])
async def test_native_stream_refusal_delivery_boundary(native_probe, behavior, refusal_survives, provider):
    """Snapshot the native gap; this is not a test claiming full refusal support.

    An isolated refusal delta is dropped, even after text. A refusal attached
    to a non-empty content delta survives normalization in these two paths.
    """
    litellm, probe, marker = native_probe
    state = MockState()
    state.set_behavior("mock-primary", behavior)
    token = marker.set("refusal-request")
    try:
        with serve_mock(state) as server:
            stream = await litellm.acompletion(
                model=provider + "/mock-primary", api_base=server.base_url, api_key="synthetic-only",
                messages=[{"role": "user", "content": "synthetic"}], max_retries=0,
                stream=True,
            )
            refused = False
            text = False
            reasons = []
            async for chunk in stream:
                for choice in chunk.choices:
                    refused |= bool(getattr(choice.delta, "refusal", None) or
                                    (getattr(choice.delta, "provider_specific_fields", None) or {}).get("refusal"))
                    text |= bool(choice.delta.content)
                    if choice.finish_reason is not None:
                        reasons.append(choice.finish_reason)
            await asyncio.sleep(0)
        assert refused is refusal_survives
        assert text is (behavior != "refusal")
        assert reasons == ["stop"]
        # The official deployment hook sees only the final typed terminal on
        # this pin; it cannot recover an earlier discarded refusal-only delta.
        assert probe.stream_signals == [(("stop", False),)]
        assert state.snapshot()["counts"] == {"mock-primary": 1}
    finally:
        marker.reset(token)
