"""Native key helper safety: synthetic data, stub HTTP, and no native key operations."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

# Explicit path also supports pytest and direct unittest invocation.
OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import gateway_ops
import key_admin

MASTER = "sk-synthetic-mock-administrator-00000001"
PRODUCTION = "sk-synthetic-production-administrator-00000002"
NATIVE = "sk-synthetic-native-key-00000003"


class KeySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS's temporary path can itself include a symlink (/var -> /private/var).
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.env = self.write(".env.mock", f"LITELLM_MASTER_KEY={MASTER}\nADMIN_PORT=4101\nCONSUMER_PORT=4100\n")
        self.key = self.write("key.json", json.dumps({"key": NATIVE}))
        self.ledger = self.root / "revocations.jsonl"
        with key_admin.revocations.locked(self.ledger, "mock:4101", initialize=True) as journal:
            self.binding = key_admin.journal_binding(journal)
        self.key.write_text(json.dumps({"key": NATIVE, "target_binding": self.binding}))

    def write(self, name, text, mode=0o600):
        path = self.root / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            code = key_admin.main(args)
        self.assertNotIn(MASTER, output.getvalue() + errors.getvalue())
        self.assertNotIn(PRODUCTION, output.getvalue() + errors.getvalue())
        self.assertNotIn(NATIVE, output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def execute_args(self, action="info", **options):
        args = [action, "--key-file", str(options.get("key_file", self.key)), "--env-file", str(self.env), "--execute", "--target", "mock", "--ledger-file", str(self.ledger)]
        if action != "info":
            args.append("--ack-key-change")
        if action == "create":
            args += ["--name", "demo", "--deployment-id", "mock-primary"]
        return args

    @contextlib.contextmanager
    def stub_http(self, response=None, exception=None):
        with patch.object(key_admin.urllib.request, "build_opener") as build:
            opener = build.return_value
            def respond(request, **kwargs):
                if request.full_url.endswith("/gateway/config"):
                    return io.BytesIO(b'{"profile":"mock","candidates":[{"id":"mock-primary"},{"id":"mock-secondary"}]}')
                if request.full_url.endswith("/v1/models"):
                    raise urllib.error.HTTPError(request.full_url, 401, "rejected", {}, None)
                if exception is not None:
                    raise exception
                if request.full_url.endswith("/key/info?key=" + key_admin.revocations.key_hash(NATIVE)):
                    return io.BytesIO(json.dumps({"key": key_admin.revocations.key_hash(NATIVE), "info": {"status": "deleted"}}).encode())
                body = json.dumps(response if response is not None else {"info": {"models": ["general-free"]}}).encode()
                return io.BytesIO(body)
            opener.open.side_effect = respond
            yield build, opener

    def test_create_plan_cannot_read_files_or_create_credentials(self):
        with patch.object(key_admin, "read_private", side_effect=AssertionError("secret read forbidden")), \
             patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            code, output, _ = self.invoke(["create", "--name", "demo", "--deployment-id", "mock-primary",
                                         "--key-file", "/not-created/key.json", "--env-file", "/not-read/.env.mock"])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertFalse(report["executed"])
        self.assertFalse(report["target_verified"])
        self.assertEqual(report["requested_scope"]["metadata"]["gateway"]["privacy_scope"], "non_sensitive")
        self.assertEqual(report["requested_scope"]["models"], ["general-free"])

    def test_mutation_requires_acknowledgement_before_reading_any_secret(self):
        with patch.object(key_admin, "read_private", side_effect=AssertionError("secret read forbidden")), \
             patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            for action in ("delete", "create"):
                args = self.execute_args(action)
                args.remove("--ack-key-change")
                code, _, errors = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertIn("--ack-key-change", errors)

    def test_delete_plan_does_not_read_the_secret(self):
        with patch.object(key_admin, "read_private", side_effect=AssertionError("secret read forbidden")):
            self.assertEqual(self.invoke(["delete", "--key-file", "/not-read/key.json"])[0], 0)

    def test_every_execution_requires_explicit_env_file(self):
        with patch.object(key_admin, "read_private", side_effect=AssertionError("secret read forbidden")):
            for action in ("info", "create", "delete"):
                args = self.execute_args(action)
                index = args.index("--env-file")
                del args[index:index + 2]
                code, _, errors = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertIn("explicit --env-file", errors)

    def test_mock_file_is_only_credential_and_port_source(self):
        self.write(".env", f"LITELLM_MASTER_KEY={PRODUCTION}\nADMIN_PORT=4001\n")
        self.write("active.env", f"LITELLM_MASTER_KEY={PRODUCTION}\nADMIN_PORT=4001\n")
        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": PRODUCTION, "ADMIN_PORT": "4001",
                                     "HTTP_PROXY": "http://example.invalid:80", "GATEWAY_CONFIG_DIR": "ignored"}), \
             patch.object(gateway_ops, "environment", side_effect=AssertionError("implicit env forbidden")), \
             patch.object(gateway_ops, "read_env", side_effect=AssertionError("fallback forbidden")), \
             self.stub_http() as (build, opener):
            self.assertEqual(self.invoke(self.execute_args())[0], 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "127.0.0.1:4101")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + MASTER)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], key_admin.NoRedirect)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertEqual(opener.open.call_count, 1)

    def test_production_requires_selection_and_uses_its_own_port(self):
        production_file = self.write(".env", f"LITELLM_MASTER_KEY={PRODUCTION}\nADMIN_PORT=4001\nGATEWAY_CONFIG_DIR=/reviewed/release\nPROVIDER_API_KEY=unread-literal\n")
        args = self.execute_args()
        args[args.index("--env-file") + 1] = str(production_file)
        args[args.index("--target") + 1] = "production"
        production_ledger = self.root / "revocations.production.jsonl"
        with key_admin.revocations.locked(production_ledger, "production:4001", initialize=True) as journal:
            self.key.write_text(json.dumps({"key": NATIVE, "target_binding": key_admin.journal_binding(journal)}))
        args[args.index("--ledger-file") + 1] = str(production_ledger)
        with self.stub_http() as (_, opener):
            self.assertEqual(self.invoke(args)[0], 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "127.0.0.1:4001")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + PRODUCTION)

    def test_matching_explicit_admin_url_is_used(self):
        with self.stub_http() as (_, opener):
            self.assertEqual(self.invoke(self.execute_args() + ["--admin-url", "http://127.0.0.1:4101"])[0], 0)
        self.assertEqual(opener.open.call_args.args[0].host, "127.0.0.1:4101")

    def test_wrong_admin_port_fails_before_key_read_or_request(self):
        with patch.object(key_admin, "read_key", side_effect=AssertionError("key read forbidden")), \
             patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            code, _, errors = self.invoke(self.execute_args() + ["--admin-url", "http://127.0.0.1:4001"])
        self.assertEqual(code, 1)
        self.assertIn("does not match", errors)

    def test_missing_empty_and_placeholder_master_never_fall_back(self):
        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": PRODUCTION, "ADMIN_PORT": "4001"}), \
             patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            for assignment in ("", "LITELLM_MASTER_KEY=\n", "LITELLM_MASTER_KEY=sk-replace-with-administrator-value-000\n",
                               "LITELLM_MASTER_KEY=sk-short\n"):
                self.env.write_text(assignment + "ADMIN_PORT=4101\n")
                self.assertEqual(self.invoke(self.execute_args())[0], 1)
            self.env.unlink()
            self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_missing_bad_or_ambiguous_ports_never_fall_back(self):
        with patch.dict(os.environ, {"ADMIN_PORT": "4001"}), \
             patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            for assignment in ("", "ADMIN_PORT=\n", "ADMIN_PORT=0\n", "ADMIN_PORT=65536\n", "ADMIN_PORT=41xx\n",
                               "ADMIN_PORT=4101\nADMIN_PORT=4001\n"):
                self.env.write_text(f"LITELLM_MASTER_KEY={MASTER}\n" + assignment)
                self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_rejects_non_loopback_and_malformed_urls_without_echo(self):
        with patch.object(key_admin, "read_private", side_effect=AssertionError("read forbidden")):
            for url in ("", "http://127.0.0.1:4101\n", "http://127.\t0.0.1:4101", " http://127.0.0.1:4101",
                        "https://127.0.0.1:4101", "http://example.invalid:4101", "http://localhost:4101",
                        f"http://{MASTER}@127.0.0.1:4101", f"http://127.0.0.1:4101/?key={NATIVE}",
                        "http://127.0.0.1", f"http://[{MASTER}]:4101", f"http://127.0.0.1:{MASTER}",
                        "http://127.0.0.1:99999", "http://127.0.0.1:4101/other", "http://127.0.0.1:4101/#x"):
                with self.subTest(url=url):
                    self.assertEqual(self.invoke(self.execute_args() + ["--admin-url", url])[0], 1)

    def test_env_syntax_is_literal_but_rejects_interpolation_commands_and_duplicate_keys(self):
        marker = self.root / "never-created"
        with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            for contents in (f"LITELLM_MASTER_KEY={MASTER}\nADMIN_PORT=${{ADMIN_PORT}}\n",
                             f"LITELLM_MASTER_KEY=$(touch {marker})\nADMIN_PORT=4101\n",
                             f"LITELLM_MASTER_KEY=`touch {marker}`\nADMIN_PORT=4101\n",
                             f"LITELLM_MASTER_KEY={MASTER}\nLITELLM_MASTER_KEY={PRODUCTION}\nADMIN_PORT=4101\n",
                             f"export LITELLM_MASTER_KEY={MASTER}\nADMIN_PORT=4101\n",
                             f"LITELLM_MASTER_KEY='{MASTER}\nADMIN_PORT=4101\n",
                             f"LITELLM_MASTER_KEY={MASTER} # comment\nADMIN_PORT=4101\n"):
                self.env.write_text(contents)
                self.assertEqual(self.invoke(self.execute_args())[0], 1)
        self.assertFalse(marker.exists())

    def test_quoted_literal_values_are_supported(self):
        self.env.write_text(f"# Literal only\nLITELLM_MASTER_KEY='{MASTER}'\nADMIN_PORT=\"4101\"\n")
        self.assertEqual(key_admin.selected_target(self.env, None), ("http://127.0.0.1:4101", MASTER))

    def test_insecure_file_modes_are_rejected_for_env_and_key(self):
        for path in (self.env, self.key):
            for mode in (0o644, 0o640, 0o660, 0o700, 0o777):
                path.chmod(mode)
                with self.assertRaisesRegex(key_admin.OpsError, "Private"):
                    key_admin.read_private(path)
            path.chmod(0o600)

    def test_read_only_private_files_are_supported(self):
        self.env.chmod(0o400)
        self.key.chmod(0o400)
        self.assertEqual(key_admin.read_key(self.key), NATIVE)
        self.assertEqual(key_admin.selected_target(self.env, None)[1], MASTER)

    def test_symlink_files_parents_and_hardlinks_are_rejected(self):
        symlink = self.root / "env-link"
        symlink.symlink_to(self.env)
        directory_link = self.root / "directory-link"
        directory_link.symlink_to(self.root, target_is_directory=True)
        for path in (symlink, directory_link / self.env.name):
            with self.assertRaises(key_admin.OpsError):
                key_admin.read_private(path)
        hardlink = self.root / "env-hardlink"
        os.link(self.env, hardlink)
        with self.assertRaises(key_admin.OpsError):
            key_admin.read_private(hardlink)
        with self.assertRaises(key_admin.OpsError):
            key_admin.read_private(self.env)

    def test_parent_writable_by_others_is_rejected(self):
        self.root.chmod(0o770)
        try:
            with self.assertRaisesRegex(key_admin.OpsError, "parent"):
                key_admin.read_private(self.env)
        finally:
            self.root.chmod(0o700)

    def test_fifo_and_directory_are_rejected_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        directory = self.root / "directory"
        directory.mkdir(mode=0o700)
        for path in (fifo, directory):
            with self.assertRaises(key_admin.OpsError):
                key_admin.read_private(path)

    def test_oversized_and_non_utf8_env_files_are_rejected(self):
        self.env.write_bytes(b"x" * (key_admin.MAX_PRIVATE_BYTES + 1))
        self.assertEqual(self.invoke(self.execute_args())[0], 1)
        self.env.write_bytes(b"\xff")
        self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_bad_key_json_and_shapes_are_redacted(self):
        for contents in ("{bad-json-" + NATIVE, json.dumps([NATIVE]), json.dumps({"key": 1}),
                         json.dumps({"key": "not-a-native-key"}), json.dumps({"key": NATIVE + "\n"})):
            self.key.write_text(contents)
            with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
                self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_create_exclusively_saves_synthetic_stub_response_privately(self):
        output_file = self.root / "new-key.json"
        with self.stub_http({"key": NATIVE}) as (_, opener):
            code, output, _ = self.invoke(self.execute_args("create", key_file=output_file))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["created"])
        self.assertEqual(stat.S_IMODE(output_file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(output_file.read_text())["key"], NATIVE)
        self.assertEqual(json.loads(output_file.read_text())["target_binding"], self.binding)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:4101/key/generate")
        payload = json.loads(request.data)
        self.assertEqual(payload["models"], ["general-free"])
        self.assertEqual(payload["metadata"]["gateway"]["deployment_ids"], ["mock-primary"])
        self.assertEqual(payload["duration"], "30d")
        self.assertEqual(payload["max_parallel_requests"], 2)

    def test_unregistered_deployment_never_creates_a_native_key(self):
        output_file = self.root / "unregistered.json"
        args = self.execute_args("create", key_file=output_file)
        args[args.index("--deployment-id") + 1] = "unregistered-deployment"
        with self.stub_http({"key": NATIVE}) as (_, opener):
            code, _, errors = self.invoke(args)
        self.assertEqual(code, 1)
        self.assertIn("not registered", errors)
        self.assertFalse(output_file.exists())
        self.assertTrue(all(call.args[0].full_url.endswith("/gateway/config") for call in opener.open.call_args_list))

    def test_create_refuses_existing_output_and_symlinks_before_request(self):
        symlink = self.root / "output-symlink"
        symlink.symlink_to(self.key)
        for output_file in (self.key, symlink):
            with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
                self.assertEqual(self.invoke(self.execute_args("create", key_file=output_file))[0], 1)
        self.assertEqual(key_admin.read_key(self.key), NATIVE)

    def test_create_refuses_public_output_parent(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.invoke(self.execute_args("create", key_file=public / "key.json"))[0], 1)
        self.assertFalse((public / "key.json").exists())

    def test_uncertain_create_keeps_reservation_and_does_not_retry(self):
        output_file = self.root / "uncertain.json"
        with self.stub_http(exception=urllib.error.URLError(f"unsafe-echo-{MASTER}-{NATIVE}")) as (_, opener):
            code, _, errors = self.invoke(self.execute_args("create", key_file=output_file))
        self.assertEqual(code, 1)
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(output_file.exists())
        self.assertEqual(output_file.read_bytes(), b"")
        self.assertIn("reservation was retained", errors)

    def test_delete_uses_selected_target_and_retains_local_file(self):
        with self.stub_http({"deleted_keys": [NATIVE]}) as (_, opener):
            code, output, _ = self.invoke(self.execute_args("delete"))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["revocation_requested"])
        request = next(c.args[0] for c in opener.open.call_args_list if c.args[0].full_url.endswith("/key/delete"))
        self.assertEqual(request.full_url, "http://127.0.0.1:4101/key/delete")
        self.assertEqual(json.loads(request.data), {"keys": [key_admin.revocations.key_hash(NATIVE)]})
        self.assertTrue(json.loads(output)["old_key_rejected"])
        self.assertNotIn(NATIVE, self.ledger.read_text())
        self.assertTrue(self.key.exists())

    def test_wrong_target_or_journal_key_file_is_rejected_before_network(self):
        for binding in ({"scope": "production:4001", "journal_id": self.binding["journal_id"]},
                        {"scope": "mock:4101", "journal_id": "f" * 32}, None):
            record = {"key": NATIVE}
            if binding is not None:
                record["target_binding"] = binding
            self.key.write_text(json.dumps(record))
            with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
                for action in ("info", "delete"):
                    code, _, errors = self.invoke(self.execute_args(action))
                    self.assertEqual(code, 1)
                    self.assertIn("another target/journal", errors)

    def test_bind_legacy_file_requires_exact_existing_native_key(self):
        self.key.write_text(json.dumps({"key": NATIVE, "key_alias": "legacy"}))
        args = self.execute_args("bind-existing") + ["--ack-bind-existing"]
        with self.stub_http({"info": {"status": "active"}}):
            self.assertEqual(self.invoke(args)[0], 0)
        self.assertEqual(json.loads(self.key.read_text())["target_binding"], self.binding)
        with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.invoke(args)[0], 1)

    def test_bind_legacy_file_never_treats_missing_wrong_instance_as_success(self):
        self.key.write_text(json.dumps({"key": NATIVE}))
        before = self.key.read_bytes()
        args = self.execute_args("bind-existing") + ["--ack-bind-existing"]
        with patch.object(key_admin, "verify_profile"), patch.object(key_admin, "private_admin", side_effect=key_admin.OpsError("Native key was not found")):
            self.assertEqual(self.invoke(args)[0], 1)
        self.assertEqual(self.key.read_bytes(), before)

    def test_legacy_binding_checks_response_hash_and_native_status_vocabulary(self):
        for response, success in (({"key": "f" * 64, "info": {"status": "active"}}, False),
                                  ({"key": key_admin.revocations.key_hash(NATIVE), "info": {"status": "revoked"}}, True)):
            self.key.write_text(json.dumps({"key": NATIVE}))
            with patch.object(key_admin, "private_admin", return_value=response):
                if success:
                    key_admin.bind_existing(self.key, self.binding, "http://127.0.0.1:4101", MASTER)
                    self.assertEqual(json.loads(self.key.read_text())["target_binding"], self.binding)
                else:
                    with self.assertRaises(key_admin.OpsError):
                        key_admin.bind_existing(self.key, self.binding, "http://127.0.0.1:4101", MASTER)
                    self.assertNotIn("target_binding", json.loads(self.key.read_text()))

    def test_bind_requires_its_specific_ack_before_network(self):
        self.key.write_text(json.dumps({"key": NATIVE}))
        with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            code, _, errors = self.invoke(self.execute_args("bind-existing"))
        self.assertEqual(code, 1)
        self.assertIn("--ack-bind-existing", errors)

    def test_alternate_loopback_cannot_reuse_existing_target_scope(self):
        with patch.object(key_admin, "private_admin", side_effect=AssertionError("network forbidden")):
            code, _, errors = self.invoke(self.execute_args() + ["--admin-url", "http://127.0.0.2:4101"])
        self.assertEqual(code, 1)
        self.assertIn("alternate loopback", errors)

    def test_info_allowlists_fields_and_redacts_echoed_credentials(self):
        response = {"info": {"key_alias": MASTER, "models": ["general-free", NATIVE], "key": NATIVE,
                             "metadata": {"secret": MASTER}, "blocked": False, "max_parallel_requests": 2}}
        with self.stub_http(response):
            code, output, _ = self.invoke(self.execute_args())
        self.assertEqual(code, 0)
        info = json.loads(output)["info"]
        self.assertEqual(info["key_alias"], "[redacted]")
        self.assertEqual(info["models"], ["general-free", "[redacted]"])
        self.assertNotIn("key", info)
        self.assertNotIn("metadata", info)

    def test_http_error_with_secret_url_is_not_echoed(self):
        failure = urllib.error.HTTPError(f"http://127.0.0.1:4101/key/info?key={NATIVE}", 500, MASTER, {}, None)
        with self.stub_http(exception=failure):
            code, _, errors = self.invoke(self.execute_args())
        self.assertEqual(code, 1)
        self.assertIn("state is unknown", errors)

    def test_malformed_http_status_line_with_secret_is_not_echoed(self):
        with self.stub_http(exception=http.client.BadStatusLine(MASTER + NATIVE)):
            code, _, errors = self.invoke(self.execute_args())
        self.assertEqual(code, 1)
        self.assertIn("state is unknown", errors)

    def test_deeply_nested_private_key_json_is_a_sanitized_error(self):
        self.key.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
        with self.assertRaises(key_admin.OpsError) as error:
            key_admin.read_key(self.key)
        self.assertNotIn("Traceback", str(error.exception))

    def test_nested_untrusted_response_cannot_escape_redacted_failure(self):
        with patch.object(key_admin.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b"[" * 2000 + b"0" + b"]" * 2000)
            self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_invalid_admin_response_is_not_echoed(self):
        with patch.object(key_admin.urllib.request, "build_opener") as build:
            for body in (f"not json {MASTER} {NATIVE}".encode(), b"[]", b"x" * (key_admin.MAX_RESPONSE_BYTES + 1)):
                build.return_value.open.return_value = io.BytesIO(body)
                self.assertEqual(self.invoke(self.execute_args())[0], 1)

    def test_redirect_handler_never_follows_another_target(self):
        handler = key_admin.NoRedirect()
        for status in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(None, None, status, "redirect", {}, "http://example.invalid/"))

    def test_output_file_is_required_and_wildcard_deployment_rejected(self):
        self.assertEqual(self.invoke(["create", "--name", "demo", "--deployment-id", "*"])[0], 1)
        self.assertEqual(self.invoke(["create", "--name", "demo", "--deployment-id", "*", "--key-file", "private/key.json"])[0], 1)


if __name__ == "__main__":
    unittest.main()
