"""Real TCP contract tests: native LiteLLM Router plus its official guard hooks.

This isolates router/provider behavior from PostgreSQL and public proxy ingress.
Passing here is not proof of auth, endpoint isolation, or full-proxy behavior.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
import unittest

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm
from litellm import Router

from gateway.config import load_policy
from gateway.context import RequestContext, current_request
from gateway.contract import validate_request
from gateway.hooks import guard
from gateway.state import MemoryState
from tests.mock_upstream import MockState, serve_mock

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = [
    {"role": "system", "content": "Synthetic system instruction."},
    {"role": "user", "content": "Synthetic first question."},
    {"role": "assistant", "content": "Synthetic earlier answer."},
    {"role": "user", "content": "Synthetic follow-up."},
]


class NativeRouterContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = MockState(capture_payloads=True)
        cls.server_context = serve_mock(cls.upstream)
        cls.server = cls.server_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server_context.__exit__(None, None, None)

    async def asyncSetUp(self):
        for _ in range(300):
            if not self.upstream.snapshot()["active_requests"]:
                break
            await asyncio.sleep(0.01)
        self.upstream.reset()
        self.policy = load_policy(ROOT / "config/policy.mock.yaml")
        for deployment in self.policy.deployments:
            deployment.api_base = self.server.base_url
        self.policy.limits.first_event_timeout = 0.5
        self.policy.limits.stream_idle_timeout = 0.5
        self.policy.limits.total_timeout = 3
        self.state = MemoryState()
        guard.configure(self.policy, self.state)
        self.old_callbacks = litellm.callbacks[:]
        self.old_telemetry = getattr(litellm, "telemetry", None)
        litellm.callbacks = [guard]
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        litellm.turn_off_message_logging = True
        litellm.drop_params = False
        config = self.policy.native_config()
        for deployment in config["model_list"]:
            # Non-secret synthetic placeholder, ignored by the mock upstream.
            deployment["litellm_params"]["api_key"] = "synthetic-local-only"
        self.router = Router(model_list=config["model_list"], **config["router_settings"])
        self.chunks = []
        self.ctx = None

    async def asyncTearDown(self):
        self.router.discard()
        self.router.reset()
        litellm.callbacks = self.old_callbacks
        if self.old_telemetry is not None:
            litellm.telemetry = self.old_telemetry
        await asyncio.sleep(0.02)

    async def invoke(self, *, stream=False, allowed=None, privacy="non_sensitive"):
        body = validate_request({"model": "general-free", "messages": MESSAGES,
                                 "stream": stream, "max_completion_tokens": 32}, self.policy)
        self.ctx = RequestContext(
            body=body, revision=self.policy.revision,
            allowed_deployments=frozenset(allowed if allowed is not None else
                                         [d.id for d in self.policy.deployments]),
            privacy_scope=privacy, streaming=stream,
        )
        token = current_request.set(self.ctx)
        try:
            response = await self.router.acompletion(**body)
            if stream:
                # Proxy normally dispatches this official hook; standalone Router does not.
                iterator = guard.async_post_call_streaming_iterator_hook(None, response, {})
                async for chunk in iterator:
                    self.chunks.append(chunk)
                return self.chunks
            return response
        finally:
            current_request.reset(token)

    def assert_counts(self, primary, secondary):
        self.assertEqual(self.upstream.counts["mock-primary"], primary)
        self.assertEqual(self.upstream.counts["mock-secondary"], secondary)

    async def test_normal_uses_first_priority_and_preserves_history(self):
        response = await self.invoke()
        self.assertEqual(response.model, "mock-primary")
        self.assertEqual(response.choices[0].message.content, "Hello from the mock upstream.")
        self.assert_counts(1, 0)
        self.assertEqual(self.upstream.requests[0]["messages"], MESSAGES)
        self.assertEqual(self.ctx.attempts, 1)
        self.assertEqual(self.ctx.terminal, "complete")
        self.assertEqual(self.state.inflight, set())

    async def test_normal_stream_preserves_content_and_verified_terminal(self):
        await self.invoke(stream=True)
        text = "".join(choice.delta.content or "" for chunk in self.chunks for choice in chunk.choices)
        self.assertEqual(text, "Hello from the mock upstream.")
        self.assertTrue(self.ctx.genuine_terminal)
        self.assertEqual(self.ctx.terminal, "complete")
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())

    async def test_rate_limit_falls_back_once_and_records_attempt_chain(self):
        self.upstream.set_behavior("mock-primary", "http_429", retry_after="1")
        response = await self.invoke()
        self.assertEqual(response.model, "mock-secondary")
        self.assert_counts(1, 1)
        self.assertEqual(self.ctx.attempts, 2)
        self.assertEqual(self.upstream.requests[0]["messages"], self.upstream.requests[1]["messages"])
        events = [e for e in self.state.events if e["event"] == "attempt_started"]
        self.assertEqual([e["deployment_id"] for e in events], ["mock-primary", "mock-secondary"])
        self.assertEqual({e["request_id"] for e in events}, {self.ctx.request_id})
        self.assertEqual(self.state.pools["mock-primary-requests"]["status"], "cooldown")
        self.assertEqual(self.state.inflight, set())

    async def test_cooldown_blocks_primary_on_next_request(self):
        self.upstream.set_behavior("mock-primary", "http_429", retry_after="2")
        await self.invoke()
        await self.invoke()
        self.assert_counts(1, 2)
        self.assertEqual(self.ctx.attempts, 1)

    async def test_all_cooling_targets_make_no_new_upstream_attempt(self):
        self.upstream.set_behavior("mock-primary", "http_429", retry_after="2")
        self.upstream.set_behavior("mock-secondary", "http_429", retry_after="2")
        with self.assertRaises(Exception):
            await self.invoke()
        self.assert_counts(1, 1)
        with self.assertRaises(Exception):
            await self.invoke()
        self.assert_counts(1, 1)
        self.assertEqual(self.ctx.attempts, 0)

    async def test_audit_records_exclude_prompt_output_and_credentials(self):
        self.upstream.set_behavior("mock-primary", "http_429", retry_after="1")
        await self.invoke()
        records = str(self.state.events)
        for text in [m["content"] for m in MESSAGES] + [
            "synthetic-local-only", "Hello from the mock upstream.", self.server.base_url
        ]:
            self.assertNotIn(text, records)
        self.assertEqual({e["config_revision"] for e in self.state.events}, {self.policy.revision})
        self.assertEqual({e["request_id"] for e in self.state.events}, {self.ctx.request_id})

    async def test_unapproved_fallback_capability_rejected_before_any_call(self):
        self.policy.deployments[1].capability.parameters = []
        with self.assertRaises(Exception):
            await self.invoke()
        self.assert_counts(0, 0)

    async def test_two_server_failures_never_exceed_two_attempts(self):
        self.upstream.set_behavior("mock-primary", "http_500")
        self.upstream.set_behavior("mock-secondary", "http_500")
        with self.assertRaises(Exception):
            await self.invoke()
        self.assert_counts(1, 1)
        self.assertLessEqual(self.ctx.attempts, 2)
        self.assertEqual(self.state.inflight, set())

    async def test_upstream_authentication_error_does_not_expand_to_secondary(self):
        self.upstream.set_behavior("mock-primary", "http_401")
        with self.assertRaises(Exception):
            await self.invoke()
        self.assert_counts(1, 0)
        self.assertTrue(self.ctx.stop_attempts)
        self.assertIn("mock-primary-requests", self.state.disabled)

    async def test_unauthorized_secondary_never_called(self):
        self.upstream.set_behavior("mock-primary", "http_500")
        with self.assertRaises(Exception):
            await self.invoke(allowed={"mock-primary"})
        self.assert_counts(1, 0)

    async def test_privacy_mismatch_calls_no_upstream(self):
        with self.assertRaises(Exception):
            await self.invoke(privacy="unapproved")
        self.assert_counts(0, 0)

    async def test_unknown_or_paid_candidate_never_called(self):
        for status in ("unknown", "paid"):
            with self.subTest(status=status):
                self.policy.deployments[0].eligibility.status = status
                response = await self.invoke()
                self.assertEqual(response.model, "mock-secondary")
                self.assertEqual(self.upstream.counts["mock-primary"], 0)
        self.assert_counts(0, 2)

    async def test_missing_usage_remains_unknown(self):
        self.upstream.set_behavior("mock-primary", "missing_usage")
        response = await self.invoke()
        self.assertIsNone(response.usage)
        self.assertEqual(self.ctx.usage_source, "unknown")
        self.assertIsNone(self.ctx.observed_usage)
        self.assert_counts(1, 0)

    async def test_length_preserves_truncated_semantics_without_fallback(self):
        self.upstream.set_behavior("mock-primary", "length")
        response = await self.invoke()
        self.assertEqual(response.choices[0].finish_reason, "length")
        self.assertEqual(self.ctx.terminal, "truncated")
        self.assert_counts(1, 0)

    async def test_refusal_preserved_without_fallback_or_complete_diagnostic(self):
        self.upstream.set_behavior("mock-primary", "refusal")
        response = await self.invoke()
        self.assertEqual(response.choices[0].message.refusal, "Synthetic policy refusal.")
        self.assertEqual(self.ctx.terminal, "refused")
        self.assert_counts(1, 0)

    async def test_stream_refusal_native_gap_fails_closed_without_fallback(self):
        # KNOWN NATIVE GAP: 1.104.0 discards refusal-only deltas. An empty stop cannot
        # certify a complete answer. No custom provider/SSE parser is introduced.
        # Precise streaming refusal text/classification remains unsupported.
        self.upstream.set_behavior("mock-primary", "refusal")
        with self.assertRaises(Exception):
            await self.invoke(stream=True)
        self.assertEqual(self.ctx.terminal, "stream_interrupted")
        self.assertFalse(any(choice.finish_reason for chunk in self.chunks for choice in chunk.choices))
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())

    async def test_stream_refusal_with_content_preserved_without_fallback(self):
        # This shape survives native normalization; do not generalize to
        # refusal-only deltas, including a refusal-only delta after prior text.
        self.upstream.set_behavior("mock-primary", "refusal_with_content")
        await self.invoke(stream=True)
        refusal = "".join(getattr(choice.delta, "refusal", None) or ""
                          for chunk in self.chunks for choice in chunk.choices)
        self.assertEqual(refusal, "Synthetic policy refusal.")
        self.assertEqual(self.ctx.terminal, "refused")
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())
        self.assertNotIn("Synthetic policy refusal.", str(self.state.events))

    async def test_stream_content_filter_preserves_refused_without_fallback(self):
        self.upstream.set_behavior("mock-primary", "content_filter")
        await self.invoke(stream=True)
        reasons = [choice.finish_reason for chunk in self.chunks for choice in chunk.choices
                   if choice.finish_reason is not None]
        self.assertEqual(reasons, ["content_filter"])
        self.assertEqual(self.ctx.terminal, "refused")
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())

    async def test_stream_length_preserves_truncated_without_fallback(self):
        self.upstream.set_behavior("mock-primary", "length")
        await self.invoke(stream=True)
        reasons = [choice.finish_reason for chunk in self.chunks for choice in chunk.choices
                   if choice.finish_reason is not None]
        self.assertEqual(reasons, ["length"])
        self.assertEqual(self.ctx.terminal, "truncated")
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())

    async def test_unsupported_stream_terminal_is_not_a_success_looking_finish(self):
        self.upstream.set_behavior("mock-primary", "unexpected_finish")
        with self.assertRaises(Exception):
            await self.invoke(stream=True)
        self.assertEqual(self.ctx.terminal, "stream_interrupted")
        self.assertTrue(self.ctx.stop_attempts)
        self.assertFalse(any(choice.finish_reason for chunk in self.chunks for choice in chunk.choices))
        self.assert_counts(1, 0)
        self.assertEqual(self.state.inflight, set())
        finished = [event for event in self.state.events if event["event"] == "attempt_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "stream_interrupted")

    async def test_stream_usage_remains_unknown_and_omitted(self):
        await self.invoke(stream=True)
        self.assertEqual(self.ctx.usage_source, "unknown")
        self.assertIsNone(self.ctx.observed_usage)
        self.assertTrue(all(getattr(chunk, "usage", None) is None for chunk in self.chunks))
        finished = [event for event in self.state.events if event["event"] == "attempt_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["usage_source"], "unknown")
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.assertNotIn(field, finished[0])

    async def test_pre_event_disconnect_allows_only_one_eligible_fallback(self):
        self.upstream.set_behavior("mock-primary", "disconnect_before_role")
        await self.invoke(stream=True)
        self.assert_counts(1, 1)
        self.assertEqual(self.ctx.terminal, "complete")
        self.assertTrue(all(chunk.model == "mock-secondary" for chunk in self.chunks))
        self.assertEqual(self.state.inflight, set())

    async def test_nonstream_timeout_has_bounded_fallback(self):
        self.upstream.set_behavior("mock-primary", "timeout", timeout_seconds=2)
        started = time.monotonic()
        response = await self.invoke()
        self.assertLess(time.monotonic() - started, self.policy.limits.total_timeout)
        self.assertEqual(response.model, "mock-secondary")
        self.assert_counts(1, 1)
        self.assertEqual(self.state.inflight, set())

    async def test_stream_idle_timeout_does_not_switch_after_role(self):
        self.upstream.set_behavior("mock-primary", "slow_stream", chunk_delay=2)
        with self.assertRaises(Exception):
            await self.invoke(stream=True)
        self.assert_counts(1, 0)
        self.assertEqual(self.ctx.terminal, "stream_interrupted")
        self.assertFalse(any(choice.finish_reason for chunk in self.chunks for choice in chunk.choices))
        self.assertEqual(self.state.inflight, set())

    async def test_downstream_cancellation_releases_pool_and_closes_upstream(self):
        self.upstream.set_behavior("mock-primary", "slow_stream", chunk_delay=2)
        task = asyncio.create_task(self.invoke(stream=True))
        deadline = time.monotonic() + 2
        while not self.chunks and not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(self.chunks, "Expected a committed role event before cancellation")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_counts(1, 0)
        self.assertTrue(self.ctx.stop_attempts)
        self.assertEqual(self.ctx.terminal, "cancelled")
        self.assertEqual(self.state.inflight, set())
        deadline = time.monotonic() + 1
        while self.upstream.snapshot()["active_requests"] and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(self.upstream.active_requests, 0)
        self.assertEqual(self.upstream.cancelled["mock-primary"], 1)

    async def test_missing_policy_context_fails_before_upstream(self):
        with self.assertRaises(Exception):
            await self.router.acompletion(model="general-free", messages=MESSAGES)
        self.assert_counts(0, 0)

    async def test_success_hook_audit_failure_marks_ingress_blocker(self):
        class FailFinalRecord(MemoryState):
            async def record(self, event):
                if event["event"] == "attempt_finished":
                    raise RuntimeError("Synthetic audit storage failure")
                await super().record(event)
        self.state = FailFinalRecord()
        guard.configure(self.policy, self.state)
        # Native LiteLLM swallows this post-hook exception. Public ingress must
        # reject the returned object using these explicit context flags.
        await self.invoke()
        self.assertTrue(self.ctx.audit_failed)
        self.assertTrue(self.ctx.stop_attempts)
        self.assertFalse(self.ctx.attempt_finalized)
        self.assertFalse(self.state.healthy)
        self.assert_counts(1, 0)
        self.assertFalse(any(e["event"] == "attempt_finished" for e in self.state.events))

    async def test_failure_hook_audit_failure_prevents_secondary(self):
        class FailFinalRecord(MemoryState):
            async def record(self, event):
                if event["event"] == "attempt_finished":
                    raise RuntimeError("Synthetic audit storage failure")
                await super().record(event)
        self.state = FailFinalRecord()
        guard.configure(self.policy, self.state)
        self.upstream.set_behavior("mock-primary", "http_500")
        with self.assertRaises(Exception):
            await self.invoke()
        self.assertTrue(self.ctx.audit_failed)
        self.assertTrue(self.ctx.stop_attempts)
        self.assertFalse(self.ctx.attempt_finalized)
        self.assertFalse(self.state.healthy)
        self.assert_counts(1, 0)

    async def test_success_release_failure_leaves_unfinalized_ingress_blocker(self):
        class FailRelease(MemoryState):
            async def release(self, ids, *, status="unknown", cooldown=0):
                # Mirrors PostgresState's fail-closed health flag on persistence
                # failure; this test does not claim database recovery coverage.
                self.healthy = False
                raise RuntimeError("Synthetic pool persistence failure")
        self.state = FailRelease()
        guard.configure(self.policy, self.state)
        await self.invoke()
        self.assertFalse(self.ctx.attempt_finalized)
        self.assertFalse(self.state.healthy)
        self.assert_counts(1, 0)

    async def test_failure_release_failure_prevents_secondary(self):
        class FailRelease(MemoryState):
            async def release(self, ids, *, status="unknown", cooldown=0):
                self.healthy = False
                raise RuntimeError("Synthetic pool persistence failure")
        self.state = FailRelease()
        guard.configure(self.policy, self.state)
        self.upstream.set_behavior("mock-primary", "http_500")
        with self.assertRaises(Exception):
            await self.invoke()
        self.assertFalse(self.ctx.attempt_finalized)
        self.assertFalse(self.state.healthy)
        self.assert_counts(1, 0)

    async def _assert_interrupted(self, behavior, expected_text):
        self.upstream.set_behavior("mock-primary", behavior)
        with self.assertRaises(Exception):
            await self.invoke(stream=True)
        self.assert_counts(1, 0)
        text = "".join(choice.delta.content or "" for chunk in self.chunks for choice in chunk.choices)
        self.assertEqual(text, expected_text)
        self.assertFalse(any(choice.finish_reason for chunk in self.chunks for choice in chunk.choices))
        self.assertEqual(self.ctx.terminal, "stream_interrupted")
        self.assertEqual(self.state.inflight, set())

    async def test_abrupt_role_disconnect_is_not_retried(self):
        await self._assert_interrupted("disconnect_after_role", "")

    async def test_abrupt_content_disconnect_is_not_retried(self):
        await self._assert_interrupted("disconnect_after_content", "Hello")

    async def test_clean_role_eof_has_no_synthetic_success_or_fallback(self):
        await self._assert_interrupted("eof_after_role", "")

    async def test_clean_content_eof_has_no_synthetic_success_or_fallback(self):
        await self._assert_interrupted("eof_after_content", "Hello")


if __name__ == "__main__":
    unittest.main()
