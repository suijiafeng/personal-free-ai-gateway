"""Runtime retention failure must be visible and fail closed, not a fake zero state."""
import asyncio
import json
from pathlib import Path
import pytest

from gateway.config import load_policy
from gateway.errors import GatewayError
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState


@pytest.mark.asyncio
async def test_background_retention_failure_blocks_consumer_and_readiness(tmp_path):
    class FailingCleanupState(MemoryState):
        cleanup_calls=0
        async def cleanup(self,days):
            self.cleanup_calls+=1
            if self.cleanup_calls>1:
                raise GatewayError(503,'retention_cleanup_failed','Synthetic cleanup unavailable.')
    state=FailingCleanupState()
    calls=[]
    async def native(scope,receive,send):
        if scope['type']=='lifespan':
            await send({'type':'lifespan.startup.complete'})
        else:
            calls.append(scope)
    policy=load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml')
    master='sk-synthetic-retention-admin'
    app=GatewayIngress(native,policy,state,tmp_path,master)
    async def receive():return {'type':'http.request','body':b'', 'more_body':False}
    async def ignored(message):pass
    await app({'type':'lifespan'},receive,ignored)
    # Explicitly await the failed background job so no unhandled-task exception leaks.
    with pytest.raises(GatewayError):
        await app.cleanup_task
    for path,key,expected in [('/gateway/status',master,200),('/health/readiness','',503),('/v1/models','sk-restricted',503)]:
        sent=[]
        async def send(message):sent.append(message)
        await app({'type':'http','method':'GET','path':path,'query_string':b'',
            'headers':[(b'authorization',('Bearer '+key).encode())]},receive,send)
        assert sent[0]['status']==expected
        payload=json.loads(sent[1]['body'])
        if path!='/v1/models':
            assert payload['ready'] is False
            assert payload['database_ready'] is True
            assert payload['retention_ready'] is False
            assert payload['error_code']=='retention_cleanup_failed'
        else:
            assert payload['error']['code']=='retention_cleanup_failed'
    assert calls==[]


@pytest.mark.asyncio
async def test_deeply_nested_input_is_invalid_without_upstream_call(tmp_path):
    calls=[]
    async def native(*args):calls.append(True)
    state=MemoryState()
    app=GatewayIngress(native,load_policy(Path(__file__).parents[1]/'config/policy.mock.yaml'),state,tmp_path,'synthetic')
    sent=[]
    async def receive():return {'type':'http.request','body':b'['*2000+b'0'+b']'*2000,'more_body':False}
    async def send(event):sent.append(event)
    await app({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[(b'content-type',b'application/json')]},receive,send)
    assert sent[0]['status']==400
    assert json.loads(sent[1]['body'])['error']['code']=='invalid_request'
    assert calls==[]
