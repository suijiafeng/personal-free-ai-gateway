"""A narrow supported CustomLLM bridge to the pinned official OpenAI SDK.

The SDK owns HTTP, JSON and SSE decoding. LiteLLM owns routing and proxy output.
This bridge validates already-decoded dictionaries, preserves refusal/empty events,
and observes bounded metadata before LiteLLM can normalize or estimate it.
It is not a general provider adapter; only the policy's explicit text contract
is supported. See docs/adr-sdk-stream-bridge.md for compatibility obligations.
"""
from __future__ import annotations

import importlib.metadata
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, AsyncStream, APIStatusError, APITimeoutError, APIConnectionError, APIError

from .context import current_request
from .metadata import extract_response_metadata, filter_response_headers

SDK_PIN = "2.54.0"
TERMINALS = frozenset({"stop", "length", "content_filter"})


def _fail(message: str, status: int = 502):
    # Every message is fixed, never interpolated from provider data or credentials.
    raise CustomLLMError(status, message)


@dataclass(frozen=True)
class _Binding:
    context: Any = field(repr=False)
    deployment_id: str
    attempt: int
    model: str
    api_base: str
    api_key: str = field(repr=False)

    def check(self):
        ctx = current_request.get()
        if (ctx is not self.context or ctx.current_deployment != self.deployment_id
                or ctx.attempts != self.attempt or ctx.stop_attempts):
            _fail("Stale or stopped guarded attempt")
        return ctx


def _bind(model, api_base, api_key, messages, optional_params, streaming):
    from .hooks import guard
    ctx = current_request.get()
    if ctx is None or ctx.current_deployment not in guard.deployments:
        _fail("Missing guarded deployment", 403)
    d = guard.deployments[ctx.current_deployment]
    if (d.adapter != "openai_sdk" or model != d.model
            or str(api_base).rstrip("/") != d.api_base.rstrip("/")
            or not guard._eligible(d, ctx) or ctx.current_pools != tuple(d.quota_pools)
            or messages != ctx.body.get("messages") or streaming != ctx.streaming):
        _fail("SDK bridge target differs from guarded deployment", 403)
    if guard.policy.profile == "production" and api_key != os.environ.get(d.credential_env):
        _fail("SDK bridge credential differs from approved reference", 403)
    if not isinstance(api_key, str) or not api_key:
        _fail("SDK bridge credential reference is empty", 403)
    if importlib.metadata.version("openai") != SDK_PIN:
        _fail("Pinned SDK version mismatch")
    # The native CustomLLM parameter mapper uses max_tokens for this documented
    # extension. Check its exact value against the already validated user input;
    # do not silently delete an unknown option or lose the output bound.
    accepted = {"max_tokens", "max_completion_tokens", "stream", "max_retries", "stream_options"}
    if set(optional_params) - accepted:
        _fail("Unsupported SDK bridge parameter", 400)
    # Native Proxy adds include_usage for its own accounting. This is not a new
    # consumer option: public ingress still rejects stream_options completely.
    stream_options = optional_params.get("stream_options")
    if stream_options is not None and (not streaming or not isinstance(stream_options, dict)
            or set(stream_options) != {"include_usage"} or stream_options["include_usage"] is not True):
        _fail("Unsupported SDK infrastructure stream options", 400)
    if optional_params.get("max_retries", 0) != 0:
        _fail("SDK retries are forbidden", 400)
    maximum = optional_params.get("max_completion_tokens", optional_params.get("max_tokens"))
    if maximum != ctx.body.get("max_completion_tokens") or type(maximum) is not int:
        _fail("SDK output limit differs from approved request", 400)
    if "max_tokens" in optional_params and "max_completion_tokens" in optional_params:
        _fail("Ambiguous SDK output limit", 400)
    if optional_params.get("stream", False) is not streaming:
        _fail("SDK streaming mode differs from approved request", 400)
    return _Binding(ctx, d.id, ctx.attempts, d.model.split("/", 1)[1], d.api_base, api_key), maximum


def _sdk(binding, timeout):
    # No redirect may move an authorized credential/request to a different URL.
    # The SDK still uses the environment's network/proxy controls.
    return AsyncOpenAI(api_key=binding.api_key, base_url=binding.api_base,
        max_retries=0, timeout=timeout,
        http_client=DefaultAsyncHttpxClient(follow_redirects=False, timeout=timeout))


def _observe_usage(binding, usage):
    ctx = binding.check()
    if usage is None:
        return
    # SDK parse(to=...) returns decoded dictionaries without numeric coercion.
    # Typed CompletionUsage coerces bool/string/float, so it is NOT provenance.
    metadata = extract_response_metadata({"usage": usage})
    if ctx.observed_usage is not None and metadata.usage != ctx.observed_usage:
        _fail("Conflicting upstream usage measurements")
    ctx.observed_usage, ctx.usage_source = metadata.usage, metadata.usage_source


def _envelope(response, streaming):
    if (not isinstance(response, dict) or not isinstance(response.get("id"), str)
            or not response["id"] or type(response.get("created")) is not int
            or response.get("object") != ("chat.completion.chunk" if streaming else "chat.completion")):
        _fail("Unsupported upstream response envelope")
    return response


def _choice(choices, streaming):
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or type(choices[0].get("index")) is not int or choices[0]["index"] != 0):
        _fail("Unsupported upstream choice schema")
    choice = choices[0]
    delta = choice.get("delta" if streaming else "message")
    if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("function_call"):
        _fail("Unsupported upstream output schema")
    if any(value not in (None, [], {}) for key, value in delta.items()
           if key not in {"content", "refusal", "role", "tool_calls", "function_call"}):
        _fail("Unsupported upstream nontext or additional output fields")
    if delta.get("role") not in (None, "assistant"):
        _fail("Unsupported upstream output role")
    for name in ("content", "refusal"):
        value = delta.get(name)
        if value is not None and not isinstance(value, str):
            _fail("Unsupported upstream text schema")
    reason = choice.get("finish_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in TERMINALS):
        _fail("Unsupported upstream terminal")
    if not streaming and reason is None:
        _fail("Missing upstream terminal")
    return choice, delta, reason


def _status_error(error, binding):
    ctx = binding.check()
    headers = filter_response_headers(error.response.headers)
    ctx.observed_headers.update(headers)
    safe = CustomLLMError(error.status_code, "Upstream rejected the guarded request")
    safe.headers = headers
    return safe


class SDKTextBridge(CustomLLM):
    async def acompletion(self, model, messages, api_base, api_key, optional_params,
                          timeout=None, **kwargs):
        binding, maximum = _bind(model, api_base, api_key, messages, optional_params, False)
        try:
            async with _sdk(binding, timeout) as sdk:
                raw = await sdk.chat.completions.with_raw_response.create(
                    model=binding.model, messages=messages, max_tokens=maximum, stream=False)
                ctx = binding.check()
                ctx.observed_headers.update(filter_response_headers(raw.headers))
                response = _envelope(raw.parse(to=dict[str, Any]), False)
                choice, message, reason = _choice(response.get("choices"), False)
                _observe_usage(binding, response.get("usage"))
                result = ModelResponse(id=response["id"], created=response["created"], model=binding.model,
                    choices=[{"index":0,"finish_reason":reason,"message":{
                        "role":"assistant","content":message.get("content"),"refusal":message.get("refusal")}}],
                    usage=ctx.observed_usage)
                result._hidden_params["headers"] = dict(ctx.observed_headers)
                return result
        except APIStatusError as error:
            raise _status_error(error, binding) from None
        except APITimeoutError:
            _fail("Upstream request timed out", 408)
        except APIConnectionError:
            _fail("Upstream connection failed")
        except APIError:
            _fail("Upstream SDK rejected an invalid response")

    async def astreaming(self, model, messages, api_base, api_key, optional_params,
                         timeout=None, **kwargs) -> AsyncIterator[GenericStreamingChunk]:
        binding, maximum = _bind(model, api_base, api_key, messages, optional_params, True)
        terminal = False
        try:
            async with _sdk(binding, timeout) as sdk:
                raw = await sdk.chat.completions.with_raw_response.create(
                    model=binding.model, messages=messages, max_tokens=maximum, stream=True,
                    stream_options={"include_usage":True})
                binding.check().observed_headers.update(filter_response_headers(raw.headers))
                # Public parse(to=...) selects the SDK's uncoerced structured
                # stream representation. The SDK still decodes all SSE/JSON.
                async with raw.parse(to=AsyncStream[dict[str, Any]]) as stream:
                    async for chunk in stream:
                        ctx = binding.check()
                        _envelope(chunk, True)
                        _observe_usage(binding, chunk.get("usage"))
                        if chunk.get("choices") == []:
                            continue
                        choice, delta, reason = _choice(chunk.get("choices"), True)
                        if terminal:
                            _fail("Unexpected upstream event after terminal")
                        # GenericStreamingChunk.provider_specific_fields is a supported
                        # extension: refusal-only and genuine empty SDK events survive
                        # native filtering, without invented sentinel content.
                        if reason is None or delta.get("content") or delta.get("refusal"):
                            yield {"text":delta.get("content") or "", "tool_use":None,"index":0,
                                "is_finished":False,"finish_reason":"","usage":None,
                                "provider_specific_fields":{"refusal":delta["refusal"]} if delta.get("refusal") else {}}
                        if reason is not None:
                            terminal = True
                            ctx.genuine_terminal = True
                            terminal_reason = reason
                if not terminal:
                    _fail("Upstream ended without a terminal event")
                # Hold only the terminal marker, not the content: a transport error
                # while receiving trailing usage cannot become a success-looking stop.
                binding.check()
                yield {"text":"","tool_use":None,"index":0,
                    "is_finished":True,"finish_reason":terminal_reason,"usage":None}
        except APIStatusError as error:
            raise _status_error(error, binding) from None
        except APITimeoutError:
            _fail("Upstream request timed out", 408)
        except APIConnectionError:
            _fail("Upstream connection failed")
        except APIError:
            _fail("Upstream SDK rejected an invalid response")


bridge = SDKTextBridge()
