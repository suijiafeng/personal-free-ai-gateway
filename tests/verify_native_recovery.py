"""Disposable native-process recovery and policy rollback rehearsal.

Called only by run_postgres_suite.py after its tests. Uses actual unchanged native
LiteLLM processes and an actual PostgreSQL fixture, with local synthetic upstreams.
Not a production maintenance command or a Docker/Compose/Mac acceptance claim.
Ephemeral native test keys stay in memory; raw HTTP/subprocess output is suppressed.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import yaml

from gateway.config import load_policy
from tests.mock_upstream import MockState, serve_mock
from tests.verify_backup_restore import (
    RecoveryVerificationError, RESTORE_DATABASE, SOURCE_DATABASE,
    _assert_fixture, _connect, _source_port, verify_backup_restore,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))
from revocations import locked as journal_locked, key_hash
from recovery_guards import migration_summary, RecoveryGuardError

ADMIN = "sk-disposable-recovery-administrator-not-real"
SALT = "synthetic-recovery-salt-not-a-production-secret"


def require(condition: bool, stage: str):
    if not condition:
        raise RecoveryVerificationError(f"Native recovery rehearsal failed: {stage}; details suppressed.")


def restored_dsn(source_dsn: str) -> str:
    _source_port(source_dsn)
    parsed = urlsplit(source_dsn)
    return urlunsplit(parsed._replace(path="/" + RESTORE_DATABASE))


class NativeProcess:
    """Own only this temporary native proxy and fixed loopback HTTP client."""
    def __init__(self, policy: Path, dsn: str, root: Path, state: Path, *, port: int | None = None):
        self.policy, self.dsn, self.root, self.state = policy, dsn, root, state
        self.port = port
        self.process = None
        self.client = None

    def __enter__(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port or 0))
            port = listener.getsockname()[1]
        env = dict(os.environ)
        # No inherited provider credentials, user proxy routes or Prisma DB override
        # are needed by this synthetic fixture. Its subprocess has an explicit DSN.
        for name in list(env):
            if name.endswith("API_KEY") or name in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "DIRECT_URL", "SHADOW_DATABASE_URL", "CONFIG_FILE_PATH", "WEB_CONCURRENCY", "UVICORN_WORKERS",
            ):
                env.pop(name, None)
        env.update(DATABASE_URL=self.dsn, LITELLM_MASTER_KEY=ADMIN,
                   LITELLM_SALT_KEY=SALT, MOCK_UPSTREAM_KEY="synthetic-local-only",
                   GATEWAY_RUNTIME_DIR=str(self.root), GATEWAY_STATE_DIR=str(self.state),
                   LITELLM_LOCAL_MODEL_COST_MAP="True", LITELLM_TELEMETRY="False",
                   PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "gateway.bootstrap", "--config", str(self.policy),
             "--profile", "mock", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False)
        try:
            self.await_status(load_policy(self.policy).revision, draining=(self.state / "drain.json").exists())
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise RecoveryVerificationError("Fixture proxy did not stop gracefully; no successful rehearsal recorded.") from None

    def request(self, method: str, path: str, key: str = ADMIN, payload=None):
        try:
            return self.client.request(method, path, headers={"Authorization": "Bearer " + key}, json=payload)
        except httpx.HTTPError:
            raise RecoveryVerificationError("Fixture HTTP request failed; URL, credentials and response suppressed.") from None

    def await_status(self, revision: str, *, draining: bool, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "native proxy startup/liveness")
            try:
                r = self.client.get("/gateway/status", headers={"Authorization": "Bearer " + ADMIN})
                value = r.json()
                if (r.status_code == 200 and value.get("config_revision") == revision
                        and value.get("database_ready") is True and value.get("draining") is draining
                        and value.get("active_requests") == 0
                        and value.get("ready") is (not draining)):
                    return value
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.1)
        raise RecoveryVerificationError("Native expected revision/readiness was not observed; no success recorded.")

    def create_key(self, alias: str):
        r = self.request("POST", "/key/generate", payload={
            "key_alias": alias, "models": ["general-free"], "duration": "1h", "max_parallel_requests": 2,
            "metadata": {"gateway": {"privacy_scope": "non_sensitive", "deployment_ids": ["mock-primary", "mock-secondary"]}},
        })
        require(r.status_code == 200, "ephemeral restricted key creation")
        key = r.json().get("key")
        require(isinstance(key, str) and key.startswith("sk-"), "native key response")
        return key

    def revoke(self, key: str):
        require(self.request("POST", "/key/delete", payload={"keys": [key]}).status_code == 200, "key revocation")

    def auth(self, key: str, accepted: bool):
        code = self.request("GET", "/v1/models", key).status_code
        require(code == 200 if accepted else code in (401, 403), "native restricted key authentication")

    def generate(self, key: str):
        return self.request("POST", "/v1/chat/completions", key, {
            "model": "general-free", "messages": [{"role": "user", "content": "Synthetic recovery fixture"}],
        })

    def drain(self):
        r = self.request("POST", "/gateway/drain", payload={})
        require(r.status_code == 200, "drain request")
        self.await_status(load_policy(self.policy).revision, draining=True)

    def resume_fixture(self):
        # This is the runner's own private test state, never a production resume.
        (self.state / "drain.json").unlink()
        self.await_status(load_policy(self.policy).revision, draining=False)


def native_journal_request(proxy):
    def request(path, method="GET", payload=None):
        response = proxy.request(method, path, payload=payload)
        if path.startswith("/key/info?key=") and response.status_code == 404:
            return {"info": {"status": "absent"}}
        require(response.status_code == 200, "native journal API request")
        return response.json()
    return request


def verify_native_recovery(source_dsn, postgres_bin, temp_dir, *, stop_database, start_database):
    """Rehearse A→B→A, key rotation, real DB outage/restart, snapshot auth recovery."""
    port = _source_port(source_dsn)
    temp_dir = Path(temp_dir).resolve(strict=True)
    with _connect(port, SOURCE_DATABASE, temp_dir) as connection:
        _assert_fixture(connection, temp_dir, SOURCE_DATABASE)
        require(connection.execute('SELECT count(*) FROM public."LiteLLM_VerificationToken"').fetchone()[0] == 0,
                "initial fixture has no retained credentials")
        # Tests leave synthetic pool feedback; isolate this new process rehearsal.
        connection.execute("DELETE FROM gateway_ext.pools")
        connection.execute("DELETE FROM gateway_ext.observations")
        baseline = migration_summary(connection)
        with connection.transaction():
            connection.execute('INSERT INTO public."_prisma_migrations" (id, checksum, migration_name, started_at, applied_steps_count) VALUES (%s,%s,%s,now(),0)',
                               ('fixture-unfinished', '0' * 64, 'fixture_unfinished'))
            try:
                migration_summary(connection)
            except RecoveryGuardError:
                pass
            else:
                raise RecoveryVerificationError("Unfinished native migration was not blocked.")
            connection.execute('DELETE FROM public."_prisma_migrations" WHERE id=%s', ('fixture-unfinished',))
        try:
            with connection.transaction():
                connection.execute('CREATE TABLE gateway_ext.fixture_failed_migration (value integer)')
                connection.execute('SELECT 1 / 0')
        except psycopg.errors.DivisionByZero:
            pass
        require(connection.execute("SELECT to_regclass('gateway_ext.fixture_failed_migration')").fetchone()[0] is None,
                "failed migration DDL transaction rolled back")
        require(migration_summary(connection) == baseline, "failed migration preserves baseline")

    started = time.monotonic()
    evidence = {"status": "not_completed", "scope": "native processes + disposable PostgreSQL + local TCP mock"}
    with tempfile.TemporaryDirectory(prefix="native-recovery-", dir=temp_dir) as directory:
        work = Path(directory)
        state = work / "source-state"
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            http_port = reserved.getsockname()[1]
        journal_path = work / "revocations.jsonl"
        journal_target = f"mock:{http_port}"
        with journal_locked(journal_path, journal_target, initialize=True):
            pass
        upstream = MockState(capture_payloads=False)
        with serve_mock(upstream) as mock:
            a = yaml.safe_load((ROOT / "config/policy.mock.yaml").read_text())
            for deployment in a["deployments"]:
                deployment["api_base"] = mock.base_url
            a["limits"] = {"first_event_timeout": 2, "stream_idle_timeout": 2, "total_timeout": 8, "cooldown_seconds": 1}
            b = copy.deepcopy(a)
            b["deployments"][0]["order"], b["deployments"][1]["order"] = 2, 1
            paths = {}
            for name, policy in (("a", a), ("b", b)):
                paths[name] = work / f"policy-{name}.yaml"
                paths[name].write_text(yaml.safe_dump(policy))
            revisions = {name: load_policy(path).revision for name, path in paths.items()}
            require(revisions["a"] != revisions["b"], "distinct policy revisions")
            evidence["revisions"] = revisions

            with NativeProcess(paths["a"], source_dsn, work / "source-a1", state, port=http_port) as proxy:
                old, live, stale = [proxy.create_key(alias) for alias in ("rotation-old", "rotation-new", "revoked-after-backup")]
                for key in (old, live):
                    proxy.auth(key, True)
                    response = proxy.generate(key)
                    require(response.status_code == 200 and response.headers.get("x-actual-deployment") == "mock-primary", "rotation overlap generation")
                with journal_locked(journal_path, journal_target) as journal:
                    # Simulate a crash/unknown response after native deletion but
                    # before journal confirmation: durable intent must survive.
                    journal.append("intent", key_hash(old))
                    proxy.revoke(old)
                proxy.auth(old, False)
                proxy.drain()
            evidence["rotation_overlap_and_old_revocation"] = True

            observed_routes = []
            for name in ("b", "a"):
                with NativeProcess(paths[name], source_dsn, work / ("release-" + name), state, port=http_port) as proxy:
                    before = sum(upstream.counts.values())
                    require(proxy.generate(live).status_code == 503, "release stays drained across process restart")
                    require(sum(upstream.counts.values()) == before, "drained release has zero upstream calls")
                    proxy.auth(old, False)
                    proxy.auth(live, True)
                    proxy.resume_fixture()
                    response = proxy.generate(live)
                    expected = "mock-secondary" if name == "b" else "mock-primary"
                    require(response.status_code == 200 and response.headers.get("x-actual-deployment") == expected, "actual route after release/rollback")
                    require(response.headers.get("x-config-revision") == revisions[name], "wire revision after release/rollback")
                    observed_routes.append(expected)
                    if name == "a":
                        # Upstream-observed zero quota must survive the following restore.
                        upstream.set_behavior("mock-primary", "ok", headers={
                            "x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "0",
                            "x-ratelimit-reset-requests": "180s",
                        })
                        require(proxy.generate(live).status_code == 200, "quota observation seed")
                        resources = proxy.request("GET", "/gateway/resources").json()
                        primary = next(p for p in resources["pools"] if p["id"] == "mock-primary-requests")
                        require(primary["observation"]["remaining"] == 0 and primary["availability"] == "known_exhausted", "observed source exhaustion")
                        saved_probe = primary["next_probe_at"]
                    proxy.drain()
            evidence["policy_release_rollback"] = {"observed_order": ["a", "b", "a"], "routes_after_change": observed_routes,
                "drain_persisted_across_native_processes": True, "wire_revisions_verified": True,
                "docker_ops_cli_executed": False}

            print("Native recovery: policy A/B/A and rotation verified; checking snapshot.", flush=True)
            with journal_locked(journal_path, journal_target) as journal:
                snapshot_checkpoint = journal.checkpoint()
            recovery = verify_backup_restore(source_dsn, postgres_bin, temp_dir)
            require(recovery["native_active_key_rows"] == 2, "backup includes two live native keys")
            evidence["snapshot_active_keys"] = 2

            with NativeProcess(paths["a"], source_dsn, work / "source-after-backup", state, port=http_port) as proxy:
                with journal_locked(journal_path, journal_target) as journal:
                    journal.revoke(key_hash(stale), native_journal_request(proxy))
                proxy.auth(stale, False)
                proxy.auth(old, False)
                proxy.auth(live, True)
                proxy.resume_fixture()
                require(proxy.generate(live).status_code == 200, "source live key warmup")
                proxy.await_status(revisions["a"], draining=False)
                before = sum(upstream.counts.values())
                stop_database()
                try:
                    response = proxy.generate(live)
                    require(response.status_code == 503, "actual stopped database rejects cached valid key")
                    require(sum(upstream.counts.values()) == before, "actual database outage has zero upstream attempts")
                finally:
                    start_database()
                # Losing the required audit write poisons this process on purpose.
                # DB return alone must not silently erase that unknown request state.
                status = proxy.request("GET", "/gateway/status").json()
                require(status["database_ready"] is False and status["ready"] is False,
                        "audit failure remains fail-closed after database returns")
                require(proxy.generate(live).status_code == 503, "same process remains fail-closed after audit loss")
                require(sum(upstream.counts.values()) == before, "no implicit resume after audit loss")
                require(proxy.request("POST", "/gateway/drain", payload={}).status_code == 200, "drain before explicit recovery restart")
            with NativeProcess(paths["a"], source_dsn, work / "source-database-recovered", state, port=http_port) as proxy:
                proxy.auth(old, False)
                proxy.auth(stale, False)
                proxy.auth(live, True)
                proxy.resume_fixture()
                require(proxy.generate(live).status_code == 200, "explicit native restart after database recovery")
                proxy.drain()
            evidence["actual_database_stop_restart"] = {"cached_key_failed_closed": True, "zero_upstream_during_outage": True,
                "same_process_stays_closed_after_audit_loss": True, "explicit_proxy_restart_restores_readiness": True}

            print("Native recovery: actual DB outage and explicit process recovery verified; checking restored identities.", flush=True)
            restore_state = work / "restored-state"
            restore_state.mkdir(mode=0o700)
            (restore_state / "drain.json").write_text('{"draining":true}')
            target = restored_dsn(source_dsn)
            with NativeProcess(paths["a"], target, work / "restored-first", restore_state, port=http_port) as proxy:
                proxy.auth(live, True)
                proxy.auth(old, False)
                # Snapshot genuinely resurrects a key revoked later. Demonstrate it
                # privately while generation is drained, then reapply revocation.
                proxy.auth(stale, True)
                before = sum(upstream.counts.values())
                require(proxy.generate(stale).status_code == 503, "stale restored key cannot generate during drain")
                require(sum(upstream.counts.values()) == before, "stale restored key has zero upstream calls")
                with journal_locked(journal_path, journal_target) as journal:
                    journal.require_checkpoint(snapshot_checkpoint)
                    reconciliation = journal.reconcile(native_journal_request(proxy))
                    require(reconciliation == {"revocation_intents_checked": 2, "restored_keys_revoked": 1},
                            "durable journal automatically reconciles post-backup native revocations")
                proxy.auth(stale, False)
                resources = proxy.request("GET", "/gateway/resources").json()
                primary = next(p for p in resources["pools"] if p["id"] == "mock-primary-requests")
                require(primary["availability"] == "known_exhausted" and primary["observation"]["remaining"] == 0,
                        "restored process retains measured exhaustion")
                require(primary["next_probe_at"] == saved_probe, "restore never replenishes quota or advances reset")
            with NativeProcess(paths["a"], target, work / "restored-restart", restore_state, port=http_port) as proxy:
                proxy.auth(live, True)
                proxy.auth(old, False)
                proxy.auth(stale, False)
                proxy.resume_fixture()
                upstream.reset()
                response = proxy.generate(live)
                require(response.status_code == 200 and response.headers.get("x-actual-deployment") == "mock-secondary", "restored live key generates through eligible alternate")
                require(upstream.counts["mock-primary"] == 0 and upstream.counts["mock-secondary"] == 1, "restored exhaustion excludes primary")
                proxy.drain()
            # Reuse the SAME restored database key with its stored native limit.
            # The explicit test-only policy makes the shared total budget expire
            # before the per-provider timeout; no live config/debug endpoint is used.
            deadline_policy = copy.deepcopy(a)
            deadline_policy["limits"]["first_event_timeout"] = 10
            for deployment in deadline_policy["deployments"]:
                deployment["adapter"] = "openai_sdk"
            deadline_path = work / "policy-restored-sdk-deadline.yaml"
            deadline_path.write_text(yaml.safe_dump(deadline_policy))
            deadline_revision = load_policy(deadline_path).revision
            with NativeProcess(deadline_path, target, work / "restored-key-slot-recovery", restore_state, port=http_port) as proxy:
                proxy.auth(live, True)
                info = proxy.request("GET", "/key/info?key=" + key_hash(live)).json().get("info", {})
                require(info.get("max_parallel_requests") == 2, "restored key retains native max_parallel_requests=2")
                proxy.resume_fixture()
                require(proxy.generate(live).status_code == 200, "restored SDK same-key warmup")
                proxy.await_status(deadline_revision, draining=False)
                for _ in range(3):
                    upstream.set_behavior("mock-secondary", "timeout", timeout_seconds=12)
                    response = proxy.generate(live)
                    require(response.status_code == 504 and response.json().get("error", {}).get("code") == "deadline_exceeded",
                            "restored existing key reaches the actual nonstream total deadline")
                    proxy.await_status(deadline_revision, draining=False)
                    upstream.set_behavior("mock-secondary", "ok")
                    require(proxy.generate(live).status_code == 200, "same restored key recovers without replacing or resetting its slots")
                    proxy.await_status(deadline_revision, draining=False)
                info = proxy.request("GET", "/key/info?key=" + key_hash(live)).json().get("info", {})
                require(info.get("max_parallel_requests") == 2, "restored key limit remains unchanged after recovery")
                evidence["restored_existing_key_deadline_recovery"] = {
                    "max_parallel_requests_before": 2, "max_parallel_requests_after": 2,
                    "same_key_deadline_then_success_cycles": 3,
                    "adapter": "openai_sdk", "no_key_replacement_or_counter_flush": True}
                proxy.revoke(live)
                proxy.auth(live, False)
                proxy.drain()
            # Revoke final live credential in the source too; then all processes and
            # the runner's entire server/directory are stopped and removed.
            with NativeProcess(paths["a"], source_dsn, work / "source-cleanup", state, port=http_port) as proxy:
                proxy.revoke(live)
                proxy.auth(live, False)
            evidence.update(status="passed", restored_active_key_authenticated=True,
                revoked_before_backup_stays_revoked=True, post_backup_revocation_resurrection_demonstrated=True,
                resurrected_key_reconciled_before_resume=True, reconciliation_survives_process_restart=True,
                exhaustion_and_reset_preserved=True, restored_inference_uses_only_eligible_alternate=True,
                automatic_revocation_journal_recovery=True, durable_unknown_revocation_intent_retained=True,
                unfinished_migration_guard_verified=True, failed_migration_ddl_rolled_back=True,
                all_fixture_keys_revoked=True, elapsed_seconds=round(time.monotonic() - started, 3),
                excludes=["Docker/Compose/Nginx", "Mac", "production credentials", "cross-version migration", "real suppliers"])
    return recovery, evidence
