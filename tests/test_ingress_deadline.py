"""Slow-upload regression: total deadline includes request-body reception."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from gateway.config import load_policy
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState


@pytest.mark.asyncio
async def test_slow_body_is_bounded_by_whole_request_deadline(tmp_path):
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    policy.limits.total_timeout=.04
    policy.limits.first_event_timeout=.04
    reached=False
    async def native(scope,receive,send):
        nonlocal reached
        reached=True
    app=GatewayIngress(native,policy,MemoryState(),tmp_path,'sk-administrator-test-only-placeholder')
    chunk=b'{"model":"general-free","messages":[{"role":"user","content":"fixture"}]}'
    n=0
    async def receive():
        nonlocal n
        await asyncio.sleep(.025)
        n+=1
        return {'type':'http.request','body':chunk if n==1 else b' ','more_body':n<5}
    output=[]
    async def send(message):output.append(message)
    started=time.monotonic()
    await app({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[(b'content-type',b'application/json')],'query_string':b''},receive,send)
    assert time.monotonic()-started<.12
    assert output[0]['status']==504
    assert not reached
    assert app.active_requests==0


@pytest.mark.asyncio
@pytest.mark.parametrize('native_status',[200,503])
async def test_monotonic_deadline_wins_if_native_delays_timer_delivery(tmp_path,native_status):
    """A synchronous native section must not race a pending asyncio timer."""
    from gateway.context import current_request
    from gateway.ingress import GatewayIngress
    from gateway.state import MemoryState
    import json
    from pathlib import Path
    from gateway.config import load_policy
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    state=MemoryState()
    async def native(scope,receive,send):
        await receive()
        ctx=current_request.get()
        ctx.started-=policy.limits.total_timeout+1
        ctx.attempts=1
        await send({'type':'http.response.start','status':native_status,'headers':[]})
        await send({'type':'http.response.body','body':b'not-forwarded'})
    app=GatewayIngress(native,policy,state,tmp_path,'synthetic-admin')
    sent=[]
    async def receive():return {'type':'http.request','body':json.dumps({'model':'general-free','messages':[{'role':'user','content':'fixture'}]}).encode(),'more_body':False}
    async def send(value):sent.append(value)
    await app({'type':'http','method':'POST','path':'/v1/chat/completions','query_string':b'','headers':[(b'content-type',b'application/json')]},receive,send)
    assert sent[0]['status']==504
    assert json.loads(sent[1]['body'])['error']['code']=='deadline_exceeded'
    assert b'not-forwarded' not in b''.join(v.get('body',b'') for v in sent)
    assert app.active_requests==0
