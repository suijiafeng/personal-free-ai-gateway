# V0.2 native stream boundary review

Reviewed: 2026-10-06 UTC. Runtime: the existing frozen Python 3.12 environment
with `litellm==1.104.0`. Only synthetic loopback upstreams and synthetic
credentials were used. No production provider was enabled or contacted. The
original V0.1 source, native LiteLLM package, Router, provider transformations,
and native SSE processing were not changed. No dependency upgrade was made.

## Outcome

Full streaming usage provenance and arbitrary streaming refusal preservation
are **not supported** through the reviewed official hooks in this pin. Keep
stream usage `unknown`; do not replace missing evidence with normalized values.
For exact refusal or measured token-count requirements, use an explicitly
requested nonstreaming call on a separately certified provider path. Do not
automatically replay or change a committed streaming request.

A narrower supported hardening was implemented: the gateway's official typed
iterator hook rejects unexpected terminal reasons before yielding the terminal.
Only `stop`, `length`, and `content_filter` are supported by the text-only
contract. Previously an unexpected terminal could be delivered even though the
audit status said `failed`. V0.2 interrupts that stream, releases its pool,
records one interrupted attempt, and makes no fallback call.

## Evidence from the exact package and official upstream

The [official callback documentation](https://docs.litellm.ai/docs/observability/custom_callback)
and [proxy hook documentation](https://docs.litellm.ai/docs/proxy/call_hooks)
identify supported extension points. The observed behavior below was established
by reading the installed pin and exercising native adapters, rather than by
assuming every documented callback sees the raw upstream representation.

Relevant versioned sources:

- [CustomLogger at v1.104.0](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/integrations/custom_logger.py):
  `log_post_api_call`, `async_post_call_streaming_deployment_hook`, and
  `async_post_call_streaming_iterator_hook`. No separate typed raw-provider-
  chunk callback is exposed by this interface.
- [Streaming handler at v1.104.0](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/litellm_core_utils/streaming_handler.py):
  `return_processed_chunk_logic` preserves refusal carried with non-empty
  content but drops a refusal-only chunk; `__anext__` removes usage before its
  deployment callback; `_finalize_completed_stream` can synthesize final usage.
  The async deployment callback is dispatched for a real terminal, despite the
  interface's broader per-chunk description.
- [Logging at v1.104.0](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/litellm_core_utils/litellm_logging.py):
  post-call logging exposes the response envelope/stream object, not a separate
  synchronous raw-chunk observation boundary. Success/stream logging receives
  processed or assembled responses and is not independent provenance.

Installed-source SHA-256, recorded for reproducing this review (not a claim of
whole-package reproducible-build verification):

| File below `litellm/` | SHA-256 |
| --- | --- |
| `integrations/custom_logger.py` | `d0260a889c370eb764c05fcd315fc3eb594a2b1dd708e57a94209ad7d803303a` |
| `litellm_core_utils/streaming_handler.py` | `a0891d3c589175789eb527fa80a89cdcb5b77dcbab05640ac87234e560819c4f` |
| `litellm_core_utils/litellm_logging.py` | `28f8f13a0913d01f09e1b641d11ec53ed4d7b8696f07589c8a79a704de59b82f` |

## Native TCP observations

Both the OpenAI and Groq native adapters were exercised against the same
loopback HTTP fixture. These are adapter-path tests, not proof of real-provider
behavior, billing eligibility, production transport, or actual consumer support.

1. Valid, omitted, explicit-zero, partial, and null upstream stream usage all
   result in some normalized final usage. No tested deployment-hook chunk has
   an independently attributable usage measurement. Gateway extraction remains
   unknown for every case. It stores no raw chunks, callback kwargs, refusal
   text, prompt, or completion in diagnostics.
2. A refusal-only delta with no text is dropped. The native path still supplies
   `stop`; the gateway rejects the unsupported empty stream without fallback.
3. A refusal-only delta **after prior text is also dropped**. This cannot be
   detected from the reviewed supported hooks and may appear as an ordinary
   text completion. Do not claim the empty-stop guard rejects all lost refusals.
4. A refusal accompanying non-empty content in the **same delta** survives in
   both native adapters. The gateway preserves that typed field, classifies it
   `refused`, and does not retain its text in audit metadata.
5. `content_filter` and `length` preserve their explicit terminal semantics.
   Unsupported terminal reasons are now rejected by the gateway iterator hook
   before a terminal is delivered. Earlier text is not withdrawn or replayed.
6. These async native paths call the deployment hook on the final typed terminal.
   The ordinary content/refusal stream is observed through the official iterator
   hook, not reconstructed from private upstream buffers.

## Focused verification

Run using the frozen environment from the V0.2 repository:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True LITELLM_TELEMETRY=False \
  .venv/bin/python -m pytest -q \
  tests/test_metadata.py tests/contract/test_native_router.py tests/test_mock_upstream.py
```

If the frozen virtual environment is kept separately, substitute its interpreter
path; do not silently upgrade the dependency pin. The metadata tests explicitly
assert `litellm==1.104.0`.

Final focused result: **92 passed, 19 subtests passed, 33 dependency warnings
in 18.80 seconds**. Breakdown: 46 metadata/provenance tests, 34 native Router
contract tests, and 12 fixture/consumer smoke tests. This adds 17 collected
regressions over the corresponding V0.1 suites. The warnings are upstream
Pydantic deprecation/ReadOnly warnings, not skipped or failed tests.

The run used the already-installed sibling V0.1 virtual-environment interpreter
while resolving gateway/test modules from the V0.2 checkout. Test XML and console
output are archived as the V0.2 focused regression evidence; no native package files
were changed.

This review does not replace full native Proxy + PostgreSQL, Nginx, actual
consumer, or live-provider acceptance. Production stays disabled. No upstream
issue, patch, or third-party communication was submitted.
