"""Full native Proxy/DB security boundary using the explicit SDK text bridge.

The inherited checks deliberately reuse common contract/security obligations;
these are a second adapter execution of existing scenarios, not new features.
"""
from copy import deepcopy
import time
from unittest.mock import patch

from tests.integration import test_proxy_postgres as baseline
from tests import mock_upstream

class NativeSDKProxyPostgresTests(baseline.NativeProxyPostgresTests):
    POLICY_FILENAME='policy.sdk.mock.yaml'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Warm native lazy schemas/token accounting using synthetic data before
        # measuring fault budgets. This never runs in production bootstrap.
        old = cls.app.policy.limits.total_timeout
        cls.app.policy.limits.total_timeout = 15
        try:
            for streaming in (False, True):
                response = cls.http.post('/v1/chat/completions',
                    headers={'Authorization':'Bearer '+cls.key},
                    json={'model':'general-free','messages':[{'role':'user','content':'synthetic fixture warmup'}],
                          'stream':streaming,'max_completion_tokens':16}, timeout=20)
                if response.status_code != 200 or (streaming and 'data: [DONE]' not in response.text):
                    raise AssertionError('Synthetic native SDK fixture warmup failed')
            for _ in range(200):
                if cls.app.active_requests==0 and cls.upstream.active_requests==0:
                    break
                time.sleep(.01)
        finally:
            cls.app.policy.limits.total_timeout = old

    def _finished(self, request_id):
        for _ in range(100):
            result=self.http.get('/gateway/traces',params={'request_id':request_id},headers=self.admin_headers())
            self.assertEqual(result.status_code,200,result.text)
            finished=[v for v in result.json()['data'] if v['event']=='attempt_finished']
            if finished:
                return finished
            time.sleep(.01)
        self.fail('Missing completed request trace')

    def test_sdk_refusal_shapes_preserve_exact_semantics(self):
        for behavior in ('refusal','refusal_after_content','refusal_with_content'):
            with self.subTest(behavior=behavior):
                self.upstream.set_behavior('mock-primary',behavior)
                before=dict(self.upstream.counts)
                response=self.invoke(stream=True)
                self.assertEqual(response.status_code,200,response.text)
                self.assertIn('Synthetic policy refusal.',response.text)
                self.assertIn('"refusal":',response.text)
                self.assertIn('data: [DONE]',response.text)
                self.assertEqual(self.upstream.counts['mock-primary'],before.get('mock-primary',0)+1)
                self.assertEqual(self.upstream.counts['mock-secondary'],0)
                finished=self._finished(response.headers['x-request-id'])
                self.assertEqual(len(finished),1)
                self.assertEqual(finished[0]['status'],'refused')
                self.assertNotIn('Synthetic policy refusal.',str(finished))

    def test_sdk_observed_stream_usage_and_unknown_are_distinct(self):
        for behavior,expected in (('ok',14),('zero_usage',0),('missing_usage',None),('partial_usage',None),('null_usage',None)):
            with self.subTest(behavior=behavior):
                self.upstream.set_behavior('mock-primary',behavior)
                response=self.invoke(stream=True)
                self.assertEqual(response.status_code,200,response.text)
                self.assertIn('data: [DONE]',response.text)
                # Initial response headers cannot certify usage that arrives later.
                self.assertEqual(response.headers['x-usage-source'],'unknown')
                finished=self._finished(response.headers['x-request-id'])[-1]
                self.assertEqual(finished['usage_source'],'unknown' if expected is None else 'upstream_observed')
                self.assertEqual(finished.get('total_tokens'),expected)
                # Native estimated usage is never shipped as provider measurement.
                self.assertNotIn('"prompt_tokens":',response.text)

    def test_sdk_empty_and_terminal_refusal_are_valid(self):
        original=deepcopy(mock_upstream.STREAM_EVENTS)
        for refusal in (None,'Synthetic terminal refusal.'):
            with self.subTest(refusal=bool(refusal)):
                terminal=deepcopy(original[-2])
                terminal['choices'][0]['delta']={} if refusal is None else {'refusal':refusal}
                events=[deepcopy(original[0]),terminal,deepcopy(original[-1])]
                with patch.object(mock_upstream,'STREAM_EVENTS',events):
                    response=self.invoke(stream=True)
                self.assertEqual(response.status_code,200,response.text)
                self.assertIn('data: [DONE]',response.text)
                if refusal:
                    self.assertIn(refusal,response.text)
                finished=self._finished(response.headers['x-request-id'])[-1]
                self.assertEqual(finished['status'],'refused' if refusal else 'complete')
                self.assertEqual(self.upstream.counts['mock-secondary'],0)

    def test_sdk_trailing_transport_failure_never_sends_terminal(self):
        original=deepcopy(mock_upstream.STREAM_EVENTS)
        # Fixture disconnects after event index 2. Put the true upstream terminal
        # there: the bridge must withhold it until SDK consumption finishes safely.
        events=[original[0],original[2],original[-2],original[-1]]
        self.upstream.set_behavior('mock-primary','disconnect_after_content')
        with patch.object(mock_upstream,'STREAM_EVENTS',events):
            response=self.invoke(stream=True)
        self.assertNotIn('data: [DONE]',response.text)
        self.assertNotIn('"finish_reason":"stop"',response.text.replace(' ',''))
        self.assertEqual(self.upstream.counts['mock-secondary'],0)
        self.assertEqual(self._finished(response.headers['x-request-id'])[-1]['status'],'stream_interrupted')

    def test_sdk_pre_first_event_failure_has_one_bounded_fallback(self):
        self.upstream.set_behavior('mock-primary','disconnect_before_role')
        response=self.invoke(stream=True)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('data: [DONE]',response.text)
        self.assertEqual(self.upstream.counts['mock-primary'],1)
        self.assertEqual(self.upstream.counts['mock-secondary'],1)
        finished=self._finished(response.headers['x-request-id'])
        self.assertEqual({v['deployment_id']:v['status'] for v in finished},{'mock-primary':'failed','mock-secondary':'complete'})

    def _assert_post_event_failure(self, behavior):
        self.upstream.set_behavior('mock-primary',behavior)
        response=self.invoke(stream=True)
        self.assertNotIn('data: [DONE]',response.text)
        self.assertNotIn('"finish_reason":"stop"',response.text.replace(' ',''))
        self.assertEqual(self.upstream.counts['mock-primary'],1)
        self.assertEqual(self.upstream.counts['mock-secondary'],0)
        self.assertEqual(self._finished(response.headers['x-request-id'])[-1]['status'],'stream_interrupted')
        self.assertEqual(self.app.state.inflight,set())

    def test_sdk_role_only_failure_cannot_fallback(self):
        self._assert_post_event_failure('disconnect_after_role')

    def test_sdk_after_text_failure_cannot_fallback(self):
        self._assert_post_event_failure('disconnect_after_content')

    def test_sdk_total_deadline_stops_stream_and_releases_resources(self):
        self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
        old=self.app.policy.limits.total_timeout
        self.app.policy.limits.total_timeout=.2
        started=time.monotonic()
        try:
            response=self.invoke(stream=True)
        finally:
            self.app.policy.limits.total_timeout=old
        self.assertLess(time.monotonic()-started,1.5)
        self.assertNotIn('data: [DONE]',response.text)
        self.assertNotIn('"finish_reason":"stop"',response.text.replace(' ',''))
        self.assertEqual(self.upstream.counts['mock-secondary'],0)
        for _ in range(100):
            if self.app.active_requests==0 and self.upstream.active_requests==0:
                break
            time.sleep(.01)
        self.assertEqual(self.app.active_requests,0)
        self.assertEqual(self.app.state.inflight,set())
        traces=self.http.get('/gateway/traces',params={'request_id':response.headers['x-request-id']},headers=self.admin_headers()).json()['data']
        final=next(v for v in traces if v['event']=='request_finished')
        self.assertEqual(final['error_code'],'deadline_exceeded')

    def test_sdk_stream_headers_update_only_the_confirmed_quota_scope(self):
        self.upstream.set_behavior('mock-primary','ok',headers={'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'60s'})
        response=self.invoke(stream=True)
        self.assertIn('data: [DONE]',response.text)
        resources=self.http.get('/gateway/resources',headers=self.admin_headers()).json()
        pool=next(v for v in resources['pools'] if v['id']=='mock-primary-requests')
        self.assertEqual(pool['observation']['remaining'],0)
        second=self.invoke(stream=True)
        self.assertIn('data: [DONE]',second.text)
        self.assertEqual(self.upstream.counts['mock-primary'],1)
        self.assertEqual(self.upstream.counts['mock-secondary'],1)


    def _idle_without_reset(self):
        for _ in range(300):
            if self.app.active_requests==0 and self.upstream.active_requests==0:
                break
            time.sleep(.01)
        self.assertEqual(self.app.active_requests,0)
        self.assertEqual(self.upstream.active_requests,0)
        self.assertEqual(self.app.state.inflight,set())

    def _restore_healthy_same_key(self):
        self.upstream.set_behavior('mock-primary','ok')
        self.upstream.set_behavior('mock-secondary','ok')
        response=self.invoke()
        self.assertEqual(response.status_code,200,response.text)
        self._idle_without_reset()

    def test_sdk_same_key_recovers_after_repeated_nonstream_deadlines(self):
        old=self.app.policy.limits.total_timeout
        try:
            for _ in range(3):
                self.upstream.set_behavior('mock-primary','timeout',timeout_seconds=10)
                self.upstream.set_behavior('mock-secondary','timeout',timeout_seconds=10)
                self.app.policy.limits.total_timeout=.2
                response=self.invoke()
                self.assertEqual(response.status_code,504,response.text)
                self._idle_without_reset()
                self.app.policy.limits.total_timeout=old
                self._restore_healthy_same_key()
        finally:
            self.app.policy.limits.total_timeout=old

    def test_sdk_same_key_recovers_after_repeated_stream_deadlines(self):
        old=self.app.policy.limits.total_timeout
        try:
            for _ in range(3):
                self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
                self.upstream.set_behavior('mock-secondary','slow_stream',chunk_delay=.7)
                self.app.policy.limits.total_timeout=.2
                response=self.invoke(stream=True)
                self.assertNotIn('data: [DONE]',response.text)
                self._idle_without_reset()
                self.app.policy.limits.total_timeout=old
                self._restore_healthy_same_key()
        finally:
            self.app.policy.limits.total_timeout=old

    def test_sdk_same_key_recovers_after_repeated_disconnects_and_errors(self):
        for _ in range(3):
            self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
            self.upstream.set_behavior('mock-secondary','slow_stream',chunk_delay=.7)
            with self.http.stream('POST','/v1/chat/completions',headers={'Authorization':'Bearer '+self.key},
                    json={'model':'general-free','messages':[{'role':'user','content':'synthetic cancel'}],'stream':True}) as response:
                self.assertEqual(response.status_code,200)
                for line in response.iter_lines():
                    if line.startswith('data:'):break
            self._idle_without_reset()
            self._restore_healthy_same_key()
            self.upstream.set_behavior('mock-primary','disconnect_after_content')
            self.upstream.set_behavior('mock-secondary','disconnect_after_content')
            response=self.invoke(stream=True)
            self.assertNotIn('data: [DONE]',response.text)
            self._idle_without_reset()
            self._restore_healthy_same_key()

    def test_sdk_key_slot_released_when_cancelled_before_deployment_hook(self):
        import asyncio
        from gateway.hooks import guard
        original=guard.async_pre_call_deployment_hook
        old=self.app.policy.limits.total_timeout
        async def delay_after_native_admission(*args,**kwargs):
            await asyncio.sleep(.5)
            return await original(*args,**kwargs)
        try:
            for _ in range(3):
                before=sum(self.upstream.counts.values())
                self.app.policy.limits.total_timeout=.2
                with patch.object(guard,'async_pre_call_deployment_hook',delay_after_native_admission):
                    response=self.invoke()
                self.assertEqual(response.status_code,504,response.text)
                self._idle_without_reset()
                self.assertEqual(sum(self.upstream.counts.values()),before)
                self.app.policy.limits.total_timeout=old
                self._restore_healthy_same_key()
        finally:
            self.app.policy.limits.total_timeout=old

    def test_sdk_cancel_one_of_two_does_not_release_other_request(self):
        self._restore_healthy_same_key()
        self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
        self.upstream.set_behavior('mock-secondary','slow_stream',chunk_delay=.7)
        contexts=[]
        def start():
            context=self.http.stream('POST','/v1/chat/completions',headers={'Authorization':'Bearer '+self.key},
                json={'model':'general-free','messages':[{'role':'user','content':'synthetic held stream'}],'stream':True})
            response=context.__enter__()
            lines=response.iter_lines();contexts.append((context,lines))
            self.assertEqual(response.status_code,200)
            for line in lines:
                if line.startswith('data:'):break
        try:
            start();start()
            self.assertEqual(self.app.active_requests,2)
            contexts.pop(0)[0].__exit__(None,None,None)
            for _ in range(100):
                if self.app.active_requests==1 and self.upstream.active_requests==1:break
                time.sleep(.01)
            self.assertEqual(self.app.active_requests,1)
            self.assertEqual(self.upstream.active_requests,1)
            start()
            self.assertEqual(self.app.active_requests,2)
            self.assertEqual(self.invoke().status_code,429)
            self.assertEqual(self.app.active_requests,2)
        finally:
            for context,lines in contexts:context.__exit__(None,None,None)
        self._idle_without_reset()
        self._restore_healthy_same_key()

    def test_sdk_rejected_request_cleanup_cannot_free_another_keys_slot(self):
        response=self.http.post('/key/generate',headers=self.admin_headers(),json={
            'key_alias':'synthetic-single-slot','models':['general-free'],'duration':'1h','max_parallel_requests':1,
            'metadata':{'gateway':{'privacy_scope':'non_sensitive','deployment_ids':['mock-primary','mock-secondary']}}})
        self.assertEqual(response.status_code,200,response.text)
        key=response.json()['key']
        payload={'model':'general-free','messages':[{'role':'user','content':'synthetic single slot'}]}
        self._restore_healthy_same_key()
        try:
            for cycle in range(3):
                self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
                self.upstream.set_behavior('mock-secondary','slow_stream',chunk_delay=.7)
                with self.http.stream('POST','/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={**payload,'stream':True}) as held:
                    self.assertEqual(held.status_code,200)
                    held_lines=held.iter_lines()
                    for line in held_lines:
                        if line.startswith('data:'):break
                    for _ in range(3):
                        rejected=self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
                        self.assertEqual(rejected.status_code,429,rejected.text)
                        self.assertEqual(self.upstream.active_requests,1)
                self._idle_without_reset()
                self.upstream.set_behavior('mock-primary','ok')
                self.upstream.set_behavior('mock-secondary','ok')
                recovered=self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
                self.assertEqual(recovered.status_code,200,recovered.text)
                self._idle_without_reset()
        finally:
            self.http.post('/key/delete',headers=self.admin_headers(),json={'keys':[key]})
