"""Native Router/guard/SDK plus TCP fixture; no provider keys or parser doubles."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import time

os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
import litellm
from litellm import Router
import pytest

from gateway.config import load_policy
from gateway.context import RequestContext, current_request
from gateway.contract import validate_request
from gateway.hooks import guard
from gateway.state import MemoryState
from gateway.stream_bridge import bridge
from tests.mock_upstream import MockState, serve_mock

ROOT=Path(__file__).resolve().parents[2]
OBSERVED={'prompt_tokens':8,'completion_tokens':6,'total_tokens':14}

@pytest.fixture
async def sdk_router(monkeypatch):
    upstream=MockState(capture_payloads=True)
    with serve_mock(upstream) as server:
        policy=load_policy(ROOT/'config/policy.sdk.mock.yaml')
        for d in policy.deployments:
            d.api_base=server.base_url
        policy.limits.first_event_timeout=5
        policy.limits.stream_idle_timeout=2
        policy.limits.total_timeout=10
        state=MemoryState()
        guard.configure(policy,state)
        monkeypatch.setattr(litellm,'callbacks',[guard])
        monkeypatch.setattr(litellm,'custom_provider_map',[{'provider':'gateway_openai_sdk','custom_handler':bridge}])
        monkeypatch.setattr(litellm,'telemetry',False,raising=False)
        monkeypatch.setattr(litellm,'suppress_debug_info',True)
        monkeypatch.setattr(litellm,'turn_off_message_logging',True)
        cfg=policy.native_config()
        for d in cfg['model_list']:
            d['litellm_params']['api_key']='synthetic-only'
        router=Router(model_list=cfg['model_list'],**cfg['router_settings'])
        yield policy,state,router,upstream
        router.discard();router.reset()
        await asyncio.sleep(.04)

async def invoke(fixture,*,stream=True,allowed=None,chunks=None,native_options=None):
    policy,state,router,upstream=fixture
    body=validate_request({'model':'general-free','messages':[{'role':'user','content':'SYNTHETIC_BODY_SENTINEL'}],
                           'stream':stream,'max_completion_tokens':32},policy)
    ctx=RequestContext(body=body,revision=policy.revision,
        allowed_deployments=frozenset(allowed or ['mock-primary','mock-secondary']),privacy_scope='non_sensitive',streaming=stream)
    token=current_request.set(ctx)
    chunks=[] if chunks is None else chunks
    error=None; response=None
    try:
        try:
            response=await router.acompletion(**body,**(native_options or {}))
            if stream:
                async for c in guard.async_post_call_streaming_iterator_hook(None,response,{}):
                    chunks.append(c)
        except Exception as e:
            error=e
        return ctx,chunks,response,error
    finally:
        current_request.reset(token)

@pytest.mark.asyncio
@pytest.mark.parametrize('behavior,expected,counts',[
 ('ok','complete',(1,0)), ('refusal','refused',(1,0)), ('refusal_after_content','refused',(1,0)),
 ('refusal_with_content','refused',(1,0)), ('length','truncated',(1,0)),('content_filter','refused',(1,0)),
 ('missing_usage','complete',(1,0)),('zero_usage','complete',(1,0)),('partial_usage','complete',(1,0)),('null_usage','complete',(1,0)),
 ('disconnect_before_role','complete',(1,1)),('disconnect_after_role','stream_interrupted',(1,0)),
 ('disconnect_after_content','stream_interrupted',(1,0)),('eof_after_role','stream_interrupted',(1,0)),
 ('eof_after_content','stream_interrupted',(1,0)),('unexpected_finish','stream_interrupted',(1,0)),
 ('http_429','complete',(1,1)),('http_500','complete',(1,1)),
])
async def test_stream_bridge_contract(sdk_router,behavior,expected,counts):
    policy,state,router,upstream=sdk_router
    upstream.set_behavior('mock-primary',behavior)
    ctx,chunks,_,error=await invoke(sdk_router)
    assert (error is None)==(expected!='stream_interrupted')
    assert ctx.terminal==expected
    assert (upstream.counts['mock-primary'],upstream.counts['mock-secondary'])==counts
    assert state.inflight==set()
    assert all(r.get('max_tokens')==32 and 'max_completion_tokens' not in r for r in upstream.requests)
    assert all(r.get('stream_options')=={'include_usage':True} for r in upstream.requests)
    if expected=='refused' and behavior.startswith('refusal'):
        assert ''.join(getattr(ch.delta,'refusal',None) or '' for c in chunks for ch in c.choices)=='Synthetic policy refusal.'
    if expected=='stream_interrupted':
        assert not any(ch.finish_reason for c in chunks for ch in c.choices)
    elif behavior in {'missing_usage','partial_usage','null_usage'}:
        assert ctx.usage_source=='unknown' and ctx.observed_usage is None
    else:
        assert ctx.usage_source=='upstream_observed'
        assert ctx.observed_usage==(dict.fromkeys(OBSERVED,0) if behavior=='zero_usage' else OBSERVED)
    for sentinel in ('SYNTHETIC_BODY_SENTINEL','Synthetic policy refusal.','synthetic-only'):
        assert sentinel not in str(state.events)

@pytest.mark.asyncio
@pytest.mark.parametrize('behavior,status', [('ok','complete'),('missing_usage','complete'),('refusal','refused'),('length','truncated')])
async def test_nonstream_bridge_contract(sdk_router,behavior,status):
    policy,state,router,upstream=sdk_router
    upstream.set_behavior('mock-primary',behavior,headers={'x-ratelimit-remaining-requests':'2','authorization':'not-retained'})
    ctx,_,response,error=await invoke(sdk_router,stream=False)
    assert error is None
    assert ctx.terminal==status and upstream.counts['mock-primary']==1 and upstream.counts['mock-secondary']==0
    assert ctx.observed_headers=={'x-ratelimit-remaining-requests':'2'}
    assert state.inflight==set()
    if behavior=='missing_usage':
        assert ctx.usage_source=='unknown' and getattr(response,'usage',None) is None
    else:
        assert ctx.usage_source=='upstream_observed' and response.usage.total_tokens==14
    if behavior=='refusal':
        assert response.choices[0].message.refusal=='Synthetic policy refusal.'

@pytest.mark.asyncio
async def test_sdk_bridge_cancellation_closes_upstream(sdk_router):
    policy,state,router,upstream=sdk_router
    upstream.set_behavior('mock-primary','slow_stream',chunk_delay=2)
    chunks=[]
    task=asyncio.create_task(invoke(sdk_router,chunks=chunks))
    deadline=time.monotonic()+2
    while not chunks and not task.done() and time.monotonic()<deadline:
        await asyncio.sleep(.005)
    assert chunks
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline=time.monotonic()+1
    while upstream.active_requests and time.monotonic()<deadline:
        await asyncio.sleep(.01)
    assert upstream.active_requests==0 and upstream.cancelled['mock-primary']==1
    assert state.inflight==set() and upstream.counts['mock-secondary']==0

@pytest.mark.asyncio
async def test_sdk_bridge_guarded_secondary_cannot_expand(sdk_router):
    policy,state,router,upstream=sdk_router
    upstream.set_behavior('mock-primary','http_500')
    ctx,_,_,error=await invoke(sdk_router,allowed=['mock-primary'])
    assert error is not None
    assert upstream.counts['mock-primary']==1 and upstream.counts['mock-secondary']==0
    assert state.inflight==set()

@pytest.mark.asyncio
async def test_sdk_bridge_direct_unguarded_call_is_blocked(sdk_router):
    policy,state,router,upstream=sdk_router
    with pytest.raises(Exception):
        stream=await litellm.acompletion(model='gateway_openai_sdk/openai/mock-primary',api_base=policy.deployments[0].api_base,
            api_key='synthetic-only',messages=[{'role':'user','content':'synthetic'}],stream=True)
        async for _ in stream:
            pass
    assert sum(upstream.counts.values())==0

@pytest.mark.asyncio
@pytest.mark.parametrize('malformation',['late_content','tool_calls','invalid_content','invalid_role','reasoning'])
async def test_sdk_typed_schema_rejects_unsupported_without_fallback(sdk_router,monkeypatch,malformation):
    from copy import deepcopy
    from tests import mock_upstream
    events=deepcopy(mock_upstream.STREAM_EVENTS)
    if malformation=='late_content':
        events.insert(-1,deepcopy(events[2]))
    else:
        delta=events[2]['choices'][0]['delta']
        if malformation=='tool_calls':delta['tool_calls']=[{'index':0,'id':'synthetic','type':'function','function':{'name':'f','arguments':'{}'}}]
        if malformation=='invalid_content':delta['content']={'not':'text'}
        if malformation=='invalid_role':delta['role']='system'
        if malformation=='reasoning':delta['reasoning_content']='unsupported reasoning field'
    monkeypatch.setattr(mock_upstream,'STREAM_EVENTS',events)
    ctx,chunks,_,error=await invoke(sdk_router)
    assert error is not None and ctx.terminal=='stream_interrupted'
    assert not any(ch.finish_reason for c in chunks for ch in c.choices)
    assert sdk_router[3].counts['mock-primary']==1 and sdk_router[3].counts['mock-secondary']==0
    assert sdk_router[1].inflight==set()

@pytest.mark.asyncio
async def test_sdk_two_provider_identities_share_no_implicit_fallback_authority(sdk_router):
    policy,state,router,upstream=sdk_router
    router.discard();router.reset()
    for d,provider in zip(policy.deployments,('groq','gemini')):
        d.provider=provider
        d.model=provider+'/'+d.id
    guard.configure(policy,state)
    cfg=policy.native_config()
    for d in cfg['model_list']:d['litellm_params']['api_key']='synthetic-only'
    second=Router(model_list=cfg['model_list'],**cfg['router_settings'])
    try:
        upstream.set_behavior('mock-primary','http_429')
        upstream.set_behavior('mock-secondary','refusal_after_content')
        ctx,chunks,_,error=await invoke((policy,state,second,upstream))
        assert error is None and ctx.terminal=='refused'
        assert ctx.attempted_ids=={'mock-primary','mock-secondary'}
        assert [v['model'] for v in upstream.requests]==['mock-primary','mock-secondary']
        assert ''.join(getattr(ch.delta,'refusal',None) or '' for c in chunks for ch in c.choices)=='Synthetic policy refusal.'
    finally:
        second.discard();second.reset()

@pytest.mark.asyncio
@pytest.mark.parametrize("options,allowed", [({"include_usage":True},True),({"include_usage":False},False),({"include_usage":True,"other":True},False)])
async def test_sdk_native_proxy_usage_option_is_exact(sdk_router,options,allowed):
    ctx,chunks,_,error=await invoke(sdk_router,native_options={"stream_options":options})
    assert (error is None) is allowed
    assert sum(sdk_router[3].counts.values())==(1 if allowed else 0)

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("audio",{"id":"synthetic","expires_at":1700000000,"data":"synthetic","transcript":"synthetic"}),
    ("annotations",[{"type":"url_citation","url_citation":{"start_index":0,"end_index":1,"title":"synthetic","url":"https://example.invalid"}}])])
async def test_sdk_nonstream_declared_nontext_fields_are_rejected(sdk_router,monkeypatch,field,value):
    from copy import deepcopy
    from tests import mock_upstream
    completion=deepcopy(mock_upstream.COMPLETION)
    completion["choices"][0]["message"][field]=value
    monkeypatch.setattr(mock_upstream,"COMPLETION",completion)
    ctx,_,response,error=await invoke(sdk_router,stream=False)
    assert error is not None and ctx.terminal!="complete"
    assert sdk_router[3].counts["mock-primary"]==1
    assert sdk_router[3].counts["mock-secondary"]<=1
    assert sdk_router[1].inflight==set()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad_count',[True,-1,'8',8.0])
@pytest.mark.parametrize('stream',[True,False])
async def test_sdk_direct_usage_types_are_not_coerced_into_measurements(sdk_router,monkeypatch,bad_count,stream):
    from copy import deepcopy
    from tests import mock_upstream
    events=deepcopy(mock_upstream.STREAM_EVENTS)
    events[-1]['usage']['prompt_tokens']=bad_count
    completion=deepcopy(mock_upstream.COMPLETION)
    completion['usage']['prompt_tokens']=bad_count
    monkeypatch.setattr(mock_upstream,'STREAM_EVENTS',events)
    monkeypatch.setattr(mock_upstream,'COMPLETION',completion)
    ctx,_,_,error=await invoke(sdk_router,stream=stream)
    assert error is None and ctx.terminal=='complete'
    assert ctx.observed_usage is None and ctx.usage_source=='unknown'

@pytest.mark.asyncio
async def test_sdk_conflicting_usage_after_finish_is_not_success(sdk_router,monkeypatch):
    from copy import deepcopy
    from tests import mock_upstream
    events=deepcopy(mock_upstream.STREAM_EVENTS)
    conflict=deepcopy(events[-1]);conflict['usage']['total_tokens']=99
    events.append(conflict)
    monkeypatch.setattr(mock_upstream,'STREAM_EVENTS',events)
    ctx,chunks,_,error=await invoke(sdk_router)
    assert error is not None and ctx.terminal=='stream_interrupted'
    assert not any(ch.finish_reason for c in chunks for ch in c.choices)
    assert sdk_router[3].counts['mock-primary']==1 and sdk_router[3].counts['mock-secondary']==0
