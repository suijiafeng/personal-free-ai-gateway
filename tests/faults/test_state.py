"""Deterministic observation tests. Fake SQL storage is not a PostgreSQL acceptance pass."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from gateway.errors import GatewayError
from gateway.state import MemoryState, PostgresState, safe_event

async def test_unknown_pool_allows_only_one_probe_until_released():
    state = MemoryState()
    assert await state.acquire(["shared"])
    assert not await state.acquire(["shared"])
    await state.release(["shared"])
    assert await state.acquire(["shared"])
    assert state.pools["shared"]["remaining"] is None
    assert state.pools["shared"]["source"] == "unknown"

async def test_shared_pool_scope_blocks_related_deployment_only():
    state = MemoryState()
    await state.release(["shared"], status="cooldown", cooldown=60)
    assert not await state.check_pools(["shared"])
    assert not await state.check_pools(["independent", "shared"])
    assert await state.check_pools(["independent"])

async def test_cooldown_expiry_permits_probe_but_does_not_invent_balance(monkeypatch):
    now = 1000.0
    monkeypatch.setattr("gateway.state.time.time", lambda: now)
    state = MemoryState()
    await state.release(["shared"], status="cooldown", cooldown=10)
    assert not await state.acquire(["shared"])
    now += 11
    assert await state.acquire(["shared"])
    assert not await state.acquire(["shared"])
    assert state.pools["shared"]["remaining"] is None
    assert state.pools["shared"]["limit"] is None

async def test_disabled_pool_stays_disabled_after_later_success():
    state = MemoryState()
    await state.release(["shared"], status="disabled")
    await state.release(["shared"], status="available")
    assert not await state.check_pools(["shared"])
    assert "shared" in state.disabled

async def test_overlapping_release_cannot_erase_another_inflight_probe():
    state = MemoryState()
    await state.release(["shared"], status="available")
    assert await state.acquire(["shared"])
    assert await state.acquire(["shared"])
    await state.release(["shared"], status="unknown")
    assert not await state.acquire(["shared"])
    await state.release(["shared"], status="unknown")
    assert await state.acquire(["shared"])

async def test_late_success_cannot_erase_live_cooldown():
    state = MemoryState()
    await state.release(["shared"], status="available")
    assert await state.acquire(["shared"])
    assert await state.acquire(["shared"])
    await state.release(["shared"], status="cooldown", cooldown=60)
    await state.release(["shared"], status="available")
    assert not await state.check_pools(["shared"])
    assert state.pools["shared"]["status"] == "cooldown"

async def test_failed_state_fails_closed_for_new_attempt_and_audit():
    state = MemoryState(); state.healthy = False
    with pytest.raises(GatewayError): await state.acquire(["pool"])
    with pytest.raises(GatewayError): await state.record({"event": "request_finished"})

@pytest.mark.parametrize("field", ["messages", "prompt", "completion", "api_key", "authorization", "raw_error"])
def test_raw_content_and_credentials_cannot_enter_diagnostics(field):
    with pytest.raises(ValueError): safe_event({"event": "request_finished", field: "sensitive marker"})

async def test_retention_removes_old_records_without_reclassifying_missing_usage():
    state = MemoryState()
    await state.record({"event": "attempt_finished", "usage_source": "unknown"})
    state.events.append({"event": "attempt_finished", "timestamp": (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()})
    await state.cleanup(7)
    assert len(state.events) == 1
    assert state.events[0]["usage_source"] == "unknown"
    assert "total_tokens" not in state.events[0]

class FakeCursor:
    def __init__(self, rows=()): self.rows = list(rows)
    async def fetchall(self): return copy.deepcopy(self.rows)
    async def fetchone(self): return copy.deepcopy(self.rows[0]) if self.rows else None

class FakeConnection:
    def __init__(self, db): self.db = db
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def execute(self, sql, args=()):
        if sql == "SHOW server_encoding":
            return FakeCursor([(self.db.get("encoding","UTF8"),)])
        if sql.startswith("SELECT pool_id, payload FROM gateway_ext.observations"):
            return FakeCursor(self.db.get("observations",{}).items())
        if sql.startswith("SELECT pool_id, payload"):
            return FakeCursor(self.db["pools"].items())
        if sql.startswith("SELECT to_regclass"):
            return FakeCursor([(args[0] if str(args[0]).startswith("gateway_ext.") else None,)])
        if sql.startswith("INSERT INTO gateway_ext.observations"):
            if self.db.get("fail_writes"): raise RuntimeError("synthetic storage failure")
            self.db.setdefault("observations",{})[args[0]]=json.loads(args[1])
        if sql.startswith("INSERT INTO gateway_ext.pools"):
            if self.db.get("fail_writes"): raise RuntimeError("synthetic storage failure")
            self.db["pools"][args[0]] = json.loads(args[1])
        return FakeCursor([(1,)])

class FakePersistedState(PostgresState):
    def __init__(self, db):
        super().__init__("synthetic-unit-test-only")
        self.fake_db = db
    async def _connect(self): return FakeConnection(self.fake_db)

async def test_persisted_cooldown_restores_on_restart_and_missing_pool_is_unknown():
    db = {"pools": {}}
    original = FakePersistedState(db)
    await original.initialize()
    await original.release(["shared"], status="cooldown", cooldown=3600)
    restarted = FakePersistedState(db)
    await restarted.initialize()
    assert not await restarted.check_pools(["shared"])
    assert restarted.pools["shared"]["remaining"] is None
    assert "missing" not in restarted.pools
    assert await restarted.acquire(["missing"])
    assert not await restarted.acquire(["missing"])

async def test_disabled_pool_restores_after_restart():
    db = {"pools": {}}
    original = FakePersistedState(db)
    await original.release(["disabled"], status="disabled")
    restarted = FakePersistedState(db)
    await restarted.initialize()
    assert "disabled" in restarted.disabled
    assert not await restarted.check_pools(["disabled"])

async def test_persistence_failure_marks_state_unhealthy_and_blocks_new_attempts():
    db = {"pools": {}, "fail_writes": True}
    state = FakePersistedState(db)
    with pytest.raises(GatewayError): await state.release(["shared"], status="cooldown", cooldown=60)
    assert not state.healthy
    with pytest.raises(GatewayError): await state.acquire(["independent"])

async def test_non_utf8_database_is_rejected_before_loading_observations():
    state=FakePersistedState({"pools":{},"encoding":"SQL_ASCII"})
    with pytest.raises(GatewayError,match="unsupported_database_encoding"):
        await state.initialize()

async def test_quota_observation_persists_with_exhaustion_state_atomically():
    from pathlib import Path
    from gateway.config import load_policy
    from gateway.quota import observe_headers,MOCK_REQUESTS_MINUTE_MAPPING
    policy=load_policy(Path(__file__).parents[2]/'config/policy.mock.yaml')
    pool=policy.pools[0]
    original=FakePersistedState({'pools':{},'observations':{}})
    original.pool_configs={pool.id:pool}
    observed=observe_headers(pool,{'x-ratelimit-limit-requests':'10','x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'60s'},datetime.now(timezone.utc),mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    await original.save_observations([observed.to_dict()])
    restored=FakePersistedState(original.fake_db)
    restored.pool_configs=original.pool_configs
    await restored.initialize()
    assert restored.observations[pool.id]['remaining']==0
    assert not await restored.check_pools([pool.id])

async def test_reused_pool_id_with_changed_scope_does_not_inherit_old_account_state():
    from pathlib import Path
    from gateway.config import load_policy
    from gateway.quota import observe_headers,MOCK_REQUESTS_MINUTE_MAPPING
    pool=load_policy(Path(__file__).parents[2]/'config/policy.mock.yaml').pools[0]
    original=FakePersistedState({'pools':{},'observations':{}})
    original.pool_configs={pool.id:pool}
    observed=observe_headers(pool,{'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'60s'},datetime.now(timezone.utc),mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    await original.save_observations([observed.to_dict()])
    restored=FakePersistedState(original.fake_db)
    restored.pool_configs={pool.id:pool.model_copy(update={'account_scope':'different-reviewed-scope'})}
    await restored.initialize()
    assert pool.id not in restored.pools
    assert restored.quota_snapshot(pool.id).remaining is None
    assert await restored.acquire([pool.id])
    assert not await restored.acquire([pool.id])

async def test_late_zero_observation_cannot_erase_persisted_auth_disable():
    from pathlib import Path
    from gateway.config import load_policy
    from gateway.quota import observe_headers,MOCK_REQUESTS_MINUTE_MAPPING
    pool=load_policy(Path(__file__).parents[2]/'config/policy.mock.yaml').pools[0]
    state=FakePersistedState({'pools':{},'observations':{}})
    state.pool_configs={pool.id:pool}
    await state.release([pool.id],status='disabled')
    zero=observe_headers(pool,{'x-ratelimit-remaining-requests':'0','x-ratelimit-reset-requests':'60s'},datetime.now(timezone.utc),mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    await state.save_observations([zero.to_dict()])
    restored=FakePersistedState(state.fake_db)
    restored.pool_configs=state.pool_configs
    await restored.initialize()
    assert pool.id in restored.disabled
    assert restored.pools[pool.id]['status']=='disabled'
    assert not await restored.check_pools([pool.id])
