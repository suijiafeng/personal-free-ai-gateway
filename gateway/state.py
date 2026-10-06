"""Bounded metadata and pool observations. Never store prompts, output, keys or raw errors."""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .errors import GatewayError

LOG = logging.getLogger("gateway.audit")
# Fixed 1.104.0 schema allowlist. Only request/token aggregates are retained for
# seven UTC dates; authentication/revocation tables are deliberately excluded.
NATIVE_DAILY_METADATA_TABLES = (
    "LiteLLM_DailyUserSpend", "LiteLLM_DailyGlobalSpend", "LiteLLM_DailyOrganizationSpend",
    "LiteLLM_DailyEndUserSpend", "LiteLLM_DailyAgentSpend", "LiteLLM_DailyTeamSpend",
    "LiteLLM_DailyTagSpend", "LiteLLM_DailyGuardrailMetrics", "LiteLLM_DailyGuardrailUsageUnits",
    "LiteLLM_DailyPolicyMetrics", "LiteLLM_DailyToolSpend", "LiteLLM_DailyGatewayRequests",
)

EVENT_FIELDS = frozenset({"event", "request_id", "config_revision", "key_id", "alias", "attempt", "deployment_id", "status", "error_code", "duration_ms", "usage_source", "prompt_tokens", "completion_tokens", "total_tokens", "upstream_execution_unknown", "pool_ids", "timestamp", "first_event_ms", "attempt_first_event_ms", "elapsed_ms", "retry_reason", "upstream_status", "cooldown_seconds", "cooldown_source", "decision", "exclusion_reason", "candidate_order", "phase", "actual_model", "provider"})


def safe_event(event):
    if set(event) - EVENT_FIELDS:
        raise ValueError("unknown diagnostic fields")
    return {**event, "timestamp": datetime.now(timezone.utc).isoformat()}


class MemoryState:
    """TEST ONLY. Never used by production bootstrap."""
    def __init__(self):
        self.events = []
        self.event_sequence = 0
        self.pools = {}
        self.observations = {}
        self.pool_configs = {}
        self.observation_max_age_seconds = 300
        self.inflight = set()
        self._owners = {}
        self.disabled = set()
        self.healthy = True
        self.lock = asyncio.Lock()

    async def ready(self):
        if getattr(self,"native_ready_check",None) and not self.native_ready_check():
            raise GatewayError(503,"native_auth_unavailable","Native authentication database is unavailable.")
        if not self.healthy:
            raise GatewayError(503, "state_unavailable", "Policy or diagnostic storage is unavailable.")

    async def record(self, event):
        await self.ready()
        self.event_sequence += 1
        self.events.append({**safe_event(event), "event_id": self.event_sequence})

    def scope_fingerprint(self,pool_id):
        pool=self.pool_configs.get(pool_id)
        if pool is None:
            return None
        identity={k:getattr(pool,k) for k in ("id","provider","account_scope","model_scope","dimension","window","reset_timezone")}
        return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()

    def _pool_unknown(self, pool_id, now):
        value=self.pools.get(pool_id,{})
        if value.get("status") != "available":
            return True
        try:
            observed=datetime.fromisoformat(value["observed_at"]).timestamp()
            return observed>now or now-observed>self.observation_max_age_seconds
        except (ValueError,TypeError,KeyError):
            return True

    def quota_snapshot(self,pool_id,now=None):
        from .quota import snapshot
        return snapshot(self.pool_configs[pool_id],self.observations.get(pool_id),now or datetime.now(timezone.utc))

    async def check_pools(self, ids):
        await self.ready()
        now = time.time()
        for p in ids:
            if p in self.disabled or self.pools.get(p,{}).get("next_probe_at",0)>now:
                return False
            if p in self.pool_configs and p in self.observations:
                observation=self.quota_snapshot(p)
                if observation.known_exhausted:
                    return False
            if self._pool_unknown(p,now) and p in self.inflight:
                return False
        return True

    def _merge_observations(self,observations):
        from .quota import snapshot
        now=datetime.now(timezone.utc)
        merged=[]
        for value in observations:
            pool_id=value["pool_id"]
            if pool_id not in self.pool_configs:
                continue
            observed=snapshot(self.pool_configs[pool_id],value,now)
            current=snapshot(self.pool_configs[pool_id],self.observations.get(pool_id),now)
            # An in-flight response must not erase known exhaustion before its reset.
            if current.known_exhausted and not observed.known_exhausted:
                continue
            if observed.source == "unknown":
                continue
            merged.append(observed.to_dict())
        return merged

    def _exhaustion_state(self,observation):
        if not observation.get("known_exhausted"):
            return None
        probe=observation.get("next_probe_at") or observation.get("expires_at")
        if not probe:
            return None
        reset=datetime.fromisoformat(probe).timestamp()
        existing=self.pools.get(observation["pool_id"],{})
        if existing.get("status")=="disabled" or observation["pool_id"] in self.disabled:
            return {**existing,"status":"disabled","scope_fingerprint":self.scope_fingerprint(observation["pool_id"])}
        return {"status":"known_exhausted","next_probe_at":max(reset,existing.get("next_probe_at",0)),
            "remaining":None,"limit":None,"source":"upstream_observed",
            "observed_at":observation["observed_at"],"scope_fingerprint":self.scope_fingerprint(observation["pool_id"]),
            "next_probe_source":"upstream_reset" if observation.get("next_probe_at") else "local_observation_expiry_policy"}

    async def save_observations(self,observations):
        await self.ready()
        async with self.lock:
            for observation in self._merge_observations(observations):
                self.observations[observation["pool_id"]]=observation
                exhausted=self._exhaustion_state(observation)
                if exhausted:
                    self.pools[observation["pool_id"]]=exhausted

    async def acquire(self, ids):
        async with self.lock:
            if not await self.check_pools(ids):
                return False
            self.inflight.update(ids)
            for p in ids:
                self._owners[p] = self._owners.get(p,0)+1
            return True

    async def release(self, ids, *, status="unknown", cooldown=0):
        async with self.lock:
            for p in ids:
                self._owners[p] = max(0,self._owners.get(p,0)-1)
                if not self._owners[p]:
                    self.inflight.discard(p)
                old = self.pools.get(p,{})
                # An older successful request must not erase a newer shared-pool limit.
                if old.get("status") == "disabled" or (old.get("next_probe_at",0)>time.time() and status not in ("disabled","cooldown")):
                    continue
                next_probe = time.time()+cooldown
                if status == "cooldown":
                    next_probe = max(next_probe,old.get("next_probe_at",0))
                self.pools[p] = {"status": status, "next_probe_at": next_probe,
                    "remaining": None, "limit": None, "source": "upstream_feedback" if status != "unknown" else "unknown",
                    "observed_at": datetime.now(timezone.utc).isoformat(),"scope_fingerprint":self.scope_fingerprint(p)}
                if status == "disabled":
                    self.disabled.add(p)

    async def traces(self, limit=100,request_id=None):
        return (await self.trace_page(limit=limit,request_id=request_id))["data"]

    async def trace_page(self, limit=100, **filters):
        from .trace_query import match_event, page_result
        await self.ready()
        request_ids = None
        predicates = []
        if filters.get("error_code") is not None:
            predicates.append(lambda e: e.get("error_code") == filters["error_code"])
        if filters.get("final_status") is not None:
            predicates.append(lambda e: e.get("event") == "request_finished" and e.get("status") == filters["final_status"])
        if filters.get("fallback") is not None:
            predicates.append(lambda e: e.get("event") == "request_finished" and (e.get("attempt", 0) > 1) == filters["fallback"])
        for predicate in predicates:
            matches = {e.get("request_id") for e in self.events if predicate(e)}
            request_ids = matches if request_ids is None else request_ids & matches
        rows = [e.copy() for e in self.events if match_event(e, filters)
            and (request_ids is None or e.get("request_id") in request_ids)]
        rows.sort(key=lambda row: row["event_id"], reverse=True)
        return page_result(rows, limit, filters)

    async def summary(self,retention_days=7):
        cutoff=datetime.now(timezone.utc).timestamp()-retention_days*86400
        events=[e for e in self.events if datetime.fromisoformat(e["timestamp"]).timestamp()>=cutoff]
        rows=[e for e in events if e["event"]=="request_finished"]
        outcomes={name:sum(e.get("status")==name for e in rows) for name in ("complete","truncated","refused","failed","stream_interrupted","cancelled")}
        return {"window_days":retention_days,"request_count":len(rows),"attempt_count":sum(e["event"]=="attempt_started" for e in events),
            "fallback_request_count":sum(e.get("attempt",0)>1 for e in rows),"outcomes":outcomes,
            "complete_rate":outcomes["complete"]/len(rows) if rows else None,
            "fallback_rate":sum(e.get("attempt",0)>1 for e in rows)/len(rows) if rows else None,
            "denominator":"recorded POST /v1/chat/completions requests, including rejected requests",
            "billing_zero_confirmed":None}

    async def cleanup(self, retention_days):
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        self.events = [e for e in self.events if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff]


class PostgresState(MemoryState):
    """Single-worker cache, persisted observations, and synchronous evidence writes.

    The app rejects multiple workers. Pool ownership isn't a cross-process quota ledger.
    """
    def __init__(self, dsn, expected_runtime_role=None):
        super().__init__()
        self.dsn = dsn
        self.expected_runtime_role = expected_runtime_role
        self.native_ready_check = None

    async def _connect(self):
        import psycopg
        return await psycopg.AsyncConnection.connect(self.dsn, connect_timeout=3, options="-c statement_timeout=3000", client_encoding="UTF8")

    async def initialize(self):
        async with await self._connect() as con:
            cursor = await con.execute('SHOW server_encoding')
            encoding = (await cursor.fetchone())[0]
            if encoding not in ('UTF8', b'UTF8'):
                raise GatewayError(503,'unsupported_database_encoding','The gateway requires a UTF8 PostgreSQL database.')
            for table in ("gateway_ext.events", "gateway_ext.pools", "gateway_ext.observations"):
                found = await con.execute("SELECT to_regclass(%s)", (table,))
                if (await found.fetchone())[0] is None:
                    raise GatewayError(503, "schema_migration_required", "Gateway extension schema must be prepared by the maintenance role.")
            cur = await con.execute('SELECT pool_id, payload FROM gateway_ext.pools')
            self.pools = dict(await cur.fetchall())
            self.pools = {k:v for k,v in self.pools.items() if k not in self.pool_configs or v.get("scope_fingerprint")==self.scope_fingerprint(k)}
            self.disabled = {k for k,v in self.pools.items() if v.get('status') == 'disabled'}
            cur = await con.execute('SELECT pool_id, payload FROM gateway_ext.observations')
            self.observations = dict(await cur.fetchall())
        await self.ready()

    async def ready(self):
        if getattr(self,"native_ready_check",None) and not self.native_ready_check():
            raise GatewayError(503,"native_auth_unavailable","Native authentication database is unavailable.")
        if not self.healthy:
            raise GatewayError(503, "state_unavailable", "Persisted policy state requires recovery.")
        try:
            async with await self._connect() as con:
                await con.execute('SELECT 1')
                if self.expected_runtime_role is not None:
                    role = await con.execute("""SELECT current_user, r.rolsuper, r.rolcreatedb,
                        r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                        has_database_privilege(current_database(), 'CREATE'),
                        has_database_privilege(current_database(), 'TEMP'),
                        EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspname IN ('public','gateway_ext')
                            AND (n.nspowner=r.oid OR has_schema_privilege(n.oid,'CREATE'))),
                        EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid),
                        EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                            WHERE n.nspname IN ('public','gateway_ext') AND c.relowner=r.oid)
                        FROM pg_roles r WHERE r.rolname=current_user""")
                    row = await role.fetchone()
                    if not row or row[0] != self.expected_runtime_role or any(row[1:]):
                        raise GatewayError(503,"unsafe_database_role","Runtime database role must have only the reviewed data privileges.")
                # A pre-existing UI/database configuration must not override file authority.
                for table in ("LiteLLM_Config", "LiteLLM_ProxyModelTable", "LiteLLM_ConfigOverrides"):
                    cur = await con.execute('SELECT to_regclass(%s)', ('"'+table+'"',))
                    if (await cur.fetchone())[0]:
                        if table == "LiteLLM_Config":
                            # Native 1.104.0 writes this non-routing baseline on startup.
                            cur = await con.execute("SELECT EXISTS(SELECT 1 FROM \"LiteLLM_Config\" WHERE param_name <> 'auto_router_tuning_baseline_v3')")
                        else:
                            cur = await con.execute('SELECT EXISTS(SELECT 1 FROM "'+table+'")')
                        if (await cur.fetchone())[0]:
                            raise GatewayError(503, "config_authority_conflict", "Database model/settings overrides require administrator review.")
        except GatewayError:
            raise
        except Exception:
            LOG.error("state_unavailable")
            raise GatewayError(503, "state_unavailable", "Policy or diagnostic storage is unavailable.") from None

    async def record(self, event):
        payload = safe_event(event)
        try:
            async with await self._connect() as con:
                await con.execute('INSERT INTO gateway_ext.events(payload) VALUES (%s::jsonb)', (json.dumps(payload),))
        except Exception:
            LOG.error("audit_write_failed")
            raise GatewayError(503, "audit_unavailable", "Required diagnostic recording is unavailable.") from None

    async def release(self, ids, *, status="unknown", cooldown=0):
        await super().release(ids, status=status, cooldown=cooldown)
        try:
            async with await self._connect() as con:
                for p in ids:
                    await con.execute('INSERT INTO gateway_ext.pools(pool_id,payload) VALUES (%s,%s::jsonb) ON CONFLICT(pool_id) DO UPDATE SET payload=EXCLUDED.payload', (p,json.dumps(self.pools[p])))
        except Exception:
            self.healthy = False
            LOG.error("pool_persistence_failed")
            raise GatewayError(503, "state_unavailable", "Pool observation persistence failed.") from None


    async def save_observations(self,observations):
        await self.ready()
        try:
            async with self.lock:
                merged=self._merge_observations(observations)
                exhaustion={v["pool_id"]:self._exhaustion_state(v) for v in merged if v.get("known_exhausted")}
                async with await self._connect() as con:
                    for observation in merged:
                        await con.execute('INSERT INTO gateway_ext.observations(pool_id,payload) VALUES (%s,%s::jsonb) ON CONFLICT(pool_id) DO UPDATE SET payload=EXCLUDED.payload', (observation["pool_id"],json.dumps(observation)))
                    for pool_id,value in exhaustion.items():
                        if value:
                            await con.execute('INSERT INTO gateway_ext.pools(pool_id,payload) VALUES (%s,%s::jsonb) ON CONFLICT(pool_id) DO UPDATE SET payload=EXCLUDED.payload', (pool_id,json.dumps(value)))
                for observation in merged:
                    self.observations[observation["pool_id"]]=observation
                for pool_id,value in exhaustion.items():
                    if value:
                        self.pools[pool_id]=value
        except Exception:
            self.healthy=False
            LOG.error("quota_observation_persistence_failed")
            raise GatewayError(503,"state_unavailable","Quota observation persistence failed.") from None

    async def traces(self, limit=100,request_id=None):
        return (await self.trace_page(limit=limit,request_id=request_id))["data"]

    async def trace_page(self, limit=100, **filters):
        from .trace_query import page_result
        await self.ready()
        clauses, params = [], []
        for field in ("request_id", "alias", "key_id"):
            if filters.get(field) is not None:
                clauses.append("payload->>'" + field + "'=%s")
                params.append(filters[field])
        if filters.get("before_id") is not None:
            clauses.append("id<%s")
            params.append(filters["before_id"])
        for field, operator in (("since", ">="), ("until", "<")):
            if filters.get(field) is not None:
                clauses.append("created_at" + operator + "%s::timestamptz")
                params.append(filters[field])
        for field in ("error_code", "final_status", "fallback"):
            if filters.get(field) is None:
                continue
            if field == "error_code":
                predicate = "payload->>'error_code'=%s"
            elif field == "final_status":
                predicate = "payload->>'event'='request_finished' AND payload->>'status'=%s"
            else:
                predicate = "payload->>'event'='request_finished' AND (COALESCE((payload->>'attempt')::int,0)>1)=%s"
            clauses.append("payload->>'request_id' IN (SELECT payload->>'request_id' FROM gateway_ext.events WHERE " + predicate + ")")
            params.append(filters[field])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(limit, 100) + 1)
        async with await self._connect() as con:
            cur = await con.execute("SELECT id,payload FROM gateway_ext.events" + where + " ORDER BY id DESC LIMIT %s", tuple(params))
            rows = [{**payload, "event_id": event_id} for event_id, payload in await cur.fetchall()]
        return page_result(rows, limit, filters)

    async def summary(self,retention_days=7):
        async with await self._connect() as con:
            cur=await con.execute("""SELECT payload->>'event',payload->>'status',count(*),
                count(*) FILTER(WHERE (payload->>'attempt')::int>1)
                FROM gateway_ext.events WHERE created_at>=now()-%s*interval '1 day'
                AND payload->>'event' IN ('request_finished','attempt_started')
                GROUP BY payload->>'event',payload->>'status'""",(retention_days,))
            rows=await cur.fetchall()
        outcomes={name:0 for name in ("complete","truncated","refused","failed","stream_interrupted","cancelled")}
        requests=attempts=fallbacks=0
        for event,status,count,fallback in rows:
            if event=='request_finished':
                requests+=count;fallbacks+=fallback
                outcomes[status]=outcomes.get(status,0)+count
            elif event=='attempt_started':
                attempts+=count
        return {"window_days":retention_days,"request_count":requests,"attempt_count":attempts,
            "fallback_request_count":fallbacks,"outcomes":outcomes,
            "complete_rate":outcomes["complete"]/requests if requests else None,
            "fallback_rate":fallbacks/requests if requests else None,
            "denominator":"recorded POST /v1/chat/completions requests, including rejected requests",
            "billing_zero_confirmed":None}

    async def cleanup(self, retention_days):
        try:
            async with await self._connect() as con:
                await con.execute("DELETE FROM gateway_ext.events WHERE created_at < now() - %s * interval '1 day'", (retention_days,))
                # A day-only native aggregate has no exact request timestamp.
                # Keep today plus six preceding UTC dates rather than retaining
                # an extra partial day beyond the seven-day maximum.
                cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days - 1)).date().isoformat()
                for table in NATIVE_DAILY_METADATA_TABLES:
                    exists = await con.execute("SELECT to_regclass(%s)", ('"' + table + '"',))
                    if (await exists.fetchone())[0]:
                        await con.execute('DELETE FROM "' + table + '" WHERE date < %s', (cutoff,))
        except Exception:
            LOG.error("retention_cleanup_failed")
            raise GatewayError(503, "retention_cleanup_failed", "Metadata retention cleanup failed.") from None
