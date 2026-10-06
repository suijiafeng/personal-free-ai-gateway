"""Actual native Proxy/PostgreSQL/TCP trace semantics, with no real account traffic."""
import os
from urllib.parse import urlencode
import pytest

from tests.integration.test_proxy_boundaries import native_boundary, wait_idle

pytestmark = [pytest.mark.integration, pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("GATEWAY_TEST_DATABASE_URL"),reason="Disposable PostgreSQL URL not set; not a DB test pass")]


def test_actual_fallback_has_historical_decisions_timings_and_filtered_cursor(native_boundary):
    with native_boundary() as (proxy, upstream, key, revision):
        upstream.set_behavior('mock-primary','http_429',retry_after='1')
        response=proxy.generate(key)
        assert response.status_code==200
        wait_idle(proxy,upstream)
        request_id=response.headers['x-request-id']
        query={'request_id':request_id,'fallback':'true','final_status':'complete','error_code':'rate_limited','limit':2}
        rows=[]
        while True:
            page=proxy.request('GET','/gateway/traces?'+urlencode(query))
            assert page.status_code==200
            data=page.json()
            assert data['contains_content'] is False
            assert data['config_revision']==revision
            rows.extend(data['data'])
            if data['next_cursor'] is None:
                break
            query['before_id']=data['next_cursor']
        assert len({e['event_id'] for e in rows})==len(rows)
        assert {e['request_id'] for e in rows}=={request_id}
        decisions=[e for e in rows if e['event']=='candidate_decision']
        assert {e['deployment_id'] for e in decisions}=={'mock-primary','mock-secondary'}
        assert any(e['decision']=='excluded' and e['deployment_id']=='mock-primary' for e in decisions)
        finished=sorted([e for e in rows if e['event']=='attempt_finished'],key=lambda e:e['attempt'])
        assert len(finished)==2
        assert finished[0]['upstream_status']==429
        assert finished[0]['cooldown_seconds']==1
        assert finished[0]['pool_ids']==['mock-primary-requests']
        assert all(e['duration_ms']>=0 for e in finished)
        assert all(e['first_event_ms'] is None for e in finished)
        started=[e for e in rows if e['event']=='attempt_started' and e['attempt']==2][0]
        assert started['retry_reason']=='rate_limited'
        assert started['actual_model']=='openai/mock-secondary'
        public=proxy.request('GET','/gateway/traces',key)
        assert public.status_code==403
        assert upstream.counts=={'mock-primary':1,'mock-secondary':1}
        serialized=str(rows)
        assert key not in serialized and 'Synthetic recovery fixture' not in serialized


def test_actual_stream_records_delivered_first_event_and_admin_validation(native_boundary):
    with native_boundary() as (proxy,upstream,key,revision):
        response=proxy.request('POST','/v1/chat/completions',key,{
            'model':'general-free','messages':[{'role':'user','content':'SYNTHETIC_PRIVATE_BODY'}],'stream':True})
        assert response.status_code==200
        wait_idle(proxy,upstream)
        request_id=response.headers['x-request-id']
        rows=proxy.request('GET','/gateway/traces?request_id='+request_id).json()['data']
        request=[e for e in rows if e['event']=='request_finished'][0]
        attempt=[e for e in rows if e['event']=='attempt_finished'][0]
        assert request['status']=='complete'
        assert 0<=request['first_event_ms']<=request['duration_ms']
        assert attempt['first_event_ms']==request['first_event_ms']
        assert 0<=attempt['attempt_first_event_ms']<=attempt['duration_ms']
        assert 'SYNTHETIC_PRIVATE_BODY' not in str(rows)
        counts=dict(upstream.counts)
        for query in ['key_id=SECRET','final_status=success','before_id=-2','limit=2&limit=3']:
            assert proxy.request('GET','/gateway/traces?'+query).status_code==400
        assert upstream.counts==counts


def test_native_daily_metadata_ttl_preserves_authentication_history(native_boundary):
    import asyncio
    from datetime import datetime,timedelta,timezone
    import psycopg
    from gateway.state import PostgresState,NATIVE_DAILY_METADATA_TABLES
    with native_boundary() as (proxy,upstream,key,revision):
        dsn=os.environ['GATEWAY_TEST_DATABASE_URL']
        old=(datetime.now(timezone.utc)-timedelta(days=8)).date().isoformat()
        today=datetime.now(timezone.utc).date().isoformat()
        with psycopg.connect(dsn) as con:
            history_before=con.execute('SELECT count(*) FROM "LiteLLM_DeletedVerificationToken"').fetchone()[0]
            for date in (old,today):
                con.execute('INSERT INTO "LiteLLM_DailyGatewayRequests" (date,category,route,created_at,updated_at) VALUES (%s,%s,%s,now(),now()) ON CONFLICT DO NOTHING',
                    (date,'fixture-retention','fixture-retention'))
                con.execute('INSERT INTO "LiteLLM_DailyUserSpend" (id,user_id,date,api_key,created_at,updated_at) VALUES (%s,%s,%s,%s,now(),now()) ON CONFLICT DO NOTHING',
                    ('retention-fixture-'+date,'synthetic-retention',date,'synthetic-fingerprint'))
        asyncio.run(PostgresState(dsn).cleanup(7))
        with psycopg.connect(dsn) as con:
            for table in NATIVE_DAILY_METADATA_TABLES:
                assert con.execute('SELECT count(*) FROM "'+table+'" WHERE date=%s',(old,)).fetchone()[0]==0
            assert con.execute('SELECT count(*) FROM "LiteLLM_DailyGatewayRequests" WHERE date=%s AND category=%s',(today,'fixture-retention')).fetchone()[0]==1
            assert con.execute('SELECT count(*) FROM "LiteLLM_DailyUserSpend" WHERE id=%s',('retention-fixture-'+today,)).fetchone()[0]==1
            assert con.execute('SELECT count(*) FROM "LiteLLM_DeletedVerificationToken"').fetchone()[0]==history_before
            con.execute('DELETE FROM "LiteLLM_DailyGatewayRequests" WHERE category=%s',('fixture-retention',))
            con.execute('DELETE FROM "LiteLLM_DailyUserSpend" WHERE user_id=%s',('synthetic-retention',))
        assert proxy.request('GET','/v1/models',key).status_code==200
        assert upstream.counts=={}
