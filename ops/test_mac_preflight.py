"""Synthetic unit evidence only: platform, Docker, ports, and files are isolated.

These tests do NOT validate macOS, Docker Desktop, image architecture, or startup.
"""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

SPEC = importlib.util.spec_from_file_location("mac_preflight", Path(__file__).with_name("mac_preflight.py"))
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

# Test-only synthetic values. Never reused for any service or real account.
PASSWORD = "synthetic-db-pass:@/%!"
MASTER = "sk-synthetic-unit-fixture-" + "a" * 32
SALT = "synthetic-unit-fixture-salt-" + "b" * 32


PASSWORDS = {"/synthetic-secrets/runtime": PASSWORD,
             "/synthetic-secrets/migration": "synthetic-migration-private-password",
             "/synthetic-secrets/backup": "synthetic-backup-private-password",
             "/synthetic-secrets/bootstrap": "synthetic-bootstrap-private-password"}


def test_secret(path):
    if path not in PASSWORDS:
        raise preflight.CheckError("Unknown synthetic private file.")
    return PASSWORDS[path]
test_secret.__test__ = False


def fixture_values():
    return {"POSTGRES_" + role.upper() + "_PASSWORD_FILE": "/synthetic-secrets/" + role
            for role in ("bootstrap", "migration", "runtime", "backup")} | {
            **preflight.IDENTITY_VALUES,
            "DATABASE_URL": "postgresql://gateway_runtime:" + quote(PASSWORD, safe="") + "@postgres:5432/gateway",
            "MIGRATION_DATABASE_URL": "postgresql://gateway_migrate:" + quote(PASSWORDS["/synthetic-secrets/migration"], safe="") + "@postgres:5432/gateway",
            "LITELLM_MASTER_KEY": MASTER, "LITELLM_SALT_KEY": SALT,
            "CONSUMER_PORT": "4100", "ADMIN_PORT": "4101"}


def encoded_env(values):
    return ("\n".join(key + "='" + value + "'" for key, value in values.items()) + "\n").encode()


class LiteralEnvTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(preflight, "read_secret", side_effect=test_secret)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_template_matches_documented_subset_and_is_empty(self):
        values = preflight.parse_env((preflight.ROOT / ".env.example").read_bytes())
        self.assertEqual(preflight.check_values(values, template=True), (4000, 4001))

    def test_crlf_comments_and_quoted_literal_spaces_hash_are_supported(self):
        data = b'# note\r\nPOSTGRES_RUNTIME_PASSWORD_FILE="a # b"\r\nLITELLM_SALT_KEY=abc\r\n'
        self.assertEqual(preflight.parse_env(data)["POSTGRES_RUNTIME_PASSWORD_FILE"], "a # b")

    def test_unsupported_dotenv_and_shell_syntax_are_refused_without_values(self):
        cases = [b"export POSTGRES_RUNTIME_PASSWORD_FILE=PRIVATE", b"POSTGRES_RUNTIME_PASSWORD_FILE=$(touch PRIVATE)",
                 b"POSTGRES_RUNTIME_PASSWORD_FILE=${PRIVATE}", b"POSTGRES_RUNTIME_PASSWORD_FILE='${PRIVATE}'",
                 b"POSTGRES_RUNTIME_PASSWORD_FILE=`PRIVATE`", b'POSTGRES_RUNTIME_PASSWORD_FILE="a\\nPRIVATE"',
                 b"POSTGRES_RUNTIME_PASSWORD_FILE=PRIVATE # inline", b"POSTGRES_RUNTIME_PASSWORD_FILE=PRIVATE\nPOSTGRES_RUNTIME_PASSWORD_FILE=x",
                 b"POSTGRES_RUNTIME_PASSWORD_FILE='PRIVATE\nmultiline'", b"COMPOSE_FILE=PRIVATE", b"GROQ_API_KEY=PRIVATE",
                 b"POSTGRES_RUNTIME_PASSWORD_FILE='PRIVATE' suffix", b"POSTGRES_RUNTIME_PASSWORD_FILE=PRI\x00VATE",
                 b"1KEY=PRIVATE", b"POSTGRES_RUNTIME_PASSWORD_FILE=\xff", b"\xef\xbb\xbfPOSTGRES_RUNTIME_PASSWORD_FILE=PRIVATE"]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(preflight.CheckError) as caught:
                    preflight.parse_env(case)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_encoded_password_matches_exactly(self):
        self.assertEqual(preflight.check_values(fixture_values()), (4100, 4101))

    def test_explicit_extended_parser_still_refuses_duplicates_and_interpolation(self):
        data = b"GATEWAY_CONFIG_DIR=/reviewed/path\nGROQ_API_KEY=synthetic\n"
        self.assertEqual(preflight.parse_env(data, allowed_keys=None)["GATEWAY_CONFIG_DIR"], "/reviewed/path")
        for extra in [b"GROQ_API_KEY=other\n", b"OTHER_KEY=${secret}\n"]:
            with self.assertRaises(preflight.CheckError):
                preflight.parse_env(data + extra, allowed_keys=None)

    def test_wrong_database_endpoints_passwords_and_options_fail(self):
        base = fixture_values()
        bad_urls = [base["DATABASE_URL"].replace("postgres:5432", "example.org:5432"),
                    base["DATABASE_URL"].replace("postgres:5432", "postgres:5433"),
                    base["DATABASE_URL"] + "?sslmode=disable", base["DATABASE_URL"] + "#fragment",
                    base["DATABASE_URL"].replace("gateway_runtime:", "someone:"),
                    base["DATABASE_URL"].replace("/gateway", "/other"),
                    "postgresql://gateway:wrong-password-here@postgres/gateway",
                    "postgresql://gateway:" + PASSWORD + "@postgres/gateway",
                    "postgresql://gateway:%XX@postgres/gateway",
                    "postgresql://gateway:%FF@postgres/gateway", "postgresql://[malformed"]
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(preflight.CheckError) as caught:
                    preflight.check_values({**base, "DATABASE_URL": url})
                self.assertNotIn(PASSWORD, str(caught.exception))

    def test_empty_placeholder_short_and_reused_secrets_fail(self):
        base = fixture_values()
        changes = [{"POSTGRES_RUNTIME_PASSWORD_FILE": ""}, {"POSTGRES_RUNTIME_PASSWORD_FILE": "change-me-long-password"},
                   {"LITELLM_MASTER_KEY": "sk-short"}, {"LITELLM_MASTER_KEY": "a" * 40},
                   {"LITELLM_SALT_KEY": MASTER}, {"LITELLM_SALT_KEY": "REPLACE_WITH_PRIVATE_SALT"}]
        for change in changes:
            with self.assertRaises(preflight.CheckError):
                preflight.check_values({**base, **change})

    def test_ports_must_be_distinct_nonprivileged_decimal_numbers(self):
        for value in ["80", "1023", "65536", "4100;touch x", "4e3", "127.0.0.1:4100", " 4100", "-4000"]:
            with self.assertRaises(preflight.CheckError):
                preflight.check_values({**fixture_values(), "CONSUMER_PORT": value})
        with self.assertRaises(preflight.CheckError):
            preflight.check_values({**fixture_values(), "CONSUMER_PORT": "4101"})

    def test_template_with_any_credential_and_missing_keys_fail(self):
        with self.assertRaises(preflight.CheckError):
            preflight.check_values(fixture_values(), template=True)
        with self.assertRaises(preflight.CheckError):
            preflight.check_values({})


class LocalFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env.mock"
        self.env.write_bytes(encoded_env(fixture_values()))
        self.env.chmod(0o600)

    def test_private_regular_file_is_read_without_modifying_bytes_or_mode(self):
        before = (self.env.read_bytes(), self.env.stat().st_mode, self.env.stat().st_mtime_ns)
        self.assertEqual(preflight.read_local(self.root, ".env.mock", private=True), before[0])
        self.assertEqual((self.env.read_bytes(), self.env.stat().st_mode, self.env.stat().st_mtime_ns), before)

    def test_unsafe_modes_are_refused(self):
        for mode in [0o644, 0o660, 0o700, 0o604, 0o000]:
            self.env.chmod(mode)
            with self.assertRaises((preflight.CheckError, OSError)):
                preflight.read_local(self.root, ".env.mock", private=True)

    def test_symlink_and_hardlink_secrets_are_refused(self):
        (self.root / "link").symlink_to(self.env)
        with self.assertRaises((preflight.CheckError, OSError)):
            preflight.read_local(self.root, "link", private=True)
        os.link(self.env, self.root / "hardlink")
        with self.assertRaises(preflight.CheckError):
            preflight.read_local(self.root, ".env.mock", private=True)

    def test_parent_symlink_and_path_traversal_are_refused(self):
        (self.root / "actual").mkdir()
        (self.root / "alias").symlink_to(self.root / "actual", target_is_directory=True)
        (self.root / "actual/file").write_text("public")
        for path in ["alias/file", "../file", str(self.env)]:
            with self.assertRaises((preflight.CheckError, OSError)):
                preflight.read_local(self.root, path)

    def test_world_writable_secret_parent_is_refused(self):
        self.root.chmod(0o777)
        with self.assertRaises(preflight.CheckError):
            preflight.read_local(self.root, ".env.mock", private=True)

    def test_different_owner_is_refused(self):
        with patch.object(preflight.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(preflight.CheckError):
                preflight.read_local(self.root, ".env.mock", private=True)

    def test_fifo_and_oversize_files_do_not_hang_or_pass(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(preflight.CheckError):
            preflight.read_local(self.root, "pipe")
        self.env.write_bytes(b"x" * (preflight.MAX_FILE_BYTES + 1))
        with self.assertRaises(preflight.CheckError):
            preflight.read_local(self.root, ".env.mock", private=True)


class MockPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in [".env.example", *preflight.BUNDLED_CONFIG]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(preflight.ROOT / name, target)
        (self.root / ".env.mock").write_bytes(encoded_env(fixture_values()))
        (self.root / ".env.mock").chmod(0o600)
        self.patches = [patch.object(preflight, "read_secret", side_effect=test_secret),
                        patch.dict(os.environ, {}, clear=True),
                        patch.object(preflight.platform, "system", return_value="Darwin"),
                        patch.object(preflight.platform, "machine", return_value="arm64"),
                        patch.object(preflight.shutil, "which", return_value="/trusted/docker"),
                        patch.object(preflight, "local_socket", return_value=("unix:///mock/docker.sock", "desktop")),
                        patch.object(preflight, "port_available", return_value=True),
                        patch.object(preflight, "docker_probe", side_effect=self.probe)]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    @staticmethod
    def probe(executable, args):
        if args == ["--version"]:
            return "Docker version 28.1.1, build synthetic\n"
        if args == ["compose", "version", "--short"]:
            return "2.39.0\n"
        if args == ["--host", "unix:///mock/docker.sock", "version", "--format", "{{json .Server}}"]:
            return json.dumps({"Version": "28.1.1", "Os": "linux", "Arch": "arm64", "unused": MASTER})
        raise AssertionError("Unexpected Docker operation")

    def statuses(self, report):
        return {check["check"]: check["status"] for check in report["checks"]}

    def test_synthetic_happy_path_is_still_not_mac_acceptance(self):
        report = preflight.inspect(self.root, include_plan=True)
        self.assertTrue(report["ready_for_manual_steps"])
        self.assertEqual(report["mac_container_acceptance"], "NOT_RUN")
        self.assertEqual(len(report["plan"]), 7)
        serialized = json.dumps(report)
        for secret in [PASSWORD, MASTER, SALT, fixture_values()["DATABASE_URL"]]:
            self.assertNotIn(secret, serialized)
        for command in report["plan"]:
            self.assertIn("env -i ", command)
            self.assertIn("--host ", command)
            self.assertIn("--env-file .env.mock -p gateway-mock", command)
            self.assertIn("-f deploy/compose.yaml -f deploy/compose.mock.yaml", command)
        self.assertTrue(report["plan"][0].endswith("config --quiet"))

    def test_non_mac_never_contacts_docker_or_claims_mac_pass(self):
        with patch.object(preflight.platform, "system", return_value="Linux"), \
                patch.object(preflight, "docker_probe", side_effect=AssertionError("unexpected probe")):
            report = preflight.inspect(self.root, include_plan=True)
        self.assertEqual(self.statuses(report)["macos"], "FAIL")
        self.assertEqual(self.statuses(report)["docker_server"], "NOT_RUN")
        self.assertEqual(report["plan"], [])

    def test_unknown_architecture_is_not_ready(self):
        with patch.object(preflight.platform, "machine", return_value="unexpected"):
            self.assertFalse(preflight.inspect(self.root)["ready_for_manual_steps"])

    def test_inherited_docker_and_compose_overrides_are_not_loaded(self):
        for key in ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "COMPOSE_FILE", "COMPOSE_ENV_FILES", "GATEWAY_CONFIG_DIR"]:
            with patch.dict(os.environ, {key: MASTER}), \
                    patch.object(preflight, "docker_probe", side_effect=AssertionError("override probe")):
                report = preflight.inspect(self.root, include_plan=True)
            self.assertEqual(self.statuses(report)["shell_overrides"], "FAIL")
            self.assertNotIn(MASTER, json.dumps(report))
            self.assertEqual(report["plan"], [])

    def test_production_env_and_active_release_files_are_never_read(self):
        (self.root / ".env").write_text("LITELLM_MASTER_KEY=production-secret\n")
        (self.root / ".runtime").mkdir()
        (self.root / ".runtime/active.env").write_text("GATEWAY_CONFIG_DIR=/production\n")
        with patch.dict(os.environ, {"POSTGRES_RUNTIME_PASSWORD_FILE": "wrong", "LITELLM_MASTER_KEY": "wrong"}):
            report = preflight.inspect(self.root)
        self.assertTrue(report["ready_for_manual_steps"])
        self.assertNotIn("production-secret", json.dumps(report))

    def test_missing_private_env_does_not_probe_ports_or_print_plan(self):
        (self.root / ".env.mock").unlink()
        with patch.object(preflight, "port_available", side_effect=AssertionError("port probe")):
            report = preflight.inspect(self.root, include_plan=True)
        self.assertEqual(self.statuses(report)["private_env"], "FAIL")
        self.assertEqual(self.statuses(report)["consumer_port"], "NOT_RUN")
        self.assertEqual(report["plan"], [])

    def test_configuration_drift_refuses_startup_plan(self):
        (self.root / "config/policy.yaml").write_text("enabled: true\n")
        report = preflight.inspect(self.root, include_plan=True)
        self.assertEqual(self.statuses(report)["bundled_config"], "FAIL")
        self.assertEqual(report["plan"], [])

    def test_missing_docker_and_socket_are_distinct(self):
        with patch.object(preflight.shutil, "which", return_value=None):
            report = preflight.inspect(self.root)
        self.assertEqual(self.statuses(report)["docker_client"], "FAIL")
        self.assertEqual(self.statuses(report)["docker_server"], "NOT_RUN")
        with patch.object(preflight, "local_socket", return_value=None):
            report = preflight.inspect(self.root)
        self.assertEqual(self.statuses(report)["docker_client"], "PASS")
        self.assertEqual(self.statuses(report)["docker_server"], "NOT_RUN")

    def test_failed_compose_and_engine_are_not_client_success(self):
        for failed_args, name in [(["compose", "version", "--short"], "compose_v2"),
                                  (["--host", "unix:///mock/docker.sock", "version", "--format", "{{json .Server}}"], "docker_server")]:
            with patch.object(preflight, "docker_probe", side_effect=lambda exe, args: None if args == failed_args else self.probe(exe, args)):
                report = preflight.inspect(self.root)
            self.assertEqual(self.statuses(report)["docker_client"], "PASS")
            self.assertEqual(self.statuses(report)[name], "FAIL")

    def test_compose_v1_and_unsafe_server_data_are_refused_without_echo(self):
        def result(exe, args):
            if args == ["compose", "version", "--short"]:
                return "1.29.0\n"
            if "--host" in args:
                return json.dumps({"Version": MASTER, "Os": "windows", "Arch": "arm64"})
            return self.probe(exe, args)
        with patch.object(preflight, "docker_probe", side_effect=result):
            report = preflight.inspect(self.root)
        self.assertEqual(self.statuses(report)["compose_v2"], "FAIL")
        self.assertEqual(self.statuses(report)["docker_server"], "FAIL")
        self.assertNotIn(MASTER, json.dumps(report))

    def test_early_compose_v2_does_not_get_newer_wait_plan(self):
        for version, expected in [("2.0.0", "FAIL"), ("2.19.1", "FAIL"), ("2.20.0", "PASS"), ("5.0.0", "PASS")]:
            with patch.object(preflight, "docker_probe", side_effect=lambda exe, args: version if args == ["compose", "version", "--short"] else self.probe(exe, args)):
                report = preflight.inspect(self.root)
            self.assertEqual(self.statuses(report)["compose_v2"], expected)

    def test_occupied_or_unknown_port_never_passes(self):
        for state in [False, None]:
            with patch.object(preflight, "port_available", return_value=state):
                report = preflight.inspect(self.root)
            self.assertEqual(self.statuses(report)["consumer_port"], "FAIL")

    def test_json_cli_exit_codes_and_plan_withholding(self):
        for ready in [False, True]:
            report = {"ready_for_manual_steps": ready, "checks": [], "plan": [], "note": "test"}
            with patch.object(preflight, "inspect", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(preflight.main(["plan", "--json"]), 0 if ready else 1)
            self.assertEqual(json.loads(output.getvalue()), report)


class ProbeTests(unittest.TestCase):
    def test_subprocess_is_fixed_bounded_shell_free_and_has_no_secrets(self):
        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": MASTER, "DOCKER_HOST": "tcp://private:2375", "COMPOSE_FILE": "private"}), \
                patch.object(preflight.subprocess, "run", return_value=Mock(returncode=0, stdout="synthetic")) as run:
            self.assertEqual(preflight.docker_probe("/trusted/docker", ["--version"]), "synthetic")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/trusted/docker", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn(MASTER, json.dumps(kwargs["env"]))
        self.assertNotIn("DOCKER_HOST", kwargs["env"])
        self.assertNotIn("COMPOSE_FILE", kwargs["env"])

    def test_timeout_and_os_error_do_not_leak_error_text(self):
        for error in [subprocess.TimeoutExpired("secret", 5, output=MASTER), OSError(MASTER)]:
            with patch.object(preflight.subprocess, "run", side_effect=error):
                self.assertIsNone(preflight.docker_probe("docker", ["--version"]))

    def test_port_probe_is_loopback_tcp_without_bind_or_payload(self):
        probe = Mock()
        probe.__enter__ = Mock(return_value=probe)
        probe.__exit__ = Mock(return_value=False)
        for code, expected in [(0, False), (errno.ECONNREFUSED, True), (errno.ETIMEDOUT, None)]:
            probe.connect_ex.return_value = code
            with patch.object(preflight.socket, "socket", return_value=probe) as factory:
                self.assertIs(preflight.port_available(4100), expected)
            factory.assert_called_with(socket.AF_INET, socket.SOCK_STREAM)
            probe.connect_ex.assert_called_with(("127.0.0.1", 4100))
        probe.bind.assert_not_called()
        probe.send.assert_not_called()
        probe.sendall.assert_not_called()

    def test_socket_selection_only_accepts_known_local_unix_sockets(self):
        with patch.object(preflight.Path, "home", return_value=Path("/mock-home")), \
                patch.object(preflight.Path, "stat", return_value=Mock(st_mode=stat.S_IFSOCK)):
            self.assertEqual(preflight.local_socket(), ("unix:///mock-home/.docker/run/docker.sock", "desktop"))
        with patch.object(preflight.Path, "stat", return_value=Mock(st_mode=stat.S_IFREG)):
            self.assertIsNone(preflight.local_socket())


class DatabaseSecretFileTests(unittest.TestCase):
    def test_real_private_secret_file_is_read_only_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "password"
            path.write_text(PASSWORD + "\n")
            path.chmod(0o600)
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            self.assertEqual(preflight.read_secret(str(path)), PASSWORD)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
            path.chmod(0o644)
            with self.assertRaises(preflight.CheckError):
                preflight.read_secret(str(path))
            path.chmod(0o600)
            link = root / "linked"
            link.symlink_to(path)
            with self.assertRaises(preflight.CheckError):
                preflight.read_secret(str(link))

    def test_runtime_and_migration_password_role_mixups_block(self):
        with patch.object(preflight, "read_secret", side_effect=test_secret):
            values = fixture_values()
            for change in ({"DATABASE_URL": values["MIGRATION_DATABASE_URL"]},
                           {"MIGRATION_DATABASE_URL": values["DATABASE_URL"]},
                           {"POSTGRES_BACKUP_PASSWORD_FILE": values["POSTGRES_RUNTIME_PASSWORD_FILE"]},
                           {"MAINTENANCE_DATABASE_USER": "gateway_bootstrap"}):
                with self.assertRaises(preflight.CheckError):
                    preflight.check_values({**values, **change})


if __name__ == "__main__":
    unittest.main()
