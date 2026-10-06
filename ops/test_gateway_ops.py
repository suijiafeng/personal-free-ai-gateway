"""Tests for maintenance safety. These tests never contact Docker or providers."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("gateway_ops", Path(__file__).with_name("gateway_ops.py"))
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


class SafetyTests(unittest.TestCase):
    def test_dry_runs_never_contact_live_service_or_docker(self):
        commands = [["drain"], ["backup"], ["release", "--candidate", "does-not-exist.yaml"],
                    ["rollback", "--candidate", "previous.yaml"],
                    ["restore", "--archive", "unverified-backup"], ["resume"]]
        with patch.object(ops, "admin", side_effect=AssertionError("network call")), \
             patch.object(ops, "compose", side_effect=AssertionError("Docker call")), \
             patch.object(ops, "maintenance_lock", side_effect=AssertionError("write")):
            for command in commands:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ops.main(command), 0)
                self.assertFalse(json.loads(output.getvalue())["executed"])

    def test_execute_without_maintenance_ack_is_denied(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             patch.object(ops, "maintenance_lock", side_effect=AssertionError("write")):
            self.assertEqual(ops.main(["drain", "--execute"]), 1)

    def test_restore_needs_specific_destructive_ack(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             patch.object(ops, "maintenance_lock", side_effect=AssertionError("write")):
            self.assertEqual(ops.main(["restore", "--archive", "backup", "--execute",
                                      "--ack-maintenance", "--ack-compatible-schema"]), 1)

    def test_admin_destination_is_loopback_only(self):
        for unsafe in ["http://example.com:4001", "https://127.0.0.1:4001", "http://localhost:4001",
                       "http://127.0.0.1:4001/other", "http://user:pass@127.0.0.1:4001", "http://127.0.0.1"]:
            with self.assertRaises(ops.OpsError):
                ops.check_admin_url(unsafe)
        self.assertEqual(ops.check_admin_url("http://127.0.0.1:4001/"), "http://127.0.0.1:4001")

    def test_missing_status_fields_never_mean_drained(self):
        with patch.object(ops, "admin", return_value={}), \
             patch.object(ops.time, "monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(ops.OpsError, "timed out"):
                ops.drained("http://127.0.0.1:4001", 0.1)

    def test_drain_waits_for_zero_active(self):
        states = [{}, {"draining": True, "active_requests": 1},
                  {"draining": True, "active_requests": 0}]
        with patch.object(ops, "admin", side_effect=states), patch.object(ops.time, "sleep"):
            self.assertEqual(ops.drained("http://127.0.0.1:4001", 100)["active_requests"], 0)

    def test_status_does_not_echo_unknown_or_secret_fields(self):
        state = ops.public_status({"config_revision": "abc", "active_requests": 0,
                                   "api_key": "secret", "response": "private", "database": {"url": "secret"}})
        self.assertEqual(state, {"config_revision": "abc", "active_requests": 0})

    def test_env_read_is_literal_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("# comment\nKEY='$(touch should-not-exist)'\nURL=postgres://literal\n")
            self.assertEqual(ops.read_env(env)["KEY"], "$(touch should-not-exist)")
            self.assertFalse((Path(directory) / "should-not-exist").exists())

    def test_incomplete_backup_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ops.OpsError, "completion manifest"):
                ops.verify_backup(Path(directory))

    def test_tampered_backup_is_refused_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "policy.yaml").write_text("original")
            (target / "database.dump").write_bytes(b"dump")
            (target / "manifest.json").write_text(json.dumps({"format": 1, "files": {
                "policy.yaml": "not-the-hash", "database.dump": ops.digest(target / "database.dump")}}))
            for path in target.iterdir():
                path.chmod(0o600)
            with patch.object(ops, "validate", side_effect=AssertionError("should fail before validate")):
                with self.assertRaisesRegex(ops.OpsError, "integrity"):
                    ops.verify_backup(target)

    def test_readiness_requires_database_and_exact_revision(self):
        with patch.object(ops, "admin", return_value={"config_revision": "wrong", "draining": False,
                                                    "ready": True, "database_ready": True}), \
             patch.object(ops.time, "monotonic", side_effect=[0, 0, 100]), \
             patch.object(ops.time, "sleep"):
            with self.assertRaisesRegex(ops.OpsError, "not observed"):
                ops.await_revision("http://127.0.0.1:4001", "expected", draining=False, timeout=1)

    def test_immutable_release_refuses_modified_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.yaml"
            candidate.write_text("original")
            with patch.object(ops, "RUNTIME", root / ".runtime"):
                release = ops.select_release(candidate)
                (release / "policy.yaml").chmod(0o600)
                (release / "policy.yaml").write_text("tampered")
                with self.assertRaisesRegex(ops.OpsError, "Immutable"):
                    ops.select_release(candidate)


class DeploymentShapeTests(unittest.TestCase):
    def test_compose_loopback_internal_network_and_no_extra_worker(self):
        import yaml
        data = yaml.safe_load((ops.ROOT / "deploy/compose.yaml").read_text())
        self.assertTrue(data["networks"]["backend"]["internal"])
        self.assertEqual(data["services"]["gateway"]["networks"], ["backend"])
        for service in ("gateway", "postgres"):
            self.assertNotIn("ports", data["services"][service])
        for port in data["services"]["ingress"]["ports"]:
            self.assertTrue(port.startswith("127.0.0.1:"))
        self.assertNotIn("redis", data["services"])
        self.assertNotIn("--workers", data["services"]["gateway"]["command"])
        self.assertEqual(data["services"]["gateway"]["build"]["target"], "runtime")

    def test_nginx_has_only_exact_allowed_paths(self):
        import re
        config = (ops.ROOT / "deploy/nginx/nginx.conf").read_text()
        paths = re.findall(r"location = (\S+)", config)
        self.assertEqual(set(paths), {"/v1/chat/completions", "/v1/models", "/key/generate", "/key/delete",
                                     "/key/info", "/key/list", "/gateway/status", "/gateway/traces", "/gateway/drain",
                                     "/gateway/resources", "/gateway/config", "/gateway/summary"})
        self.assertEqual(config.count("location / { return 404; }"), 2)
        self.assertNotIn("/gateway/",config.split("listen 8080;",1)[1].split("listen 8081;",1)[0])
        self.assertNotIn("$request_uri", config)
        self.assertNotIn("$request_body", config)
        if "log_format minimal" in config:
            self.assertNotIn("$args", config.split("log_format minimal", 1)[1].split(";", 1)[0])
        else:
            self.assertIn("access_log off;", config)
        self.assertIn("proxy_next_upstream off;", (ops.ROOT / "deploy/nginx/proxy.inc").read_text())


if __name__ == "__main__":
    unittest.main()


class FailedReleaseRecoveryTests(unittest.TestCase):
    """Mocked command transport; this is not Docker acceptance evidence."""
    def setUp(self):
        self.compatibility = patch.object(ops, "require_restore_compatible")
        self.compatibility.start()
        self.addCleanup(self.compatibility.stop)
    def test_successful_activation_does_not_run_recovery(self):
        with patch.object(ops, "verify_backup", return_value={"config_revision": "old"}), \
             patch.object(ops, "select_release", return_value=Path("old-release")), \
             patch.object(ops, "activate", return_value={"config_revision": "new", "ready": True}) as activation, \
             patch.object(ops, "compose") as compose:
            state = ops.activate_with_recovery(Path("candidate"), "new", Path("backup"), "http://127.0.0.1:4001")
            self.assertEqual(state["config_revision"], "new")
            self.assertEqual(activation.call_count, 1)
            compose.assert_not_called()

    def test_failed_activation_recovers_previous_and_leaves_draining(self):
        with patch.object(ops, "verify_backup", return_value={"config_revision": "old"}), \
             patch.object(ops, "select_release", return_value=Path("old-release")), \
             patch.object(ops, "activate", side_effect=[ops.OpsError("synthetic-secret"), {"config_revision": "old"}]) as activation, \
             patch.object(ops, "compose") as compose:
            with self.assertRaisesRegex(ops.OpsError, "Previous revision.*remains draining") as error:
                ops.activate_with_recovery(Path("candidate"), "new", Path("backup"), "http://127.0.0.1:4001")
            self.assertNotIn("synthetic-secret", str(error.exception))
            activation.assert_called_with(Path("old-release"), "old", "http://127.0.0.1:4001", leave_draining=True, baseline={"config_revision": "old"})
            self.assertEqual(compose.call_count, 2)
            self.assertEqual(compose.call_args_list[0].args, ("stop", "--timeout", "100", "gateway"))
            command = compose.call_args.args
            self.assertEqual(command[:10], ("run", "--rm", "--no-deps", "--no-build", "--pull", "never", "--user", "10001:10001", "gateway", "python"))
            self.assertIn("drain.json", command[-1])

    def test_recovery_failure_never_claims_previous_ready(self):
        with patch.object(ops, "verify_backup", return_value={"config_revision": "old"}), \
             patch.object(ops, "select_release", return_value=Path("old-release")), \
             patch.object(ops, "activate", side_effect=ops.OpsError("private-detail")), \
             patch.object(ops, "compose"):
            with self.assertRaisesRegex(ops.OpsError, "recovery was not verified") as error:
                ops.activate_with_recovery(Path("candidate"), "new", Path("backup"), "http://127.0.0.1:4001")
            self.assertNotIn("private-detail", str(error.exception))

    def test_invalid_prior_snapshot_stops_before_candidate_activation(self):
        with patch.object(ops, "verify_backup", side_effect=ops.OpsError("integrity")), \
             patch.object(ops, "activate") as activation, patch.object(ops, "compose") as compose:
            with self.assertRaises(ops.OpsError):
                ops.activate_with_recovery(Path("candidate"), "new", Path("backup"), "http://127.0.0.1:4001")
            activation.assert_not_called()
            compose.assert_not_called()

    def test_failed_candidate_stop_never_runs_sentinel_or_claims_recovery(self):
        with patch.object(ops, "verify_backup", return_value={"config_revision": "old"}), \
             patch.object(ops, "select_release", return_value=Path("old-release")), \
             patch.object(ops, "activate", side_effect=ops.OpsError("after-resume-failure")) as activation, \
             patch.object(ops, "compose", side_effect=ops.OpsError("stop-failed")) as compose:
            with self.assertRaisesRegex(ops.OpsError, "recovery was not verified"):
                ops.activate_with_recovery(Path("candidate"), "new", Path("backup"), "http://127.0.0.1:4001")
            compose.assert_called_once_with("stop", "--timeout", "100", "gateway")
            self.assertEqual(activation.call_count, 1)
