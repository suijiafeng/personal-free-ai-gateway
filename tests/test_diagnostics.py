from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import pytest
from gateway.config import load_policy
from gateway.diagnostics import config_view,resources_view
from gateway.ingress import GatewayIngress
from gateway.quota import observe_headers,MOCK_REQUESTS_MINUTE_MAPPING
from gateway.state import MemoryState


def configured_state():
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    state=MemoryState()
    state.pool_configs={p.id:p for p in policy.pools}
    return policy,state


@pytest.mark.asyncio
async def test_resources_refresh_is_readonly_scoped_and_redacted():
    policy,state=configured_state()
    view=await resources_view(policy,state)
    assert view['refresh_causes_inference'] is False
    assert all(x['observation']['source']=='unknown' for x in view['pools'])
    assert all(x['observation']['remaining'] is None for x in view['pools'])
    assert state.events==[] and state.pools=={} and state.observations=={}
    assert view['account_billing_zero_confirmed'] is None
    serialized=json.dumps(config_view(policy))
    assert 'api_base' not in serialized and 'credential_env' not in serialized
    assert 'MOCK_UPSTREAM_KEY' not in serialized
    assert view['preview_is_execution_guarantee'] is False


@pytest.mark.asyncio
async def test_known_exhaustion_is_retained_until_reset_then_single_probe(monkeypatch):
    policy,state=configured_state()
    now=datetime.now(timezone.utc)
    observation=observe_headers(policy.pools[0],{
        'x-ratelimit-limit-requests':'10','x-ratelimit-remaining-requests':'0',
        'x-ratelimit-reset-requests':'60s'},now,mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    await state.save_observations([observation.to_dict()])
    assert not await state.check_pools([policy.pools[0].id])
    assert await state.check_pools([policy.pools[1].id])
    view=await resources_view(policy,state)
    assert view['pools'][0]['availability']=='known_exhausted'
    assert view['pools'][0]['observation']['remaining']==0
    # A late successful release must not erase the authoritative exhaustion.
    await state.release([policy.pools[0].id],status='available')
    assert not await state.check_pools([policy.pools[0].id])
    positive=observe_headers(policy.pools[0],{'x-ratelimit-remaining-requests':'8'},now+timedelta(seconds=1),mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    await state.save_observations([positive.to_dict()])
    assert state.observations[policy.pools[0].id]['remaining']==0


@pytest.mark.asyncio
async def test_expired_availability_only_allows_one_real_probe(monkeypatch):
    policy,state=configured_state()
    await state.release(['shared'],status='available')
    state.pools['shared']['observed_at']=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    assert await state.acquire(['shared'])
    assert not await state.acquire(['shared'])
    await state.release(['shared'],status='unknown')
    assert await state.acquire(['shared'])


@pytest.mark.asyncio
async def test_private_views_require_admin_and_never_delegate_to_native(tmp_path):
    policy,state=configured_state()
    calls=[]
    async def native(*args):calls.append(True)
    master='sk-admin-test-only-private-diagnostics'
    app=GatewayIngress(native,policy,state,tmp_path,master)
    for path in ['/gateway/resources','/gateway/config']:
        for key,expected in [('sk-consumer-fixture',403),(master,200)]:
            sent=[]
            async def receive():return {'type':'http.request','body':b'','more_body':False}
            async def send(message):sent.append(message)
            await app({'type':'http','method':'GET','path':path,'headers':[(b'authorization',('Bearer '+key).encode())]},receive,send)
            assert sent[0]['status']==expected
    assert calls==[]

@pytest.mark.asyncio
async def test_zero_without_reset_uses_local_expiry_not_fake_provider_reset():
    policy,state=configured_state()
    now=datetime.now(timezone.utc)
    observed=observe_headers(policy.pools[0],{'x-ratelimit-remaining-requests':'0'},now,mapping=MOCK_REQUESTS_MINUTE_MAPPING,ttl=timedelta(seconds=60))
    await state.save_observations([observed.to_dict()])
    assert not await state.check_pools([policy.pools[0].id])
    view=await resources_view(policy,state)
    pool=view['pools'][0]
    assert pool['availability']=='known_exhausted'
    assert pool['next_probe_source']=='local_observation_expiry_policy'
    assert pool['next_probe_is_recovery_guarantee'] is False
    assert pool['observation']['reset_at'] is None
    assert pool['observation']['next_probe_at'] is None


@pytest.mark.asyncio
async def test_summary_has_explicit_denominator_and_no_fake_empty_percentage():
    _,state=configured_state()
    assert (await state.summary())['complete_rate'] is None
    await state.record({'event':'attempt_started','request_id':'one','attempt':1})
    await state.record({'event':'attempt_started','request_id':'one','attempt':2})
    await state.record({'event':'request_finished','request_id':'one','status':'complete','attempt':2})
    await state.record({'event':'request_finished','request_id':'two','status':'failed','attempt':0})
    summary=await state.summary()
    assert summary['request_count']==2 and summary['attempt_count']==2
    assert summary['fallback_request_count']==1 and summary['complete_rate']==.5
    assert summary['billing_zero_confirmed'] is None
    assert len(await state.traces(request_id='one'))==3

@pytest.mark.asyncio
async def test_duplicate_authorization_cannot_change_audit_identity(tmp_path):
    policy,state=configured_state()
    calls=[]
    async def native(*args):calls.append(True)
    app=GatewayIngress(native,policy,state,tmp_path,'sk-admin-placeholder')
    sent=[]
    async def receive():return {'type':'http.request','body':b'{}','more_body':False}
    async def send(message):sent.append(message)
    await app({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[
        (b'authorization',b'Bearer first-consumer'),(b'Authorization',b'Bearer second-consumer'),
        (b'content-type',b'application/json')]},receive,send)
    assert sent[0]['status']==400
    assert calls==[]


@pytest.mark.asyncio
async def test_private_summary_accepts_only_bounded_retention_window(tmp_path):
    policy,state=configured_state()
    calls=[]
    async def native(*args):calls.append(True)
    app=GatewayIngress(native,policy,state,tmp_path,'sk-fixture-admin')
    for query,expected in [(b'days=1',200),(b'days=8',400),(b'days=1&days=2',400),(b'other=1',400)]:
        sent=[]
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        async def send(event):sent.append(event)
        await app({'type':'http','method':'GET','path':'/gateway/summary','query_string':query,
            'headers':[(b'authorization',b'Bearer sk-fixture-admin')]},receive,send)
        assert sent[0]['status']==expected
        if expected==200:
            value=json.loads(sent[1]['body'])
            assert value['window_days']==1 and value['fallback_rate'] is None
    assert calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('wait,expected',[(5,True),(None,False),(-1,False)])
async def test_only_validated_known_wait_is_forwarded_as_retry_after(tmp_path,wait,expected):
    from gateway.context import current_request
    policy,state=configured_state()
    async def native(scope,receive,send):
        await receive()
        ctx=current_request.get()
        ctx.retry_after_until=time.monotonic()+wait if wait is not None else None
        await send({'type':'http.response.start','status':429,'headers':[(b'retry-after',b'UNTRUSTED_RAW') ]})
        await send({'type':'http.response.body','body':b'UNTRUSTED_RAW'})
    app=GatewayIngress(native,policy,state,tmp_path,'admin')
    sent=[]
    async def receive():return {'type':'http.request','body':json.dumps({'model':'general-free','messages':[{'role':'user','content':'fixture'}]}).encode(),'more_body':False}
    async def send(event):sent.append(event)
    await app({'type':'http','method':'POST','path':'/v1/chat/completions','query_string':b'',
        'headers':[(b'content-type',b'application/json')]},receive,send)
    assert sent[0]['status']==429
    header=dict(sent[0]['headers']).get(b'retry-after')
    assert (header is not None)==expected
    if expected:assert 1<=int(header)<=wait
    assert b'UNTRUSTED_RAW' not in b''.join(event.get('body',b'') for event in sent)
