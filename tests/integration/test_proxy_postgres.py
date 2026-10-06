"""Real native LiteLLM Proxy + PostgreSQL + TCP mock, not an in-memory auth substitute.

Run in the same network namespace as the disposable Postgres fixture. Real providers
and the user's actual consumer are deliberately excluded.
"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import urlparse
import unittest

import httpx
import uvicorn
import yaml
import pytest
import psycopg
from concurrent.futures import ThreadPoolExecutor

from tests.mock_upstream import MockState, serve_mock

ROOT=Path(__file__).resolve().parents[2]
ADMIN="sk-test-only-administrator-not-a-real-secret"
SALT="test-only-independent-encryption-salt"


@unittest.skipUnless(os.getenv("GATEWAY_TEST_DATABASE_URL"),"PostgreSQL fixture URL not set; not a DB test pass")
class NativeProxyPostgresTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def capture_output(self, capfd):
        self.capture = capfd

    @classmethod
    def setUpClass(cls):
        fixture_url=urlparse(os.environ['GATEWAY_TEST_DATABASE_URL'])
        if fixture_url.hostname not in ('127.0.0.1','localhost','postgres-test') or fixture_url.path != '/gateway_test':
            raise RuntimeError('This mutating suite requires an isolated loopback/postgres-test database named gateway_test.')
        cls.tmp=tempfile.TemporaryDirectory(prefix="gateway-proxy-fixture-")
        cls.upstream=MockState(capture_payloads=True)
        cls.mock_context=serve_mock(cls.upstream)
        cls.mock=cls.mock_context.__enter__()
        policy=yaml.safe_load((ROOT/"config"/getattr(cls,"POLICY_FILENAME","policy.mock.yaml")).read_text())
        for d in policy['deployments']:
            d['api_base']=cls.mock.base_url
        policy['limits']={'first_event_timeout':1,'stream_idle_timeout':1,'total_timeout':4,'cooldown_seconds':1}
        config=Path(cls.tmp.name)/'policy.yaml';config.write_text(yaml.safe_dump(policy))
        os.environ.update(DATABASE_URL=os.environ['GATEWAY_TEST_DATABASE_URL'],LITELLM_MASTER_KEY=ADMIN,LITELLM_SALT_KEY=SALT,MOCK_UPSTREAM_KEY='synthetic-local-only',GATEWAY_RUNTIME_DIR=cls.tmp.name,LITELLM_LOCAL_MODEL_COST_MAP='True')
        from gateway.bootstrap import build_app
        cls.app=build_app(config,Path(cls.tmp.name)/'state')
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        cls.server=uvicorn.Server(uvicorn.Config(cls.app,host='127.0.0.1',port=port,log_level='critical',access_log=False))
        cls.thread=threading.Thread(target=cls.server.run,daemon=True);cls.thread.start()
        cls.http=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=8,trust_env=False)
        for _ in range(600):
            try:
                response=cls.http.get('/health/readiness')
                if response.status_code==200:
                    break
            except httpx.HTTPError:
                pass
            if not cls.thread.is_alive():
                raise RuntimeError('Native proxy failed to start')
            time.sleep(.1)
        else:
            raise RuntimeError('Native proxy readiness timed out')
        cls.key=cls.create_key('fixture-consumer')

    @classmethod
    def tearDownClass(cls):
        # Disposable test credentials are revoked; no persistent production grants.
        try:
            cls.http.post('/key/delete',headers=cls.admin_headers(),json={'keys':[cls.key]})
        finally:
            cls.http.close();cls.server.should_exit=True;cls.thread.join(10)
            cls.mock_context.__exit__(None,None,None);cls.tmp.cleanup()

    @classmethod
    def admin_headers(cls):
        return {'Authorization':'Bearer '+ADMIN}

    @classmethod
    def create_key(cls,alias,ids=None):
        r=cls.http.post('/key/generate',headers=cls.admin_headers(),json={
            'key_alias':alias,'models':['general-free'],'duration':'1h','max_parallel_requests':2,
            'metadata':{'gateway':{'privacy_scope':'non_sensitive','deployment_ids':ids or ['mock-primary','mock-secondary']}}})
        if r.status_code!=200:
            raise AssertionError(f'Native restricted key generation failed: {r.status_code} {r.text}')
        return r.json()['key']

    def setUp(self):
        for _ in range(300):
            if self.app.active_requests==0 and self.upstream.active_requests==0:
                break
            time.sleep(.01)
        self.upstream.reset()
        # Reset only synthetic fixture observations; no production recovery promise.
        self.app.state.pools.clear();self.app.state.observations.clear();self.app.state.disabled.clear();self.app.state.inflight.clear()
        self.app.state._owners.clear()
        self.app.state.healthy=True
        from litellm.proxy import proxy_server
        proxy_server.llm_router.cache.flush_cache()
        proxy_server.llm_router.cooldown_cache.cooldown_store.flush_cache()

    def invoke(self,**extra):
        return self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+self.key},json={
            'model':'general-free','messages':[{'role':'user','content':'SYNTHETIC_BODY_SENTINEL'}],**extra})

    def test_native_restricted_key_nonstream_and_models(self):
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertIn(r.json()['model'],['general-free','mock-primary']);self.assertEqual(r.headers['x-actual-deployment'],'mock-primary');self.assertEqual(r.json()['usage']['total_tokens'],14);self.assertEqual(r.headers['x-usage-source'],'upstream_observed')
        self.assertTrue(r.headers['x-request-id'])
        r=self.http.get('/v1/models',headers={'Authorization':'Bearer '+self.key})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual([v['id'] for v in r.json()['data']],['general-free'])

    def test_invalid_key_master_and_admin_isolation(self):
        for key in ['sk-invalid',ADMIN]:
            r=self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':'general-free','messages':[{'role':'user','content':'fixture'}]})
            self.assertIn(r.status_code,[401,403])
        r=self.http.post('/key/generate',headers={'Authorization':'Bearer '+self.key},json={})
        self.assertEqual(r.status_code,403)
        for path in ['/responses','/v1/responses','/model/new','/config/update','/ui','/health']:
            self.assertEqual(self.http.get(path).status_code,404)
        self.assertEqual(sum(self.upstream.counts.values()),0)

    def test_body_overrides_unknown_alias_and_unsupported_fields(self):
        for k,v in {'api_base':'https://not-allowed.invalid','api_key':'SYNTHETIC_SECRET_SENTINEL','fallbacks':['paid'],'drop_params':True,'temperature':.5,'tools':[],'metadata':{}}.items():
            r=self.invoke(**{k:v});self.assertEqual(r.status_code,400,(k,r.text))
            # Error response bytes can arrive before the required audit finally
            # releases local admission. This test checks each independent input
            # contract, not deliberate saturation of the separate concurrency cap.
            for _ in range(650):
                if self.app.active_requests==0:break
                time.sleep(.01)
            self.assertEqual(self.app.active_requests,0)
        r=self.invoke(model='real-model-name');self.assertEqual(r.status_code,404)
        self.assertEqual(sum(self.upstream.counts.values()),0)

    def test_native_fallback_and_sanitized_trace(self):
        self.upstream.set_behavior('mock-primary','http_429',retry_after='1')
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['model'],'mock-secondary')
        self.assertEqual(self.upstream.counts['mock-primary'],1);self.assertEqual(self.upstream.counts['mock-secondary'],1)
        logs=self.http.get('/gateway/traces',headers=self.admin_headers()).json()
        serialized=str(logs)
        self.assertNotIn('SYNTHETIC_BODY_SENTINEL',serialized);self.assertNotIn(self.key,serialized);self.assertNotIn(ADMIN,serialized)
        attempts=[x for x in logs['data'] if x.get('request_id')==r.headers['x-request-id'] and x['event']=='attempt_started']
        self.assertEqual(len(attempts),2)

    def test_stream_normal_and_clean_eof_never_faked_success(self):
        r=self.invoke(stream=True);self.assertEqual(r.status_code,200,r.text)
        self.assertIn('data: [DONE]',r.text);self.assertIn('"finish_reason":"stop"',r.text.replace(' ',''))
        self.upstream.reset();self.upstream.set_behavior('mock-primary','eof_after_role')
        r=self.invoke(stream=True)
        self.assertNotIn('data: [DONE]',r.text)
        self.assertNotIn('"finish_reason":"stop"',r.text.replace(' ',''))
        self.assertEqual(self.upstream.counts['mock-secondary'],0)

    def test_native_revoke_rejects_old_key(self):
        k=self.create_key('fixture-rotation')
        h={'Authorization':'Bearer '+k}
        self.assertEqual(self.http.get('/v1/models',headers=h).status_code,200)
        r=self.http.post('/key/delete',headers=self.admin_headers(),json={'keys':[k]})
        self.assertEqual(r.status_code,200,r.text)
        self.assertIn(self.http.get('/v1/models',headers=h).status_code,[401,403])

    def test_restricted_secondary_grant_cannot_expand(self):
        k=self.create_key('fixture-primary-only',['mock-primary'])
        try:
            self.upstream.set_behavior('mock-primary','http_500')
            r=self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+k},json={'model':'general-free','messages':[{'role':'user','content':'fixture'}]})
            self.assertGreaterEqual(r.status_code,400);self.assertEqual(self.upstream.counts['mock-secondary'],0)
        finally:
            self.http.post('/key/delete',headers=self.admin_headers(),json={'keys':[k]})

    def test_proxy_concurrency_limit_is_bounded(self):
        self.assertEqual(self.invoke().status_code,200)  # establish healthy quota pool
        # The response body can arrive before audit finalization releases its slot.
        for _ in range(300):
            if self.app.active_requests==0 and self.upstream.active_requests==0:
                break
            time.sleep(.01)
        self.assertEqual(self.app.active_requests,0)
        self.upstream.reset()
        release=threading.Event()
        self.upstream.set_behavior('mock-primary','ok',release_event=release)
        with ThreadPoolExecutor(max_workers=2) as executor:
            work=[executor.submit(self.invoke) for _ in range(2)]
            try:
                # Synchronize on actual admission rather than an arbitrary 0.5s
                # machine-speed assumption. A completed/failed request still
                # fails every original occupancy/outcome assertion below.
                admission_deadline=time.monotonic()+self.app.policy.limits.total_timeout
                while time.monotonic()<admission_deadline:
                    if self.upstream.active_requests==2:
                        break
                    if any(f.done() for f in work):
                        break
                    time.sleep(.005)
                # Both actual upstream requests are held until after the third check.
                self.assertEqual(self.upstream.active_requests,2)
                self.assertEqual(self.app.active_requests,2)
                self.assertFalse(any(f.done() for f in work))
                r=self.invoke()
                self.assertEqual(r.status_code,429,r.text)
                self.assertEqual(self.upstream.counts['mock-primary'],2)
                self.assertEqual(self.upstream.counts['mock-secondary'],0)
            finally:
                release.set()
            self.assertEqual([f.result().status_code for f in work],[200,200])
        for _ in range(100):
            if self.app.active_requests==0:
                break
            time.sleep(.01)
        self.assertEqual(self.app.active_requests,0)

    def test_proxy_disconnect_releases_slot_and_does_not_retry(self):
        self.upstream.set_behavior('mock-primary','slow_stream',chunk_delay=.7)
        with self.http.stream('POST','/v1/chat/completions',headers={'Authorization':'Bearer '+self.key},json={
                'model':'general-free','messages':[{'role':'user','content':'fixture'}],'stream':True}) as response:
            self.assertEqual(response.status_code,200)
            for line in response.iter_lines():
                if line.startswith('data:'):
                    break
        for _ in range(200):
            if self.app.active_requests==0 and self.upstream.active_requests==0:
                break
            time.sleep(.01)
        self.assertEqual(self.app.active_requests,0)
        self.assertEqual(self.upstream.counts['mock-secondary'],0)
        self.assertEqual(self.app.state.inflight,set())
        self.assertGreaterEqual(self.upstream.cancelled['mock-primary'],1)
        self.assertTrue(self.app.state.healthy)
        self.upstream.set_behavior('mock-primary','ok')
        self.assertEqual(self.invoke().status_code,200)

    def test_upstream_auth_failure_is_502_not_consumer_401(self):
        self.upstream.set_behavior('mock-primary','http_401')
        r=self.invoke();self.assertEqual(r.status_code,502,r.text)
        self.assertEqual(r.json()['error']['code'],'upstream_auth_error')
        self.assertEqual(self.upstream.counts['mock-secondary'],0)

    def test_secret_bearing_errors_do_not_escape_wire_logs_or_database(self):
        sentinel='SYNTHETIC_PRIVATE_ERROR_'+self.key
        self.capture.readouterr()
        for model in ['mock-primary','mock-secondary']:
            self.upstream.set_behavior(model,'http_500',error_message=sentinel)
        r=self.invoke()
        self.assertGreaterEqual(r.status_code,400)
        logs=self.http.get('/gateway/traces',headers=self.admin_headers()).text
        captured=self.capture.readouterr()
        for output in [r.text,logs,captured.out,captured.err]:
            self.assertNotIn(sentinel,output)
            self.assertNotIn('SYNTHETIC_BODY_SENTINEL',output)
            self.assertNotIn(self.key,output)
        with psycopg.connect(os.environ['GATEWAY_TEST_DATABASE_URL']) as con:
            for table in ['LiteLLM_ErrorLogs','LiteLLM_SpendLogs']:
                if con.execute('SELECT to_regclass(%s)', ('"'+table+'"',)).fetchone()[0]:
                    self.assertEqual(con.execute('SELECT count(*) FROM "'+table+'"').fetchone()[0],0)

    def test_database_override_is_detected_before_upstream(self):
        with psycopg.connect(os.environ['GATEWAY_TEST_DATABASE_URL']) as con:
            con.execute('INSERT INTO "LiteLLM_ConfigOverrides"(config_type,config_value,"created_at","updated_at") VALUES (%s,%s::jsonb,now(),now())',('fixture-conflict','{}'))
        try:
            r=self.invoke();self.assertEqual(r.status_code,503,r.text)
            self.assertEqual(sum(self.upstream.counts.values()),0)
        finally:
            with psycopg.connect(os.environ['GATEWAY_TEST_DATABASE_URL']) as con:
                con.execute('DELETE FROM "LiteLLM_ConfigOverrides" WHERE config_type=%s',('fixture-conflict',))

    def test_database_unreachable_rejects_even_cached_valid_key(self):
        self.assertEqual(self.invoke().status_code,200)
        self.upstream.reset()
        dsn=self.app.state.dsn
        self.app.state.dsn='postgresql://gateway_test@127.0.0.1:1/unreachable'
        try:
            r=self.invoke();self.assertEqual(r.status_code,503,r.text)
            self.assertEqual(sum(self.upstream.counts.values()),0)
        finally:
            self.app.state.dsn=dsn

    def test_persisted_pool_cooldown_restores_and_unknown_stays_unknown(self):
        self.upstream.set_behavior('mock-primary','http_429',retry_after='60')
        self.assertEqual(self.invoke().status_code,200)
        from gateway.state import PostgresState
        async def check():
            restored=PostgresState(os.environ['GATEWAY_TEST_DATABASE_URL'])
            await restored.initialize()
            self.assertEqual(restored.pools['mock-primary-requests']['status'],'cooldown')
            self.assertFalse(await restored.check_pools(['mock-primary-requests']))
            self.assertIsNone(restored.pools['mock-primary-requests']['remaining'])
            self.assertNotIn('never-observed',restored.pools)
            self.assertTrue(await restored.check_pools(['never-observed']))
        asyncio.run(check())

    def test_retention_deletes_only_expired_metadata(self):
        with psycopg.connect(os.environ['GATEWAY_TEST_DATABASE_URL']) as con:
            con.execute("INSERT INTO gateway_ext.events(created_at,payload) VALUES (now()-interval '8 days',%s::jsonb)", ('{"event":"expired-fixture"}',))
            con.execute("INSERT INTO gateway_ext.events(payload) VALUES (%s::jsonb)", ('{"event":"recent-fixture"}',))
        asyncio.run(self.app.state.cleanup(7))
        with psycopg.connect(os.environ['GATEWAY_TEST_DATABASE_URL']) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM gateway_ext.events WHERE payload->>'event'='expired-fixture'").fetchone()[0],0)
            self.assertGreaterEqual(con.execute("SELECT count(*) FROM gateway_ext.events WHERE payload->>'event'='recent-fixture'").fetchone()[0],1)

    def test_drain_stops_new_requests_and_reports_revision(self):
        r=self.http.post('/gateway/drain',headers=self.admin_headers(),json={})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(r.json()['draining']);self.assertEqual(r.json()['active_requests'],0)
        self.assertEqual(r.json()['config_revision'],self.app.policy.revision)
        try:
            self.assertEqual(self.invoke().status_code,503)
            self.assertEqual(sum(self.upstream.counts.values()),0)
        finally:
            (self.app.state_dir/'drain.json').unlink(missing_ok=True)

    def test_all_cooling_is_explicit_503_with_zero_new_attempts(self):
        for d in self.app.policy.deployments:
            self.app.state.pools[d.quota_pools[0]]={'status':'cooldown','next_probe_at':time.time()+60,'remaining':None}
        r=self.invoke();self.assertEqual(r.status_code,503,r.text)
        self.assertEqual(r.json()['error']['code'],'no_eligible_free_model')
        self.assertEqual(sum(self.upstream.counts.values()),0)

    def test_runtime_disabled_eligibility_cannot_be_called(self):
        previous=[d.enabled for d in self.app.policy.deployments]
        try:
            for d in self.app.policy.deployments:
                d.enabled=False
            r=self.invoke();self.assertEqual(r.status_code,503,r.text)
            self.assertEqual(sum(self.upstream.counts.values()),0)
        finally:
            for d,enabled in zip(self.app.policy.deployments,previous):
                d.enabled=enabled

    def test_unsupported_fallback_capability_rejects_before_primary(self):
        secondary=self.app.policy.deployments[1]
        previous=secondary.capability.max_output_tokens
        secondary.capability.max_output_tokens=16
        try:
            r=self.invoke(max_completion_tokens=32);self.assertEqual(r.status_code,400,r.text)
            self.assertEqual(sum(self.upstream.counts.values()),0)
        finally:
            secondary.capability.max_output_tokens=previous

    def test_nonstream_refusal_and_length_keep_semantics(self):
        self.upstream.set_behavior('mock-primary','refusal')
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(r.json()['choices'][0]['message'].get('refusal'))
        self.assertEqual(self.upstream.counts['mock-secondary'],0)
        self.upstream.set_behavior('mock-primary','length')
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['choices'][0]['finish_reason'],'length')
        self.assertEqual(self.upstream.counts['mock-secondary'],0)

    def test_observed_quota_persists_and_blocks_only_its_scope(self):
        self.upstream.set_behavior('mock-primary','ok',headers={
            'x-ratelimit-limit-requests':'10','x-ratelimit-remaining-requests':'0',
            'x-ratelimit-reset-requests':'60s','authorization':'SYNTHETIC_HEADER_SECRET'})
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        resources=self.http.get('/gateway/resources',headers=self.admin_headers())
        self.assertEqual(resources.status_code,200,resources.text)
        primary=resources.json()['pools'][0]
        self.assertEqual(primary['observation']['source'],'upstream_observed')
        self.assertEqual(primary['observation']['remaining'],0)
        self.assertEqual(primary['availability'],'known_exhausted')
        self.assertNotIn('SYNTHETIC_HEADER_SECRET',resources.text)
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.headers['x-actual-deployment'],'mock-secondary')
        self.assertEqual(self.upstream.counts['mock-primary'],1)
        from gateway.state import PostgresState
        async def check():
            restored=PostgresState(os.environ['GATEWAY_TEST_DATABASE_URL'])
            restored.pool_configs={p.id:p for p in self.app.policy.pools}
            await restored.initialize()
            self.assertEqual(restored.quota_snapshot('mock-primary-requests').remaining,0)
            self.assertFalse(await restored.check_pools(['mock-primary-requests']))
        asyncio.run(check())

    def test_measured_usage_is_distinct_from_missing_and_stream_unknown(self):
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['usage']['prompt_tokens'],8)
        self.assertEqual(r.json()['usage']['completion_tokens'],6)
        self.assertEqual(r.headers['x-usage-source'],'upstream_observed')
        self.upstream.set_behavior('mock-primary','missing_usage')
        r=self.invoke();self.assertIsNone(r.json().get('usage'))
        self.assertEqual(r.headers['x-usage-source'],'unknown')
        self.upstream.set_behavior('mock-primary','ok')
        r=self.invoke(stream=True);self.assertEqual(r.headers['x-usage-source'],'unknown')

    def test_private_diagnostics_deny_consumers_and_never_probe(self):
        for path in ['/gateway/resources','/gateway/config']:
            self.assertEqual(self.http.get(path,headers={'Authorization':'Bearer '+self.key}).status_code,403)
            r=self.http.get(path,headers=self.admin_headers());self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn('credential_env',r.text);self.assertNotIn('api_base',r.text)
            self.assertNotIn(ADMIN,r.text)
        self.assertEqual(sum(self.upstream.counts.values()),0)

    def test_postgres_client_encoding_is_forced_utf8(self):
        before=os.environ.get('PGCLIENTENCODING')
        os.environ['PGCLIENTENCODING']='SQL_ASCII'
        try:
            async def check():
                async with await self.app.state._connect() as con:
                    result=await con.execute('SHOW client_encoding')
                    self.assertEqual((await result.fetchone())[0],'UTF8')
            asyncio.run(check())
        finally:
            if before is None:os.environ.pop('PGCLIENTENCODING',None)
            else:os.environ['PGCLIENTENCODING']=before

    def test_unaliased_keys_have_distinct_safe_audit_fingerprints(self):
        keys=[self.create_key(None) for _ in range(2)]
        fingerprints=[]
        try:
            for key in keys:
                r=self.http.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':'general-free','messages':[{'role':'user','content':'fixture'}]})
                self.assertEqual(r.status_code,200,r.text)
                trace=self.http.get('/gateway/traces',params={'request_id':r.headers['x-request-id']},headers=self.admin_headers())
                self.assertEqual(trace.status_code,200,trace.text)
                records=trace.json()['data']
                self.assertTrue(records)
                self.assertEqual({v['request_id'] for v in records},{r.headers['x-request-id']})
                fingerprints.append(next(v['key_id'] for v in records if v['event']=='attempt_started'))
                self.assertNotIn(key,trace.text)
            self.assertNotEqual(*fingerprints)
            summary=self.http.get('/gateway/summary',headers=self.admin_headers())
            self.assertEqual(summary.status_code,200,summary.text)
            self.assertGreaterEqual(summary.json()['request_count'],2)
            self.assertIn('denominator',summary.json())
            self.assertIsNone(summary.json()['billing_zero_confirmed'])
        finally:
            self.http.post('/key/delete',headers=self.admin_headers(),json={'keys':keys})

    def test_unknown_reset_zero_is_locally_paused_not_unrestricted(self):
        self.upstream.set_behavior('mock-primary','ok',headers={'x-ratelimit-remaining-requests':'0'})
        self.assertEqual(self.invoke().status_code,200)
        resources=self.http.get('/gateway/resources',headers=self.admin_headers()).json()
        pool=resources['pools'][0]
        self.assertEqual(pool['availability'],'known_exhausted')
        self.assertEqual(pool['next_probe_source'],'local_observation_expiry_policy')
        self.assertIsNone(pool['observation']['reset_at'])
        self.assertFalse(pool['next_probe_is_recovery_guarantee'])
        r=self.invoke();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.headers['x-actual-deployment'],'mock-secondary')
        self.assertEqual(self.upstream.counts['mock-primary'],1)
