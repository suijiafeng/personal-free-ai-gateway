"""Small, non-retaining metadata boundary for native LiteLLM callbacks.

Feed ``extract_response_metadata`` the official post-API callback's
``original_response``, or the narrow stream_bridge's direct SDK usage snapshot.
The latter is captured before LiteLLM normalization. A normalized ModelResponse
or final native stream usage is not
evidence of upstream measurements: LiteLLM may have invented those numbers.
No adapter, request forwarding, stream consumption, or SSE parsing lives here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any


USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
QUOTA_HEADERS = frozenset(
    f"x-ratelimit-{quantity}-{dimension}"
    for quantity in ("limit", "remaining", "reset")
    for dimension in ("requests", "tokens")
)
ALLOWED_HEADERS = QUOTA_HEADERS | {"retry-after"}


@dataclass(frozen=True)
class ResponseMetadata:
    """Detached, allowlisted values; contains no body, IDs, or credentials."""

    usage: dict[str, int] | None
    usage_source: str
    headers: dict[str, str]


def filter_response_headers(headers: object) -> dict[str, str]:
    """Copy only quota/availability headers, without interpreting their units.

    Provider-specific dimension/window mapping belongs to the quota policy.
    Retry-After is availability feedback, never proof of a quota reset.
    """
    if not isinstance(headers, Mapping):
        return {}
    result: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name, value in headers.items():
        if not isinstance(name, str):
            continue
        name = name.lower()
        if name not in ALLOWED_HEADERS or name in ambiguous:
            continue
        if not isinstance(value, str) or not value or len(value) > 256:
            continue
        # Never retain line breaks, control characters, or binary header data.
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            continue
        value = value.strip()
        if not value:
            continue
        if name in result and result[name] != value:
            result.pop(name)
            ambiguous.add(name)
        else:
            result[name] = value
    return result


def extract_response_headers(response: object) -> dict[str, str]:
    """Extract native response header metadata, not normalized usage.

    Native async nonstream OpenAI supplies raw headers on the success response
    only AFTER log_post_api_call. Streams can supply raw response_headers in
    their deployment hook's request_data; prefer that direct mapping. A native
    stream wrapper also carries provider-prefixed additional_headers.
    """
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, Mapping):
        return {}
    headers = hidden.get("headers")
    if isinstance(headers, Mapping):
        return filter_response_headers(headers)
    additional = hidden.get("additional_headers")
    if not isinstance(additional, Mapping):
        return {}
    # Do not trust unprefixed convenience headers or any x-litellm-* fields.
    prefix = "llm_provider-"
    return filter_response_headers({
        name[len(prefix):]: value
        for name, value in additional.items()
        if isinstance(name, str) and name.lower().startswith(prefix)
    })


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Ambiguous response metadata")
        result[name] = value
    return result


def extract_response_metadata(
    original_response: object,
    response_headers: object = None,
    *,
    streaming: bool = False,
) -> ResponseMetadata:
    """Copy proven OpenAI-shaped nonstream token measurements, or unknown.

    Only a complete upstream usage trio is publishable. Missing/null/partial,
    negative, bool, floating-point, or string counts remain unknown; zero is
    valid only if the upstream explicitly supplied it. Other body fields and
    nested usage details are deliberately discarded. Estimates are neither
    accepted nor generated here, and streams always remain unknown.

    This function transiently decodes the existing JSON callback value; it
    never stores, logs, or returns that value. Typed/native response objects,
    iterators, coroutines, SSE strings, and other values are not inspected.
    """
    headers = filter_response_headers(response_headers)
    unknown = ResponseMetadata(None, "unknown", headers)
    if streaming:
        return unknown
    if isinstance(original_response, (str, bytes, bytearray)):
        try:
            original_response = json.loads(original_response, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            return unknown
    if not isinstance(original_response, Mapping):
        return unknown
    usage = original_response.get("usage")
    if not isinstance(usage, Mapping):
        return unknown
    observed: dict[str, int] = {}
    for name in USAGE_FIELDS:
        value = usage.get(name)
        if type(value) is not int or value < 0:
            return unknown
        observed[name] = value
    return ResponseMetadata(observed, "upstream_observed", headers)
