#!/usr/bin/env python3
"""Native key API helper. Plans never read credentials; execution selects one env file."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request

from gateway_ops import OpsError, check_admin_url
from mac_preflight import CheckError, parse_env
import revocations

MAX_PRIVATE_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 128 * 1024


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@contextmanager
def private_parent(path: Path, *, output: bool = False):
    """Open a stable parent directory without traversing any symlink component."""
    absolute = path.absolute()  # Deliberately do not resolve/follow symlinks.
    if not absolute.name or ".." in absolute.parts:
        raise OpsError("Use a direct file path without parent-directory traversal.")
    directory = None
    try:
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in absolute.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & (0o077 if output else 0o022):
            raise OpsError("Private file parent must be owned by you and not writable by others; key output directory must be 0700.")
        yield directory, absolute.name
    except OSError:
        raise OpsError("Cannot open the private file path safely; use existing owned directories without symlinks.") from None
    finally:
        if directory is not None:
            os.close(directory)


def read_private(path: Path) -> bytes:
    """Bounded, owner-only, one-link, regular-file read via an anchored descriptor."""
    with private_parent(path) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}):
                raise OpsError("Private file must be an owned regular file with one link and mode 0600 or 0400.")
            data = bytearray()
            while len(data) <= MAX_PRIVATE_BYTES:
                chunk = os.read(descriptor, min(8192, MAX_PRIVATE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
            if len(data) > MAX_PRIVATE_BYTES:
                raise OpsError("Private file exceeds the bounded size limit.")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OpsError("Private file changed while being read; finish editing before retrying.")
            return bytes(data)
        finally:
            os.close(descriptor)


def key_token(value, *, administrator: bool = False) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"sk-[!-~]{1,509}", value) is not None
            and (not administrator or len(value) >= 32))


def read_key_record(path: Path) -> dict:
    try:
        data = json.loads(read_private(path))
    except (ValueError, UnicodeError, RecursionError):
        raise OpsError("Private key file is not a valid native key response.") from None
    if not isinstance(data, dict) or not key_token(data.get("key")):
        raise OpsError("Private key file does not contain a native key response.")
    return data


def read_key(path: Path, *, binding: dict | None = None) -> str:
    data = read_key_record(path)
    if binding is not None and data.get("target_binding") != binding:
        raise OpsError("Key file is unbound or belongs to another target/journal. No request was sent. Use separately reviewed bind-existing only for verified legacy files.")
    return data["key"]


def journal_binding(journal) -> dict:
    checkpoint = journal.checkpoint()
    return {"scope": checkpoint["target"], "journal_id": checkpoint["journal_id"]}


def bind_existing(path: Path, binding: dict, url: str, administrator_key: str):
    record = read_key_record(path)
    if "target_binding" in record:
        raise OpsError("Existing target binding cannot be relabeled. Use the original target/journal.")
    token_hash = revocations.key_hash(record["key"])
    # Unlike reconciliation, a 404 is never accepted as proof of legacy ownership.
    response = private_admin(url, administrator_key, "/key/info?key=" + token_hash)
    info = response.get("info")
    if response.get("key") != token_hash or not isinstance(info, dict) or info.get("status") not in {"active", "revoked", "expired", "deleted"}:
        raise OpsError("The selected native database did not confirm this legacy key; file was not rebound.")
    record["target_binding"] = binding
    with private_parent(path, output=True) as (directory, name):
        # Verify the private source again immediately before its atomic replacement.
        original = read_key_record(path)
        if original != {key: value for key, value in record.items() if key != "target_binding"}:
            raise OpsError("Legacy key file changed during verification; binding was refused.")
        temporary = name + ".binding." + uuid.uuid4().hex
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(record, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def safe_admin_url(url: str) -> str:
    # URL parse errors may include untrusted input. Never print their raw text.
    try:
        if any(ord(character) <= 32 or ord(character) > 126 for character in url):
            raise ValueError
        return check_admin_url(url)
    except (OpsError, ValueError):
        raise OpsError("Admin URL must be an HTTP loopback IP with an explicit port and no credentials, path, query, or fragment.") from None


def selected_target(path: Path, requested_url: str | None) -> tuple[str, str]:
    """Only this explicit file supplies the admin key and port. No ambient fallback."""
    try:
        values = parse_env(read_private(path), allowed_keys=None)
    except CheckError as exc:
        # The shared literal parser emits static diagnostics/line numbers only.
        raise OpsError(str(exc)) from None
    key = values.get("LITELLM_MASTER_KEY", "")
    if not key_token(key, administrator=True) or re.search(r"replace|changeme|change.me|example|placeholder|<|>", key, re.I):
        raise OpsError("Selected env file must contain an existing non-placeholder sk- administrator key of at least 32 characters.")
    port = values.get("ADMIN_PORT", "")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise OpsError("Selected env file must contain an explicit ADMIN_PORT between 1 and 65535.")
    url = safe_admin_url(requested_url if requested_url is not None else f"http://127.0.0.1:{int(port)}")
    if urllib.parse.urlsplit(url).port != int(port):
        raise OpsError("Admin URL port does not match ADMIN_PORT in the selected env file; no request was sent.")
    if urllib.parse.urlsplit(url).hostname != "127.0.0.1":
        raise OpsError("Selected native key target must use 127.0.0.1; alternate loopback addresses cannot share a journal scope.")
    return url, key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_admin(url: str, administrator_key: str, path: str, method: str = "GET", payload: dict | None = None, *, allow_missing: bool = False) -> dict:
    """One request to the selected loopback target, with no proxies or redirects."""
    request = urllib.request.Request(safe_admin_url(url) + path,
                                     data=json.dumps(payload or {}).encode() if method == "POST" else None,
                                     method=method,
                                     headers={"Authorization": "Bearer " + administrator_key,
                                              "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            encoded = response.read(MAX_RESPONSE_BYTES + 1)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise OpsError("Private admin response exceeded its size limit; state is unknown.")
        data = json.loads(encoded)
    except urllib.error.HTTPError as exc:
        if allow_missing and method == "GET" and path.startswith("/key/info?key=") and exc.code == 404:
            # Missing is only allowed by the journal workflow, after admin readiness.
            return {"info": {"status": "absent"}}
        raise OpsError("Private admin request failed; state is unknown. Inspect privately before retrying.") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, RecursionError):
        # In particular, key/info query strings and response bodies stay private.
        raise OpsError("Private admin request failed; state is unknown. Inspect privately before retrying.") from None
    if not isinstance(data, dict):
        raise OpsError("Private admin response was not an object; state is unknown.")
    return data


def verify_profile(url: str, administrator_key: str, target: str):
    config = private_admin(url, administrator_key, "/gateway/config")
    if config.get("profile") != target:
        raise OpsError("Running policy profile differs from the explicit key-management target; no mutation was attempted.")
    return config


def journal_request(url: str, administrator_key: str):
    return lambda path, method="GET", payload=None: private_admin(
        url, administrator_key, path, method, payload, allow_missing=True)


def verify_key_rejected(url: str, key: str):
    request = urllib.request.Request(safe_admin_url(url) + "/v1/models", headers={"Authorization": "Bearer " + key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return
    except (OSError, ValueError, http.client.HTTPException):
        pass
    raise OpsError("Old-key rejection was not verified; revocation intent remains durable. Inspect privately before retrying.")


def public_info(response: dict, secrets: tuple[str, ...]) -> dict:
    info = response.get("info", response)
    if not isinstance(info, dict):
        raise OpsError("Private admin response lacked a key information object.")

    def redact(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        return value

    return {field: redact(info[field]) for field in
            ("key_alias", "models", "expires", "blocked", "max_parallel_requests") if field in info}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "info", "delete", "ledger-init", "reconcile", "bind-existing"])
    parser.add_argument("--env-file", type=Path, help="Required for execution: sole private credential and ADMIN_PORT source")
    parser.add_argument("--admin-url", help="HTTP 127.0.0.1 URL; port must match selected ADMIN_PORT (default: derived from that file)")
    parser.add_argument("--target", choices=["production", "mock"])
    parser.add_argument("--ledger-file", type=Path, help="Private journal outside database/backup directories; required for mutations")
    parser.add_argument("--ack-bind-existing", action="store_true", help="Explicitly approve binding this legacy private file after native ownership verification")
    parser.add_argument("--ack-ledger-history", action="store_true", help="Initialization only: confirm all historical revocations are accounted for")
    parser.add_argument("--expected-revision", help="Reconciliation requires the exact drained revision")
    parser.add_argument("--name")
    parser.add_argument("--deployment-id", action="append", default=[])
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ack-key-change", action="store_true", help="Confirm the specific persistent key creation/revocation")
    args = parser.parse_args(argv)
    try:
        if args.admin_url is not None:
            safe_admin_url(args.admin_url)
        if args.action in {"create", "info", "delete", "bind-existing"} and not args.key_file:
            raise OpsError("--key-file is required; a key is never printed to the terminal.")
        if args.action == "create":
            if not args.name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.name):
                raise OpsError("Create requires a non-secret client --name (letters/numbers/dot/underscore/dash).")
            if not args.deployment_id or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item) for item in args.deployment_id):
                raise OpsError("Create requires explicit registered --deployment-id values.")
            payload = {"key_alias": args.name, "models": ["general-free"], "duration": "30d",
                       "max_parallel_requests": 2, "metadata": {"gateway": {
                           "privacy_scope": "non_sensitive", "deployment_ids": args.deployment_id}}}
        else:
            payload = None
        if not args.execute:
            print(json.dumps({"action": args.action, "mode": "DRY_RUN", "executed": False,
                              "target": args.target, "ledger_file": str(args.ledger_file) if args.ledger_file else None,
                              "key_file": str(args.key_file), "requested_scope": payload,
                              "env_file": str(args.env_file) if args.env_file else None,
                              "admin_url": args.admin_url, "target_verified": False,
                              "note": "No credential files were read; no key was created, transmitted, or revoked. Execution requires explicit --target, --env-file and --ledger-file; target and binding are checked then."}, indent=2))
            return 0
        if args.action != "info" and not args.ack_key_change:
            raise OpsError("Persistent key changes require --ack-key-change after approval of this exact action.")
        if args.env_file is None:
            raise OpsError("Execution requires an explicit --env-file; no default, shell, or active-release credentials are used.")
        if args.target is None:
            raise OpsError("Execution requires an explicit --target production or mock.")
        if args.ledger_file is None:
            raise OpsError("Key operations require --ledger-file for exact target binding; no journal is inferred or silently skipped.")
        if args.action == "bind-existing" and not args.ack_bind_existing:
            raise OpsError("Legacy file binding requires --ack-bind-existing after review of this exact file and target.")
        if args.action == "ledger-init" and not args.ack_ledger_history:
            raise OpsError("Journal initialization requires --ack-ledger-history after reviewing earlier revocations.")
        url, administrator_key = selected_target(args.env_file, args.admin_url)
        consumer_url = None
        if args.action == "delete":
            values = parse_env(read_private(args.env_file), allowed_keys=None)
            port = values.get("CONSUMER_PORT", "")
            if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535 or int(port) == urllib.parse.urlsplit(url).port:
                raise OpsError("Deletion requires an explicit distinct CONSUMER_PORT in the selected env for old-key rejection verification.")
            consumer_url = f"http://127.0.0.1:{int(port)}"
        with ExitStack() as stack:
            journal = stack.enter_context(revocations.locked(args.ledger_file, revocations.scope(args.target, url),
                                          initialize=args.action == "ledger-init"))
            binding = journal_binding(journal)
            if args.action == "create":
                with private_parent(args.key_file, output=True) as (directory, name):
                    # Exclusive reservation prevents overwriting either a key or a symlink.
                    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=directory)
                    attempted = False
                    try:
                        with os.fdopen(fd, "w") as output:
                            os.fchmod(output.fileno(), 0o600)
                            config = verify_profile(url, administrator_key, args.target)
                            candidates = config.get("candidates")
                            if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
                                raise OpsError("Registered deployment identities could not be verified; no key was created.")
                            registered = {item.get("id") for item in candidates if isinstance(item.get("id"), str)}
                            if not set(args.deployment_id) <= registered:
                                raise OpsError("Requested deployment is not registered in the selected target; no key was created.")
                            attempted = True
                            response = private_admin(url, administrator_key, "/key/generate", "POST", payload)
                            key = response.get("key")
                            if not key_token(key):
                                raise OpsError("Native create response lacked a key; inspect the private key list before retrying.")
                            json.dump({"key": key, "key_alias": args.name, "models": ["general-free"],
                                       "deployment_ids": args.deployment_id, "target_binding": binding}, output)
                            output.write("\n")
                            output.flush()
                            os.fsync(output.fileno())
                        os.fsync(directory)
                        print(json.dumps({"created": True, "key_file": str(args.key_file), "fingerprint": fingerprint(key)}))
                    except Exception:
                        if not attempted:
                            os.unlink(name, dir_fd=directory)
                        else:
                            # A failed response can still follow a committed native creation.
                            # Keep even an empty reservation to prevent an accidental retry.
                            print("A key may have been created. The private output reservation was retained; inspect/revoke through the private key list before retrying.", file=sys.stderr)
                        raise
            elif args.action == "delete":
                key = read_key(args.key_file, binding=binding)
                verify_profile(url, administrator_key, args.target)
                journal.revoke(revocations.key_hash(key), journal_request(url, administrator_key))
                verify_key_rejected(consumer_url, key)
                print(json.dumps({"revocation_requested": True, "old_key_rejected": True,
                                  "fingerprint": fingerprint(key), "journal_durable": True,
                                  "note": "Native deletion and old-key rejection verified. Local key file retained privately."}))
            elif args.action == "bind-existing":
                if "target_binding" in read_key_record(args.key_file):
                    raise OpsError("Existing target binding cannot be relabeled; no request was sent.")
                verify_profile(url, administrator_key, args.target)
                bind_existing(args.key_file, binding, url, administrator_key)
                print(json.dumps({"legacy_file_bound": True, "native_credentials_changed": False,
                                  "key_file": str(args.key_file)}))
            elif args.action == "ledger-init":
                print(json.dumps({"journal_initialized": True, "target": args.target,
                                  "historical_revocations_automatically_discovered": False}))
            elif args.action == "reconcile":
                if not args.expected_revision:
                    raise OpsError("Reconciliation requires --expected-revision while generation is drained.")
                verify_profile(url, administrator_key, args.target)
                state = private_admin(url, administrator_key, "/gateway/status")
                if (state.get("config_revision") != args.expected_revision or state.get("draining") is not True
                        or state.get("database_ready") is not True or state.get("active_requests") != 0):
                    raise OpsError("Expected drained revision and database readiness were not observed; no reconciliation attempted.")
                print(json.dumps({"target": args.target, **journal.reconcile(journal_request(url, administrator_key)),
                                  "draining": True}))
            else:
                key = read_key(args.key_file, binding=binding)
                response = private_admin(url, administrator_key, "/key/info?" + urllib.parse.urlencode({"key": key}))
                print(json.dumps({"fingerprint": fingerprint(key), "info": public_info(response, (administrator_key, key))}, indent=2))
        return 0
    except (OpsError, revocations.LedgerError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"success": False, "error": "Private key operation failed. Inspect files and state privately before retrying."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
