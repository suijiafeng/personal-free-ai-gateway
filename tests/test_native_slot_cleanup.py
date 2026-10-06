import asyncio
import json
import time
from pathlib import Path
import pytest
from gateway.config import load_policy
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState

SCOPE={'type':'http','method':'POST','path':'/v1/chat/completions','query_string':b'',
       'headers':[(b'content-type',b'application/json')]}
BODY=json.dumps({'model':'general-free','messages':[{'role':'user','content':'synthetic'}]}).encode()

async def receive():return {'type':'http.request','body':BODY,'more_body':False}

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['hang','exception'])
async def test_native_cleanup_failure_is_bounded_and_blocks_future_admission(tmp_path,monkeypatch,mode,caplog):
    monkeypatch.setattr('gateway.ingress.NATIVE_SLOT_CLEANUP_TIMEOUT_SECONDS',.03)
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    state=MemoryState();calls=[];sent=[]
    async def native(scope,receive,send):
        calls.append(1);await receive()
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'{}','more_body':False})
    async def cleanup():
        if mode=='hang':await asyncio.Event().wait()
        raise RuntimeError('PRIVATE_CLEANUP_SENTINEL')
    async def send(message):sent.append(message)
    app=GatewayIngress(native,policy,state,tmp_path,'synthetic-admin',native_request_cleanup=cleanup)
    start=time.monotonic();await asyncio.wait_for(app(SCOPE,receive,send),.4)
    assert time.monotonic()-start<.3 and app.active_requests==0
    assert state.healthy is False and state.events == []  # Failed state never fabricates a successful audit row.
    assert 'PRIVATE_CLEANUP_SENTINEL' not in caplog.text
    assert 'native_slot_cleanup_failed' in caplog.text
    sent.clear();await asyncio.wait_for(app(SCOPE,receive,send),.4)
    assert len(calls)==1
    assert next(m['status'] for m in sent if m['type']=='http.response.start')==503

@pytest.mark.asyncio
async def test_input_rejected_before_native_does_not_invoke_native_slot_cleanup(tmp_path):
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    state=MemoryState();native_calls=[];cleanup_calls=[];sent=[]
    async def native(scope,receive,send):native_calls.append(1)
    async def cleanup():cleanup_calls.append(1)
    async def send(message):sent.append(message)
    async def invalid_receive():return {'type':'http.request','body':b'{"model":"general-free","messages":[],"api_key":"SYNTHETIC"}','more_body':False}
    app=GatewayIngress(native,policy,state,tmp_path,'synthetic-admin',native_request_cleanup=cleanup)
    await app(SCOPE,invalid_receive,send)
    assert native_calls==[] and cleanup_calls==[] and app.active_requests==0
    assert next(m['status'] for m in sent if m['type']=='http.response.start')==400
