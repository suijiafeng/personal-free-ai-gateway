"""Real HTTP smoke tests for the deterministic synthetic upstream and consumer."""
from __future__ import annotations

import time
import unittest

import httpx
from openai import OpenAI

from examples.client import consume, make_client
from tests.mock_upstream import MockBehavior, MockState, serve_mock


class MockUpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = MockState(capture_payloads=True)
        cls.server_context = serve_mock(cls.state)
        cls.server = cls.server_context.__enter__()
        cls.client = make_client(base_url=cls.server.base_url,
                                 api_key="synthetic-local-only", timeout=2.0)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server_context.__exit__(None, None, None)

    def setUp(self):
        self.wait_idle()
        self.state.reset()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.state.snapshot()["active_requests"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.state.snapshot()["active_requests"], 0)

    def complete(self, *, stream=True):
        return consume(self.client, model="mock-primary", stream=stream)

    def test_model_listing_does_not_count_as_generation(self):
        self.assertEqual([model.id for model in self.client.models.list()],
                         ["mock-primary", "mock-secondary"])
        self.assertEqual(dict(self.state.counts), {})

    def test_nonstream_shape_and_synthetic_payload_capture(self):
        result = self.complete(stream=False)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.text, "Hello from the mock upstream.")
        self.assertEqual(result.model, "mock-primary")
        self.assertEqual(result.usage["total_tokens"], 14)
        self.assertEqual(result.request_id, result.response_id)
        self.assertEqual(self.state.counts["mock-primary"], 1)
        self.assertEqual(self.state.requests[0]["messages"][0]["role"], "user")
        self.assertNotIn("synthetic-local-only", str(self.state.snapshot()))

    def test_sse_role_empty_content_finish_usage_and_single_id(self):
        with self.client.chat.completions.create(
            model="mock-primary", messages=[{"role": "user", "content": "Synthetic input"}],
            stream=True, stream_options={"include_usage": True},
        ) as stream:
            chunks = list(stream)
        self.assertEqual(len(chunks), 7)
        self.assertEqual(chunks[0].choices[0].delta.role, "assistant")
        self.assertEqual(chunks[1].choices[0].delta.content, "")
        self.assertEqual("".join(choice.delta.content or "" for chunk in chunks
                                 for choice in chunk.choices), "Hello from the mock upstream.")
        self.assertEqual(chunks[-2].choices[0].finish_reason, "stop")
        self.assertEqual(chunks[-1].choices, [])
        self.assertEqual(chunks[-1].usage.total_tokens, 14)
        self.assertEqual(len({chunk.id for chunk in chunks}), 1)

    def test_usage_absence_is_unknown_not_zero(self):
        self.state.set_behavior("mock-primary", "missing_usage")
        for stream in (True, False):
            with self.subTest(stream=stream):
                result = self.complete(stream=stream)
                self.assertEqual(result.status, "complete")
                self.assertIsNone(result.usage)
                self.assertEqual(result.usage_source, "unknown")

    def test_model_refusal_and_length_are_not_complete(self):
        for behavior, expected in (("refusal", "refused"), ("length", "truncated")):
            for stream in (True, False):
                with self.subTest(behavior=behavior, stream=stream):
                    self.state.set_behavior("mock-primary", behavior)
                    result = self.complete(stream=stream)
                    self.assertEqual(result.status, expected)
                    self.assertEqual(result.finish_reason, "stop" if behavior == "refusal" else "length")
                    if behavior == "refusal":
                        self.assertTrue(result.refusal)

    def test_faults_preserve_stream_commit_boundary(self):
        for behavior, status, text, event_count in (
            ("disconnect_before_role", "error", "", 0),
            ("disconnect_after_role", "interrupted", "", 1),
            ("disconnect_after_content", "interrupted", "Hello", 3),
            ("eof_without_finish", "interrupted", "Hello", 3),
            ("eof_after_role", "interrupted", "", 1),
            ("eof_after_content", "interrupted", "Hello", 3),
        ):
            with self.subTest(behavior=behavior):
                self.wait_idle()
                self.state.reset()
                self.state.set_behavior("mock-primary", behavior)
                result = self.complete()
                self.assertEqual(result.status, status)
                self.assertEqual(result.text, text)
                self.assertIsNone(result.finish_reason)
                self.assertIsNone(result.usage)
                self.assertEqual(self.state.counts["mock-primary"], 1)
                self.assertEqual(self.state.counts["mock-secondary"], 0)
                self.wait_idle()
                self.assertEqual(self.state.emitted_events["mock-primary"], event_count)

    def test_http_errors_have_no_sdk_retries_and_controlled_headers(self):
        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code):
                self.state.set_behavior("mock-primary", f"http_{code}", retry_after="2",
                                        headers={"x-ratelimit-remaining-requests": "0"})
                before = self.state.counts["mock-primary"]
                result = self.complete(stream=False)
                self.assertEqual(result.status, "error")
                self.assertEqual(self.state.counts["mock-primary"], before + 1)
        with httpx.Client() as http:
            self.state.set_behavior("mock-primary", "http_429", retry_after="2")
            response = http.post(self.server.base_url + "/chat/completions",
                                 json={"model": "mock-primary", "messages": []})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "2")

    def test_behavior_sequence_then_default(self):
        self.state.set_sequence("mock-primary", ["http_500", "http_429", "ok"])
        self.assertEqual([self.complete(stream=False).status for _ in range(4)],
                         ["error", "error", "complete", "complete"])
        self.assertEqual(self.state.counts["mock-primary"], 4)

    def test_timeout_stops_reading_and_registers_cancellation(self):
        self.state.set_behavior("mock-primary", "timeout", timeout_seconds=2.0)
        with make_client(base_url=self.server.base_url, api_key="synthetic-local-only",
                         timeout=0.05) as client:
            result = consume(client, model="mock-primary", stream=False)
        self.assertEqual(result.status, "error")
        self.wait_idle()
        self.assertEqual(self.state.cancelled["mock-primary"], 1)
        self.assertEqual(self.state.counts["mock-primary"], 1)

    def test_client_disconnect_closes_stream_and_frees_active_request(self):
        self.state.set_behavior("mock-primary", "slow_stream", chunk_delay=0.2)
        with self.client.chat.completions.create(
            model="mock-primary", messages=[{"role": "user", "content": "Synthetic input"}],
            stream=True,
        ) as stream:
            first = next(iter(stream))
            self.assertEqual(first.choices[0].delta.role, "assistant")
        self.wait_idle()
        self.assertEqual(self.state.cancelled["mock-primary"], 1)
        self.assertEqual(self.state.completed["mock-primary"], 0)
        self.assertEqual(self.state.counts["mock-secondary"], 0)

    def test_default_state_does_not_capture_payloads(self):
        state = MockState()
        with serve_mock(state) as server, make_client(
            base_url=server.base_url, api_key="synthetic-local-only"
        ) as client:
            self.assertEqual(consume(client, model="mock-primary", stream=False).status, "complete")
        self.assertEqual(state.requests, [])
        self.assertNotIn("requests", state.snapshot())

    def test_invalid_behavior_is_rejected(self):
        with self.assertRaises(ValueError):
            MockBehavior(kind="typo")
        with self.assertRaises(ValueError):
            MockBehavior(chunk_delay=-1)


if __name__ == "__main__":
    unittest.main()
