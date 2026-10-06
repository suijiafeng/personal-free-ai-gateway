#!/usr/bin/env python3
"""Maintenance CLI. Plans are default. No credentials or runtime state are fabricated."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
ACTIVE = RUNTIME / "active.env"
_TARGET = None
_JOURNAL = None
import revocations
from recovery_guards import MIGRATION_SQL, RecoveryGuardError, require_migrations, require_compatible


class OpsError(RuntimeError):
    pass


# key_admin shares these helpers. When this file is the CLI entry point, avoid
# importing a second module with a different OpsError class/target state.
if __name__ == "__main__":
    sys.modules.setdefault("gateway_ops", sys.modules[__name__])


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_env(path: Path) -> dict[str, str]:
    """Read literal dotenv values without evaluating shell commands/interpolation."""
    result = {}
    if not path.exists():
        return result
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OpsError(f"Invalid environment assignment at {path.name}:{lineno}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.replace("_", "a").isalnum():
            raise OpsError(f"Invalid environment key at {path.name}:{lineno}")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise OpsError(f"Unclosed quoted value at {path.name}:{lineno}")
            value = value[1:-1]
        result[key] = value
    return result


@contextmanager
def selected_target(args):
    """Capture exactly one private env and isolate project, profile and release state."""
    global _TARGET, RUNTIME, ACTIVE
    from key_admin import read_private, selected_target as select_admin
    from mac_preflight import parse_env
    if not args.target or not args.env_file:
        raise OpsError("Execution/read-only live checks require explicit --target and --env-file.")
    url, key = select_admin(args.env_file, args.admin_url)
    values = parse_env(read_private(args.env_file), allowed_keys=None)
    if values.get("LITELLM_MASTER_KEY") != key or int(values.get("ADMIN_PORT", "0")) != urllib.parse.urlsplit(url).port:
        raise OpsError("Selected env changed during validation; no operation was attempted.")
    if not args.docker_host.startswith("unix:///") or any(c in args.docker_host for c in "\n\r\x00"):
        raise OpsError("Maintenance accepts only an explicit local Unix Docker socket, never an ambient remote context.")
    previous = (_TARGET, RUNTIME, ACTIVE)
    RUNTIME = ROOT / ".runtime" / args.target
    ACTIVE = RUNTIME / "active.env"
    _TARGET = {"profile": args.target, "env_file": args.env_file.absolute(), "values": values,
               "url": url, "docker_host": args.docker_host,
               "project": "gateway-mock" if args.target == "mock" else "personal-free-ai-gateway",
               "reviewed_egress": getattr(args, "reviewed_egress", False),
               "providers": sorted(set(getattr(args, "provider", [])))}
    try:
        yield _TARGET
    finally:
        _TARGET, RUNTIME, ACTIVE = previous


def profile() -> str:
    return _TARGET["profile"] if _TARGET else "production"


def environment() -> dict[str, str]:
    if _TARGET is None:
        raise OpsError("No explicit maintenance target was selected; ambient credentials are never used.")
    active = read_env(ACTIVE)
    if set(active) - {"GATEWAY_CONFIG_DIR"}:
        raise OpsError("Active release file contains unexpected settings.")
    # No ambient Compose/Docker/provider/credential variables can shadow the file.
    safe = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    return {**safe, **_TARGET["values"], **active}


def active_policy() -> Path:
    configured = environment().get("GATEWAY_CONFIG_DIR")
    path = Path(configured) if configured else ROOT / "config"
    if not path.is_absolute():
        path = ROOT / "deploy" / path
    return path.resolve() / ("policy.sdk.mock.yaml" if profile() == "mock" else "policy.yaml")


def docker(*args: str, capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
    if _TARGET is None:
        raise OpsError("No explicit local Docker target was selected.")
    if shutil.which("docker") is None:
        raise OpsError("Docker is unavailable; this operation was not run.")
    result = subprocess.run(["docker", "--host", _TARGET["docker_host"], *args], cwd=ROOT,
                            env=environment(), capture_output=capture, check=False, **kwargs)
    if result.returncode:
        raise OpsError("Selected Docker operation failed; inspect local diagnostics privately. No success was recorded.")
    return result


def compose(*args: str, capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
    if _TARGET is None:
        raise OpsError("No explicit Compose target was selected.")
    command = ["compose", "--project-directory", str(ROOT / "deploy"),
               "--env-file", str(_TARGET["env_file"]), "-p", _TARGET["project"]]
    if ACTIVE.is_file():
        command += ["--env-file", str(ACTIVE)]
    for relative in compose_files():
        command += ["-f", str(ROOT / relative)]
    return docker(*command, *args, capture=capture, **kwargs)


def compose_files() -> list[str]:
    files = ["deploy/compose.yaml"]
    if profile() == "mock":
        if _TARGET and (_TARGET.get("reviewed_egress") or _TARGET.get("providers")):
            raise OpsError("Mock maintenance cannot use real-provider egress overlays.")
        return files + ["deploy/compose.mock.yaml"]
    if _TARGET and _TARGET.get("reviewed_egress"):
        files.append("deploy/compose.egress.yaml")
        if set(_TARGET.get("providers", [])) - {"groq", "gemini"}:
            raise OpsError("Unsupported provider overlay; only explicitly selected reviewed files are allowed.")
        for provider in ("groq", "gemini"):
            if provider in _TARGET.get("providers", []):
                files.append(f"deploy/compose.{provider}.yaml")
    return files


def effective_stack() -> dict:
    config = json.loads(compose("config", "--format", "json", text=True).stdout)
    # A policy bind source changes by design on release/restore. All other
    # effective settings (including egress, secrets and migration service) bind
    # the snapshot to the same stack. Resolved secrets never leave memory.
    gateway = config["services"]["gateway"]
    volumes = gateway.get("volumes", [])
    policy_mounts = [item for item in volumes if isinstance(item, dict) and item.get("target") == "/app/config"]
    if len(policy_mounts) != 1 or policy_mounts[0].get("type") != "bind" or policy_mounts[0].get("read_only") is not True:
        raise OpsError("Effective stack must have exactly one read-only policy bind mount.")
    policy_mounts[0]["source"] = "<approved-immutable-policy-release>"
    # Compose name is part of the target. Extension provenance is recorded
    # separately; no unknown Compose file is implicitly merged.
    signature = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    files = {name: digest(ROOT / name) for name in compose_files()}
    if _TARGET and _TARGET.get("reviewed_egress"):
        for name in ("deploy/egress/approved-hosts.txt", "deploy/egress/squid.conf", "deploy/reviewed_start.py"):
            files[name] = digest(ROOT / name)
    return {"files": files, "effective_config_sha256": signature,
            "reviewed_egress": bool(_TARGET and _TARGET.get("reviewed_egress")),
            "providers": _TARGET.get("providers", []) if _TARGET else []}


def assert_running_stack() -> dict:
    services = ["gateway", "postgres", "ingress"]
    if profile() == "mock":
        services.append("mock-upstream")
    elif _TARGET.get("reviewed_egress"):
        services.append("egress")
    raw = compose("config", "--hash", ",".join(services), text=True).stdout
    hashes = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] not in services or not re.fullmatch('[0-9a-f]{64}', parts[1]):
            raise OpsError("Compose could not provide exact service hashes; maintenance was not started.")
        hashes[parts[0]] = parts[1]
    if set(hashes) != set(services):
        raise OpsError("Compose service hashes are incomplete; maintenance was not started.")
    expected_files = [str((ROOT / path).resolve()) for path in compose_files()]
    for service in services:
        containers = compose("ps", "--all", "-q", service, text=True).stdout.strip().splitlines()
        if len(containers) != 1 or not re.fullmatch('[0-9a-f]{12,64}', containers[0]):
            raise OpsError("Expected one owned container per maintenance service; stack identity is unverified.")
        labels = json.loads(docker("inspect", "--format", "{{json .Config.Labels}}", containers[0], text=True).stdout)
        if (labels.get("com.docker.compose.project") != _TARGET["project"]
                or labels.get("com.docker.compose.service") != service
                or labels.get("com.docker.compose.config-hash") != hashes[service]
                or service == "gateway" and labels.get("com.docker.compose.project.config_files", "").split(",") != expected_files):
            raise OpsError("Running Compose stack differs from explicitly selected files/settings. Restore its exact reviewed stack before maintenance; no base-only fallback is allowed.")
    return hashes


def validate_provider_selection(live_config: dict, candidate: Path | None = None):
    if profile() == "mock":
        return
    candidates = live_config.get("candidates")
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        raise OpsError("Running provider selection could not be verified.")
    enabled = sorted({item["provider"] for item in candidates if item.get("enabled") is True})
    selected = _TARGET.get("providers", [])
    if enabled != selected or enabled and not _TARGET.get("reviewed_egress"):
        raise OpsError("Enabled providers must exactly match explicit reviewed egress/provider flags; no provider activation is inferred.")
    if candidate is not None:
        code = ("import json,sys; from gateway.config import load_policy; "
                "p=load_policy(sys.argv[1]); print(json.dumps(sorted({d.provider for d in p.deployments if d.enabled})))")
        result = subprocess.run([sys.executable, "-c", code, str(candidate)], cwd=ROOT, capture_output=True, text=True)
        if result.returncode or json.loads(result.stdout) != enabled:
            raise OpsError("Candidate changes the enabled-provider set. This requires separate reviewed secret/ACL/egress activation; gateway-only policy release is refused before stopping.")
    if _TARGET.get("reviewed_egress"):
        expected = {"groq": "api.groq.com", "gemini": "generativelanguage.googleapis.com"}
        hosts = [line.strip() for line in (ROOT / "deploy/egress/approved-hosts.txt").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        wanted = sorted(expected[name] for name in selected) or ["deny-all.invalid"]
        if sorted(hosts) != wanted:
            raise OpsError("Reviewed egress ACL does not exactly match selected enabled providers; reload/verify the separately reviewed egress stack before maintenance.")


def postgres(role: str, binary: str, *args: str, **kwargs):
    if role not in {"backup", "maintenance"} or binary not in {"psql", "pg_dump", "pg_restore"}:
        raise OpsError("Unsupported PostgreSQL maintenance command.")
    values = environment()
    key = "BACKUP_DATABASE_USER" if role == "backup" else "MAINTENANCE_DATABASE_USER"
    expected = "gateway_backup" if role == "backup" else "gateway_migrate"
    if values.get("DATABASE_NAME") != "gateway" or values.get(key) != expected:
        raise OpsError("Selected env must explicitly identify gateway database and distinct backup/maintenance roles.")
    secret = "/run/secrets/postgres_backup_password" if role == "backup" else "/run/secrets/postgres_migration_password"
    # Password is read inside the container, never transmitted in argv or output.
    script = 'export PGPASSWORD="$(cat "$1")"; shift; exec "$@"'
    return compose("exec", "-T", "postgres", "sh", "-ec", script, "gateway-postgres", secret,
                   binary, "-h", "127.0.0.1", "-U", expected, "-d", "gateway", *args, **kwargs)


def migration_state() -> dict:
    return require_migrations(postgres("backup", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atc", MIGRATION_SQL, text=True).stdout.strip())


def gateway_image_id() -> str:
    # Keep interpolated configuration (including credentials) in memory only.
    config = json.loads(compose("config", "--format", "json", text=True).stdout)
    reference = config["services"]["gateway"]["image"]
    identity = docker("image", "inspect", "--format", "{{.Id}}", reference, text=True).stdout.strip()
    running = compose("images", "-q", "gateway", text=True).stdout.strip().splitlines()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity) or not running:
        raise OpsError("Exact gateway image identity is unavailable; no activation is permitted.")
    if any(not re.fullmatch(r"(?:sha256:)?[0-9a-f]{12,64}", item) or not identity.removeprefix("sha256:").startswith(item.removeprefix("sha256:")) for item in running):
        raise OpsError("Configured image tag differs from the running gateway image; treat this as a separately reviewed upgrade.")
    return identity


def current_journal():
    if _JOURNAL is None:
        raise OpsError("An existing, target-bound revocation journal must be locked for maintenance.")
    return _JOURNAL


def reconcile_revocations(url: str) -> dict:
    from key_admin import journal_request
    state = admin(url, "/gateway/status")
    if not database_ready(state) or state.get("draining") is not True or state.get("active_requests") != 0:
        raise OpsError("Revocation reconciliation requires a drained, database-ready gateway.")
    return current_journal().reconcile(journal_request(url, environment()["LITELLM_MASTER_KEY"]))


def validate(path: Path) -> dict:
    if not path.is_file():
        raise OpsError(f"Policy file does not exist: {path}")
    result = subprocess.run([sys.executable, "-m", "gateway.config", "validate", "--config", str(path)],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        # Config can contain accidental credentials, so do not dump error contents.
        raise OpsError("Policy validation failed. Run the read-only validator locally for details.")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpsError("Validator did not return its JSON report; no release is possible.") from exc
    revision = report.get("config_revision")
    if not isinstance(revision, str) or not revision:
        raise OpsError("Validator did not provide config_revision; no release is possible.")
    return report


def check_admin_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise OpsError("Admin URL must be an HTTP loopback IP and port. Use an SSH tunnel for remote hosts.")
    if not parsed.port:
        raise OpsError("Admin URL must include its explicit loopback port.")
    return url.rstrip("/")


def admin(url: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    from key_admin import private_admin
    if _TARGET is None or url != _TARGET["url"]:
        raise OpsError("Admin URL does not match the explicitly selected maintenance target.")
    return private_admin(url, environment()["LITELLM_MASTER_KEY"], path, method, payload)


def public_status(data: dict) -> dict:
    """Allowlist fields instead of logging arbitrary response bodies."""
    return {key: data[key] for key in
            ("config_revision", "draining", "active_requests", "ready", "database_ready", "database")
            if key in data and isinstance(data[key], (str, int, float, bool, type(None)))}


def drained(url: str, timeout: float) -> dict:
    admin(url, "/gateway/drain", "POST")
    deadline = time.monotonic() + timeout
    while True:
        state = admin(url, "/gateway/status")
        if state.get("draining") is True and state.get("active_requests") == 0:
            return state
        if time.monotonic() >= deadline:
            raise OpsError("Drain timed out. Gateway remains draining; no forced stop or success was recorded.")
        time.sleep(1)


def database_ready(state: dict) -> bool:
    return (state.get("database_ready") is True or state.get("database") == "ready"
            or isinstance(state.get("database"), dict) and state["database"].get("ready") is True)


def await_revision(url: str, revision: str, *, draining: bool, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = admin(url, "/gateway/status")
            if (last.get("config_revision") == revision and last.get("draining") is draining
                    and database_ready(last) and (draining or last.get("ready") is True)):
                return last
        except OpsError:
            pass
        time.sleep(1)
    raise OpsError("Expected revision/readiness was not observed; maintenance remains incomplete.")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp." + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def maintenance_lock():
    from key_admin import private_parent
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with private_parent(RUNTIME / "maintenance.lock", output=True) as (directory, name):
        lock = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            revocations._private_descriptor(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OpsError("Another maintenance operation holds the lock.") from None
            yield
        finally:
            os.close(lock)


def postgres_version() -> str:
    return postgres("backup", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atc", "SHOW server_version_num", text=True).stdout.strip()


def backup(url: str, parent: Path) -> Path:
    state = admin(url, "/gateway/status")
    policy = active_policy()
    report = validate(policy)
    if report.get("profile") != profile():
        raise OpsError("Policy profile differs from the explicitly selected maintenance target.")
    if state.get("config_revision") != report["config_revision"]:
        raise OpsError("Running revision differs from the policy selected for backup; resolve the drift first.")
    service_hashes = assert_running_stack()
    stack_identity = effective_stack()
    migrations = migration_state()
    image = gateway_image_id()
    checkpoint = current_journal().checkpoint()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    from key_admin import private_parent
    with private_parent(parent / "unused", output=True):
        pass
    created_at = datetime.now(timezone.utc)
    timestamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    target = parent / timestamp
    target.mkdir(mode=0o700)
    snapshot = target / "policy.yaml"
    shutil.copyfile(policy, snapshot)
    os.chmod(snapshot, 0o600)
    with snapshot.open("rb") as source:
        os.fsync(source.fileno())
    if validate(snapshot)["config_revision"] != report["config_revision"]:
        raise OpsError("Policy changed during backup; snapshot is incomplete.")
    archive = target / "database.dump"
    # A failed dump leaves an explicitly incomplete directory, without manifest.json.
    with archive.open("xb") as output:
        os.chmod(archive, 0o600)
        postgres("backup", "pg_dump", "--format=custom", "--no-owner", "--no-acl", capture=False, stdout=output, stderr=subprocess.PIPE)
        output.flush()
        os.fsync(output.fileno())
    images = compose("images", "--format", "json", text=True).stdout
    manifest = {
        "format": 2, "managed_by": "gateway_ops", "target": profile(),
        "retention_managed": bool(_TARGET and _TARGET.get("retention_managed")),
        "revocation_checkpoint": checkpoint, "migrations": migrations, "gateway_image_id": image,
        "stack_identity": stack_identity, "service_config_hashes": service_hashes,
        "created_at": created_at.isoformat(), "completed_at": datetime.now(timezone.utc).isoformat(),
        "config_revision": report["config_revision"],
        "files": {"policy.yaml": digest(snapshot), "database.dump": digest(archive)},
        "postgres_version_num": postgres_version(),
        "compose_images": json.loads(images),
        "environment_file_included": False,
        "database_contains_sensitive_authentication_data": True,
        "note": "Database is sensitive. Preserve the separately held original salt and database credentials."
    }
    require_compatible(migration_state(), migrations)
    if gateway_image_id() != image or assert_running_stack() != service_hashes or effective_stack() != stack_identity:
        raise OpsError("Image or Compose stack changed during backup; no completion manifest was written.")
    write_json(target / "manifest.json", manifest)
    return target


def verify_backup(target: Path) -> dict:
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise OpsError("Backup has no completion manifest; it is incomplete or not a supported backup.")
    from key_admin import private_parent, read_private
    with private_parent(manifest_path, output=True):
        pass
    manifest = json.loads(read_private(manifest_path))
    if manifest.get("format") not in {1, 2}:
        raise OpsError("Unsupported backup manifest format.")
    for name in ("policy.yaml", "database.dump"):
        member = target / name
        if member.is_symlink() or not member.is_file() or member.stat().st_nlink != 1 or member.stat().st_mode & 0o077:
            raise OpsError("Backup members must be private single-link regular files, never symlinks.")
        if not (target / name).is_file() or manifest.get("files", {}).get(name) != digest(target / name):
            raise OpsError("Backup integrity check failed.")
    report = validate(target / "policy.yaml")
    if report.get("profile") != profile():
        raise OpsError("Backup policy profile differs from the selected maintenance target.")
    if report["config_revision"] != manifest.get("config_revision"):
        raise OpsError("Backup policy revision differs from its manifest.")
    if manifest.get("format") == 2:
        if manifest.get("managed_by") != "gateway_ops" or manifest.get("target") != profile():
            raise OpsError("Backup owner or target differs; automatic restore is refused.")
        require_migrations(manifest.get("migrations"))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("gateway_image_id", "")):
            raise OpsError("Backup lacks an immutable image identity.")
    return manifest


def require_restore_compatible(manifest: dict):
    if manifest.get("format") != 2:
        raise OpsError("Legacy backup lacks migration/image/revocation evidence; rehearse and review it separately.")
    current_journal().require_checkpoint(manifest.get("revocation_checkpoint"))
    if effective_stack() != manifest.get("stack_identity"):
        raise OpsError("Backup effective Compose/egress stack differs. Automatic cross-stack restore or base-only recreation is refused.")
    require_compatible(migration_state(), manifest.get("migrations"))
    if gateway_image_id() != manifest["gateway_image_id"]:
        raise OpsError("Gateway image differs from the backup; automatic cross-version restore is refused.")


def select_release(candidate: Path) -> Path:
    release = RUNTIME / "releases" / digest(candidate)
    release.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = release / "policy.yaml"
    if destination.exists() and digest(destination) != digest(candidate):
        raise OpsError("Immutable release content changed; refusing to overwrite.")
    if not destination.exists():
        shutil.copyfile(candidate, destination)
        os.chmod(destination, 0o444)
    if profile() == "mock":
        mock = release / "policy.sdk.mock.yaml"
        if not mock.exists():
            shutil.copyfile(destination, mock)
            os.chmod(mock, 0o444)
        elif digest(mock) != digest(destination):
            raise OpsError("Immutable mock release content changed; refusing activation.")
    # No secrets belong in policy; container UID 10001 must read the mounted directory.
    os.chmod(release, 0o555)
    path = str(release)
    if any(char in path for char in "\n\r$'\""):
        raise OpsError("Release path cannot be represented safely in a Compose env file.")
    return release


def activate(release: Path, revision: str, url: str, *, leave_draining: bool = False, baseline: dict | None = None) -> dict:
    if ACTIVE.exists():
        shutil.copyfile(ACTIVE, RUNTIME / "previous-active.env")
        os.chmod(RUNTIME / "previous-active.env", 0o600)
    temporary = RUNTIME / ("active.env.tmp." + uuid.uuid4().hex)
    with temporary.open("x") as output:
        os.chmod(temporary, 0o600)
        output.write(f"GATEWAY_CONFIG_DIR='{release}'\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(ACTIVE)
    directory = os.open(RUNTIME, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    # No build, pull, dependency recreation or implicit image upgrade during config release.
    compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "gateway")
    state = await_revision(url, revision, draining=True)
    assert_running_stack()
    migration_state()
    if baseline is not None:
        require_restore_compatible(baseline)
    if leave_draining:
        return state
    reconcile_revocations(url)
    compose("exec", "-T", "--user", "10001:10001", "gateway", "python", "-c",
            "import os; from pathlib import Path; "
            "Path(os.environ['GATEWAY_STATE_DIR'], 'drain.json').unlink(missing_ok=True)")
    return await_revision(url, revision, draining=False)



def activate_with_recovery(release: Path, revision: str, saved: Path, url: str) -> dict:
    """Activate a candidate; if verification fails, restore the verified snapshot.

    Recovery is a failed release, never a success, and leaves the previous policy
    draining for private review. It does not roll back images or schema. The backup
    is verified again before use so drift/corruption cannot choose an arbitrary
    fallback policy. Docker execution is separately subject to target-host tests.
    """
    # Snapshot recovery prerequisites must be known before touching active.env.
    previous = verify_backup(saved)
    previous_release = select_release(saved / "policy.yaml")
    require_restore_compatible(previous)
    try:
        return activate(release, revision, url, baseline=previous)
    except (OpsError, OSError, ValueError, RecoveryGuardError, revocations.LedgerError):
        # Candidate readiness/resume may have failed after removing its sentinel.
        # Re-establish drain in the shared state volume before recreating any app.
        try:
            # A failed post-resume check may leave an unverified candidate serving.
            # Stop that owned service first; if Docker cannot confirm the stop,
            # report UNKNOWN and never claim previous revision recovery succeeded.
            compose("stop", "--timeout", "100", "gateway")
            compose("run", "--rm", "--no-deps", "--no-build", "--pull", "never", "--user", "10001:10001", "gateway",
                    "python", "-c", "import os; from pathlib import Path; "
                    "p=Path(os.environ['GATEWAY_STATE_DIR']); p.mkdir(parents=True,exist_ok=True); "
                    "(p/'drain.json').write_text('{\"draining\":true}')")
            require_restore_compatible(previous)
            activate(previous_release, previous["config_revision"], url, leave_draining=True, baseline=previous)
        except (OpsError, OSError, ValueError, RecoveryGuardError, revocations.LedgerError):
            raise OpsError("Candidate release failed and previous revision recovery was not verified. "
                           "Do not resume; inspect the gateway privately. No successful release was recorded.") from None
        raise OpsError("Candidate release failed. Previous revision and database readiness were verified; "
                       "the gateway remains draining for review. No successful release was recorded.") from None


def plan(args) -> dict:
    plans = {
        "drain": ["POST private /gateway/drain", "Wait until draining=true and active_requests=0; timeout leaves drain in place"],
        "backup": ["Verify running revision matches active policy", "Verify selected/running Compose stack, migrations, image and existing revocation journal", "Write private config snapshot and read-only-role pg_dump", "Fsync SHA-256 completion manifest after success; exclude .env and journal"],
        "release": ["Validate candidate read-only", "Drain and wait", "Back up config and native PostgreSQL data", "Stop gateway", "Activate immutable policy with existing image", "Verify revision/database readiness", "Remove drain sentinel and verify readiness", "On failed activation, recover verified prior policy and leave draining", "Record observed success only after verified activation"],
        "rollback": ["Validate explicitly chosen earlier policy", "Require database-schema compatibility acknowledgement", "Perform the same backup/drain/recreate/verify procedure as release; do not downgrade image or database"],
        "restore": ["Verify backup hashes and policy", "Require exact PostgreSQL version and explicit database replacement acknowledgement", "Drain; take pre-restore safety backup", "Stop gateway", "Restore dump in one transaction; fail closed on errors", "Activate backed-up policy with currently approved compatible image", "Verify revision/database readiness while still draining", "Reapply least-privilege grants and journaled revocations through native APIs", "Leave gateway draining for independent key/history checks; resume separately"],
        "resume": ["Require explicit expected config revision", "Verify database readiness and zero active requests while draining", "Remove drain sentinel", "Verify ready=true and expected revision"],
    }
    result = {"operation": args.command, "target": args.target, "reviewed_egress": args.reviewed_egress,
              "providers": sorted(set(args.provider)), "env_file": str(args.env_file) if args.env_file else None, "mode": "DRY_RUN", "steps": plans[args.command],
              "executed": False, "runtime_state": "not observed"}
    if args.command in {"release", "rollback"}:
        result["candidate"] = str(args.candidate)
    if args.command == "restore":
        result["backup"] = str(args.archive)
    return result


def preflight(args) -> dict:
    failures = []
    for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY",
                "DATABASE_NAME", "MAINTENANCE_DATABASE_USER", "BACKUP_DATABASE_USER",
                "POSTGRES_MIGRATION_PASSWORD_FILE", "POSTGRES_BACKUP_PASSWORD_FILE"):
        if not environment().get(key):
            failures.append(f"{key} is missing")
    local_env = _TARGET["env_file"]
    if local_env.exists() and local_env.stat().st_mode & 0o077:
        failures.append(".env is readable by other users; chmod 600 is required")
    if not shutil.which("docker"):
        failures.append("Docker/Compose runtime verification unavailable")
    report = validate(args.config)
    return {"mode": "READ_ONLY", "config_revision": report["config_revision"], "blockers": failures,
            "target": profile(), "deployment_verified": False,
            "required_manual_gates": ["Image digest and signature/provenance verification", "License/security review", "Compose and native key/revocation integration tests", "Backup/restore rehearsal", "Provider/account/privacy and explicit network-egress review before enabling a provider"]}


def main(argv=None) -> int:
    global _JOURNAL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "status", "drain", "backup", "release", "rollback", "restore", "resume"])
    parser.add_argument("--execute", action="store_true", help="Default: plan only, without credential reads or writes")
    parser.add_argument("--ack-maintenance", action="store_true")
    parser.add_argument("--ack-compatible-schema", action="store_true")
    parser.add_argument("--ack-database-replace", action="store_true")
    parser.add_argument("--ack-revocation-history", action="store_true", help="Restore/resume: confirm external/raw-API revocations have been accounted for, beyond helper-only history")
    parser.add_argument("--ack-recovery-review", action="store_true", help="Resume after failed maintenance additionally requires its verified --archive")
    parser.add_argument("--target", choices=["production", "mock"])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--reviewed-egress", action="store_true", help="Production only: preserve the existing explicitly reviewed egress overlay")
    parser.add_argument("--provider", action="append", choices=["groq", "gemini"], default=[], help="Production only: explicitly select each already-approved provider overlay")
    parser.add_argument("--ledger-file", type=Path, help="Existing private revocation journal, never restored from the selected archive")
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    parser.add_argument("--admin-url")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--retention-managed", action="store_true", help="Explicitly opt new backups into the separate reviewed retention tool")
    parser.add_argument("--expected-revision")
    parser.add_argument("--timeout", type=float, default=100)
    args = parser.parse_args(argv)
    try:
        if args.provider and not args.reviewed_egress or args.target == "mock" and (args.reviewed_egress or args.provider):
            raise OpsError("Provider overlays require explicit production reviewed egress; mock cannot mix real-provider settings.")
        if args.command in {"release", "rollback"} and not args.candidate:
            raise OpsError("--candidate is required; an earlier revision is never guessed.")
        if args.command == "restore" and not args.archive:
            raise OpsError("--archive is required.")
        if args.command not in {"status", "preflight"} and not args.execute:
            print(json.dumps(plan(args), indent=2))
            return 0
        if args.execute:
            if not args.ack_maintenance:
                raise OpsError("Execution requires --ack-maintenance; no changes were made.")
            if args.command in {"release", "rollback", "restore", "resume"} and not args.ack_compatible_schema:
                raise OpsError("Execution requires --ack-compatible-schema after a documented compatibility review.")
            if args.command == "restore" and not args.ack_database_replace:
                raise OpsError("Restore requires --ack-database-replace; existing database content will be replaced.")
            if args.command in {"restore", "resume"} and not args.ack_revocation_history:
                raise OpsError("Restore/resume requires --ack-revocation-history after external/raw-API revocations are independently accounted for; the journal covers managed-helper operations only.")
        if not 0 < args.timeout <= 600:
            raise OpsError("Drain timeout must be positive and no more than 600 seconds.")
        with selected_target(args) as target:
            target["retention_managed"] = args.retention_managed
            args.admin_url = target["url"]
            args.config = args.config or active_policy()
            args.backup_dir = args.backup_dir or ROOT / "backups" / profile()
            if args.command == "preflight":
                result = preflight(args)
                print(json.dumps(result, indent=2))
                return 2 if result["blockers"] else 0
            live_config = admin(args.admin_url, "/gateway/config")
            if live_config.get("profile") != profile():
                raise OpsError("Running policy profile differs from the explicit maintenance target.")
            validate_provider_selection(live_config)
            if args.command == "status":
                print(json.dumps({"target": profile(), **public_status(admin(args.admin_url, "/gateway/status"))}, indent=2))
                return 0
            if args.command != "drain" and args.ledger_file is None:
                raise OpsError("Maintenance requires --ledger-file; missing revocation history is never skipped.")
            with maintenance_lock(), ExitStack() as stack:
                assert_running_stack()
                if args.command != "drain":
                    _JOURNAL = stack.enter_context(revocations.locked(args.ledger_file, revocations.scope(profile(), args.admin_url)))
                if args.command == "drain":
                    result = public_status(drained(args.admin_url, args.timeout))
                elif args.command == "backup":
                    result = {"backup": str(backup(args.admin_url, args.backup_dir))}
                elif args.command in {"release", "rollback"}:
                    candidate = args.candidate.resolve()
                    report = validate(candidate)
                    if report.get("profile") != profile():
                        raise OpsError("Candidate profile differs from the selected maintenance target.")
                    validate_provider_selection(live_config, candidate)
                    migration_state()
                    drained(args.admin_url, args.timeout)
                    saved = backup(args.admin_url, args.backup_dir)
                    release = select_release(candidate)
                    if validate(release / "policy.yaml")["config_revision"] != report["config_revision"]:
                        raise OpsError("Candidate changed during release; gateway remains draining.")
                    write_json(RUNTIME / "maintenance-state.json", {"phase": "incomplete", "operation": args.command, "safety_backup": str(saved)})
                    compose("stop", "--timeout", "100", "gateway")
                    state = activate_with_recovery(release, report["config_revision"], saved, args.admin_url)
                    result = {"operation": args.command, "backup": str(saved), **public_status(state)}
                    write_json(RUNTIME / "last-success.json", {"observed_at": datetime.now(timezone.utc).isoformat(), **result})
                    write_json(RUNTIME / "maintenance-state.json", {"phase": "verified", "operation": args.command, "revocation_checkpoint": current_journal().checkpoint()})
                elif args.command == "restore":
                    archive = args.archive.resolve()
                    manifest = verify_backup(archive)
                    validate_provider_selection(live_config, archive / "policy.yaml")
                    require_restore_compatible(manifest)
                    if manifest["postgres_version_num"] != postgres_version():
                        raise OpsError("PostgreSQL version differs. Rehearse migration separately; automatic restore refused.")
                    drained(args.admin_url, args.timeout)
                    saved = backup(args.admin_url, args.backup_dir)
                    write_json(RUNTIME / "maintenance-state.json", {"phase": "incomplete", "operation": "restore", "safety_backup": str(saved)})
                    compose("stop", "--timeout", "100", "gateway")
                    with (archive / "database.dump").open("rb") as source:
                        postgres("maintenance", "pg_restore", "--clean", "--if-exists", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", stdin=source)
                    compose("run", "--rm", "--no-deps", "--no-build", "--pull", "never", "migrate", "--grants-only")
                    require_compatible(migration_state(), manifest["migrations"])
                    state = activate(select_release(archive / "policy.yaml"), manifest["config_revision"], args.admin_url,
                                     leave_draining=True, baseline=manifest)
                    reconciliation = reconcile_revocations(args.admin_url)
                    result = {"operation": "restore", "safety_backup": str(saved), **public_status(state), **reconciliation,
                              "next": "Check old-key rejection and restored state privately; resume is a separate confirmed action"}
                    write_json(RUNTIME / "maintenance-state.json", {"phase": "verified", "operation": "restore", "config_revision": manifest["config_revision"], "revocation_checkpoint": current_journal().checkpoint()})
                else:
                    if not args.expected_revision:
                        raise OpsError("Resume requires --expected-revision; no revision is inferred.")
                    marker = RUNTIME / "maintenance-state.json"
                    marker_value = json.loads(marker.read_text()) if marker.exists() else {}
                    if "revocation_checkpoint" in marker_value:
                        current_journal().require_checkpoint(marker_value["revocation_checkpoint"])
                    if marker.exists() and marker_value.get("phase") != "verified":
                        if not args.ack_recovery_review or not args.archive:
                            raise OpsError("Previous maintenance is incomplete. Resume requires --ack-recovery-review and the verified safety --archive.")
                        saved = verify_backup(args.archive)
                        require_restore_compatible(saved)
                        if saved["config_revision"] != args.expected_revision:
                            raise OpsError("Failed-maintenance recovery must match the selected safety backup revision.")
                    state = await_revision(args.admin_url, args.expected_revision, draining=True)
                    if state.get("active_requests") != 0:
                        raise OpsError("Active requests remain; resume was not performed.")
                    migration_state()
                    reconciliation = reconcile_revocations(args.admin_url)
                    compose("exec", "-T", "--user", "10001:10001", "gateway", "python", "-c",
                            "import os; from pathlib import Path; Path(os.environ['GATEWAY_STATE_DIR'], 'drain.json').unlink(missing_ok=True)")
                    try:
                        result = {**public_status(await_revision(args.admin_url, args.expected_revision, draining=False)), **reconciliation}
                    except OpsError:
                        # Do not leave an unverified resume serving if its check fails.
                        compose("stop", "--timeout", "100", "gateway")
                        raise OpsError("Resume could not be verified; gateway was stopped. Review privately before restarting.") from None
                    write_json(RUNTIME / "maintenance-state.json", {"phase": "verified", "operation": "resume", "revocation_checkpoint": current_journal().checkpoint()})
                print(json.dumps({"mode": "EXECUTED", "target": profile(), "revocation_journal_coverage": "managed-helper-only", **result}, indent=2))
        return 0
    except (OpsError, RecoveryGuardError, revocations.LedgerError) as exc:
        print(json.dumps({"error": str(exc), "success": False}), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "Maintenance failed; inspect private local state. No success was recorded.", "success": False}), file=sys.stderr)
        return 1
    finally:
        _JOURNAL = None


if __name__ == "__main__":
    raise SystemExit(main())
