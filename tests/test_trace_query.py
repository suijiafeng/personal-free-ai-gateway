"""Private, paginated diagnostic metadata contract; no generated content export."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import pytest

from gateway.errors import GatewayError
from gateway.state import MemoryState
from gateway.trace_query import parse_trace_query


@pytest.mark.parametrize("query", [
    "limit=0", "limit=101", "limit=1&limit=2", "before_id=-1", "before_id=9223372036854775808",
    "before_id=0", "unknown=hi", "request_id=no", "since=2026-01-01", "until=", "key_id=secret",
    "key_id="+"a"*64, "final_status=success", "fallback=1", "alias=provider-real-model",
    "error_code=hello%27OR%271", "since=2026-01-02T00:00:00Z&until=2026-01-01T00:00:00Z",
])
def test_trace_query_rejects_ambiguous_unbounded_or_unsafe_filters(query):
    with pytest.raises(GatewayError) as error:
        parse_trace_query(query.encode())
    assert error.value.status == 400
    assert "secret" not in str(error.value)


def test_trace_query_normalizes_aware_time_and_canonical_uuid():
    result=parse_trace_query(urlencode({"request_id":"00000000000000000000000000000001", "limit":4,
        "since":"2026-01-01T08:00:00+08:00", "until":"2026-01-02T00:00:00Z", "fallback":"true"}).encode())
    assert result=={"request_id":"00000000-0000-0000-0000-000000000001", "limit":4,
        "since":"2026-01-01T00:00:00+00:00", "until":"2026-01-02T00:00:00+00:00", "fallback":True}


async def populated_state():
    state=MemoryState()
    for request_id, status, attempts in (("one","complete",2),("two","failed",1)):
        for attempt in range(1,attempts+1):
            await state.record({"event":"attempt_started","request_id":request_id,"alias":"general-free","key_id":"a"*16,"attempt":attempt})
            await state.record({"event":"attempt_finished","request_id":request_id,"alias":"general-free","key_id":"a"*16,"attempt":attempt,
                "error_code":"rate_limited" if request_id=="one" and attempt==1 else None})
        await state.record({"event":"request_finished","request_id":request_id,"alias":"general-free","key_id":"a"*16,"attempt":attempts,"status":status})
    return state


@pytest.mark.asyncio
async def test_cursor_is_stable_when_new_events_arrive_and_never_duplicates():
    state=await populated_state()
    page=await state.trace_page(limit=3)
    first=[e['event_id'] for e in page['data']]
    await state.record({"event":"request_finished","request_id":"new","status":"complete"})
    second=await state.trace_page(limit=100,before_id=page['next_cursor'])
    later=[e['event_id'] for e in second['data']]
    assert first==[8,7,6]
    assert later==[5,4,3,2,1]
    assert len(set(first+later))==8
    assert second['next_cursor'] is None
    assert page['contains_content'] is False


@pytest.mark.asyncio
async def test_request_filters_keep_complete_attempt_timeline():
    state=await populated_state()
    for filters in ({'final_status':'complete'},{'fallback':True},{'error_code':'rate_limited'},
                    {'error_code':'rate_limited','final_status':'complete','fallback':True}):
        page=await state.trace_page(**filters)
        assert len(page['data'])==5
        assert {e['request_id'] for e in page['data']}=={'one'}
    assert (await state.trace_page(final_status='failed',fallback=True))['data']==[]
    assert len((await state.trace_page(fallback=False))['data'])==3


@pytest.mark.asyncio
async def test_event_time_and_identity_filters_have_exclusive_upper_bound():
    state=await populated_state()
    pivot=state.events[4]['timestamp']
    early=await state.trace_page(until=pivot,key_id='a'*16,alias='general-free')
    late=await state.trace_page(since=pivot)
    assert len(early['data'])==4
    assert len(late['data'])==4
    assert (await state.trace_page(key_id='b'*16))['data']==[]


@pytest.mark.asyncio
async def test_cleanup_does_not_reuse_cursor_ids():
    state=MemoryState()
    await state.record({'event':'request_finished'})
    state.events[0]['timestamp']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    await state.cleanup(7)
    await state.record({'event':'request_finished'})
    assert (await state.trace_page())['data'][0]['event_id']==2
