"""Reference consumer: official OpenAI Python SDK, without automatic retries.

Set GATEWAY_BASE_URL and GATEWAY_API_KEY for an already-created restricted key.
This example never creates a credential, retries, or switches models itself.
Run: python examples/client.py --prompt 'Say hello briefly.'
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

import httpx
from openai import OpenAI, OpenAIError


@dataclass
class CompletionResult:
    status: str
    text: str = ""
    refusal: str = ""
    finish_reason: str | None = None
    model: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    usage_source: str = "unknown"
    error_type: str | None = None


def make_client(*, base_url: str, api_key: str, timeout: float = 30.0) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout,
                  http_client=httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout))


def read_consumer_key(path: Path) -> str:
    # Reuse the standard-library-only, anchored 0400/0600 reader. No private key
    # needs to be copied into shell history, exported globally, or printed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
    try:
        from key_admin import read_key
        from gateway_ops import OpsError
        try:
            return read_key(path)
        except (OpsError, OSError, RecursionError):
            raise ValueError("Cannot safely read the private consumer key file.") from None
    finally:
        sys.path.pop(0)


def _terminal_status(finish_reason: str | None, refusal: str) -> str:
    if refusal or finish_reason == "content_filter":
        return "refused"
    if finish_reason == "length":
        return "truncated"
    if finish_reason == "stop":
        return "complete"
    # Missing/unknown terminal states cannot be presented as a complete answer.
    return "interrupted"


def consume(client: OpenAI, *, model: str = "general-free", stream: bool = True,
            messages: list[dict[str, str]] | None = None,
            max_completion_tokens: int = 128, include_usage: bool = False,
            on_text: Callable[[str], None] | None = None) -> CompletionResult:
    """Consume SDK objects; the official SDK owns SSE parsing and HTTP behavior.

    A clean HTTP EOF is not evidence of a complete answer: a terminal
    finish_reason is required. Partial text remains available on interruption.
    Missing upstream usage stays None with usage_source='unknown'.
    """
    result = CompletionResult(status="error")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "Say hello briefly."}],
        "max_completion_tokens": max_completion_tokens,
        "stream": stream,
    }
    received_event = False
    try:
        if stream:
            if include_usage:
                kwargs["stream_options"] = {"include_usage": True}
            with client.chat.completions.create(**kwargs) as chunks:
                result.request_id = chunks.response.headers.get("x-request-id")
                for chunk in chunks:
                    received_event = True
                    result.response_id = chunk.id
                    result.model = chunk.model
                    if chunk.usage is not None:
                        result.usage = chunk.usage.model_dump()
                        result.usage_source = "upstream_reported"
                    for choice in chunk.choices:
                        if choice.index != 0:
                            continue
                        if choice.delta.content:
                            result.text += choice.delta.content
                            if on_text is not None:
                                on_text(choice.delta.content)
                        if choice.delta.refusal:
                            result.refusal += choice.delta.refusal
                        if choice.finish_reason is not None:
                            result.finish_reason = choice.finish_reason
        else:
            completion = client.chat.completions.create(**kwargs)
            received_event = True
            result.request_id = getattr(completion, "_request_id", None)
            result.response_id, result.model = completion.id, completion.model
            if completion.usage is not None:
                result.usage = completion.usage.model_dump()
                result.usage_source = "upstream_reported"
            if completion.choices:
                choice = completion.choices[0]
                result.text = choice.message.content or ""
                result.refusal = choice.message.refusal or ""
                result.finish_reason = choice.finish_reason
                if on_text is not None and result.text:
                    on_text(result.text)
        result.status = _terminal_status(result.finish_reason, result.refusal)
    except KeyboardInterrupt:
        result.status = "cancelled"
    except (OpenAIError, httpx.HTTPError) as exc:
        result.status = "interrupted" if received_event else "error"
        # Never echo provider errors or request bodies into local diagnostics.
        result.error_type = type(exc).__name__
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:4000/v1"))
    parser.add_argument("--model", default="general-free")
    parser.add_argument("--prompt", default="Say hello briefly.")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--key-file", type=Path, help="Existing private JSON returned by ops/key_admin.py; key is not printed")
    args = parser.parse_args()
    try:
        key = read_consumer_key(args.key_file) if args.key_file is not None else os.getenv("GATEWAY_API_KEY")
    except ValueError:
        parser.error("Private consumer key file is unavailable or unsafe; no environment fallback was used.")
    if not key:
        parser.error("Use --key-file or set GATEWAY_API_KEY to an existing restricted consumer key")
    with make_client(base_url=args.base_url, api_key=key) as client:
        result = consume(
            client, model=args.model, stream=not args.no_stream,
            messages=[{"role": "user", "content": args.prompt}],
            on_text=lambda text: print(text, end="", flush=True),
        )
    print()
    metadata = asdict(result)
    # Output text and refusal are user-facing results, not diagnostic fields.
    metadata.pop("text")
    metadata.pop("refusal")
    if result.refusal:
        print(result.refusal)
    print(json.dumps(metadata, ensure_ascii=False), file=sys.stderr)
    return 0 if result.status == "complete" else 1 if result.status == "error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
