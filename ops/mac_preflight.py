#!/usr/bin/env python3
"""Read-only Mac/mock startup checks. Standard library only; never execute a plan."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 128 * 1024
PROBE_TIMEOUT = 5
SECRET_KEYS = {"DATABASE_URL", "MIGRATION_DATABASE_URL", "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY"}
SECRET_FILE_KEYS = {"POSTGRES_BOOTSTRAP_PASSWORD_FILE", "POSTGRES_MIGRATION_PASSWORD_FILE",
                    "POSTGRES_RUNTIME_PASSWORD_FILE", "POSTGRES_BACKUP_PASSWORD_FILE"}
IDENTITY_VALUES = {"DATABASE_NAME": "gateway", "MAINTENANCE_DATABASE_USER": "gateway_migrate", "BACKUP_DATABASE_USER": "gateway_backup"}
ENV_KEYS = SECRET_KEYS | SECRET_FILE_KEYS | set(IDENTITY_VALUES) | {"CONSUMER_PORT", "ADMIN_PORT"}
# This deliberately narrow starter supports the reviewed bundled configuration only.
# Fingerprints are configuration-drift guards, not a signature or an authenticity check.
BUNDLED_CONFIG = {
    'deploy/compose.yaml': '71cec11efd42d1fce36e4c06d843931a66560ad6b420f1a9123f5a848519a72a',
    'deploy/compose.mock.yaml': '47d24a2546ba4f75bd651301065dd0e9126e29e26c6f343f97b5d4bfd2fbd409',
    'deploy/Dockerfile': '1204e7e8f3ac9e58ccb68bef37a8494578f99c91f0509ac0ea77d4c693f9b6c3',
    'config/policy.yaml': '3844eb9e59281ea9dbbe46b796fa17534bc0f6c213b13ed33e13d13329a888bb',
    'config/policy.mock.yaml': 'ffdf6c3029258219bb6ce36c2ab3503c9d5599e4108949a09e600190388e7568',
    'config/policy.sdk.mock.yaml': '859b370fc843003593a946bc2115fc715838190b590fa6f8eee597e8e1e5a8c3',
    'deploy/nginx/nginx.conf': 'c3b9aa041956af62bf5c9bf741389d0e4147036cc161b7aec58a0e50e235dadd',
    'deploy/nginx/proxy.inc': '5202be3499d71b98065684d6455f5adcc1bd43aa7b03363400136ab5f4228555',
    'deploy/postgres/entrypoint.sh': '22bcbc165cfa88cfbe5c78b623f711579e05cff396b9a63bdae44a43ce1457de',
    'deploy/postgres/10-roles.sh': 'f27b90e66e9e20d5bb8b6a79de97487a18e0a661fc272263a9a78d30a5e95a34',
    'deploy/postgres/roles.psql': 'fb0ecb36942d582704ecba8e39634011ff81ab62b499426fef847e3132076072',
    'deploy/postgres/20-gateway-ext.sql': '1083ee8add332ab998b74fad6b62fb815288b1e0fe73febd2471a3a5bc844eb9',
    'deploy/postgres/30-grants.sql': '0797114aed38e3e76a13a88ac99f78e08afd552b49b5dab0925f70b919c23d58',
    'deploy/postgres/migrate.py': '8b0684b2c9bdaf5d6ba119bde22acfff95f9a1d2020dbedc8990d974ab1a52d9',
}


class CheckError(ValueError):
    """Only static, credential-free messages belong in this exception."""


def read_local(root: Path, relative: str, *, private: bool = False) -> bytes:
    """Bounded, no-symlink regular-file read, anchored to an open repository fd."""
    parts = Path(relative).parts
    if not parts or any(part in {"..", "/"} for part in parts):
        raise CheckError("Unsupported local file path.")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = None
    try:
        if private:
            parent = os.fstat(directory)
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
                raise CheckError("Repository directory must be owned by you and not writable by others.")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise CheckError("Expected a regular local file; symlinks and devices are unsupported.")
        if private and (before.st_uid != os.geteuid() or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}):
            raise CheckError("Private env must be owned by you, have one link, and mode 0600 or 0400.")
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(file_fd, min(8192, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(file_fd)
        if len(data) > MAX_FILE_BYTES:
            raise CheckError("Local configuration exceeds the bounded file size.")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise CheckError("Local file changed while being read; rerun after editing is finished.")
        return bytes(data)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory)


def parse_env(data: bytes, *, allowed_keys: set[str] | None = ENV_KEYS) -> dict[str, str]:
    """Accept a documented literal dotenv subset, never source/expand/evaluate it.

    No export, duplicates, inline comments, escapes, multiline values, substitutions,
    or unknown keys by default. Callers that explicitly select another file may pass
    an allowlist or None, but must still validate every setting they consume.
    Quoted values may contain spaces/#; $ and backticks are refused.
    This keeps accepted values equivalent to Compose's dotenv interpretation.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CheckError("Env must be UTF-8 text.") from None
    values: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.removesuffix("\r").strip(" \t")
        if not line or line.startswith("#"):
            continue
        if any(ord(char) < 32 or ord(char) > 126 for char in line):
            raise CheckError(f"Env contains unsupported characters at line {number}.")
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise CheckError(f"Use literal KEY=value assignments at line {number}.")
        key, value = match.groups()
        if (allowed_keys is not None and key not in allowed_keys) or key in values:
            raise CheckError(f"Env contains an unsupported or duplicate key at line {number}.")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                raise CheckError(f"Env has unsupported quoting at line {number}.")
            value = value[1:-1]
        elif any(char.isspace() or char in "#'\"" for char in value):
            raise CheckError(f"Quote literal values; inline comments are unsupported at line {number}.")
        if any(char in value for char in "$`\\"):
            raise CheckError(f"Interpolation, command syntax, and escapes are unsupported at line {number}.")
        values[key] = value
    return values


def read_secret(path: str) -> str:
    """Read an existing owner-only secret; never create, copy or repair it."""
    from key_admin import read_private, OpsError
    if not path or not Path(path).is_absolute():
        raise CheckError("Password file paths must be explicit absolute paths without shell expansion.")
    try:
        value = read_private(Path(path)).decode("ascii")
    except (OpsError, OSError, UnicodeError):
        raise CheckError("A database password file cannot be safely read; require owned single-link 0400/0600 files without symlinks.") from None
    value = value.removesuffix("\n")
    if not re.fullmatch(r"[!-~]{16,256}", value) or re.search(r"replace|changeme|change.me|example|placeholder|<|>", value, re.I):
        raise CheckError("Database password files require one non-placeholder printable ASCII value of 16–256 characters.")
    return value


def check_values(values: dict[str, str], *, template: bool = False) -> tuple[int, int]:
    if set(values) != ENV_KEYS or any(values[key] != expected for key, expected in IDENTITY_VALUES.items()):
        raise CheckError("Env must contain exactly the current template settings and the fixed separated database identities.")
    if template:
        if any(values[key] for key in SECRET_KEYS | SECRET_FILE_KEYS):
            raise CheckError("Template contains nonempty credentials or private paths; do not use or share it.")
    else:
        for key in SECRET_KEYS:
            value = values[key]
            if (not value.strip() or re.search(r"replace|changeme|change.me|example|placeholder|<|>", value, re.I)
                    or len(value) < (32 if key in {"LITELLM_MASTER_KEY", "LITELLM_SALT_KEY"} else 16)):
                raise CheckError("Credentials are missing, too short, or look like placeholders; fill them privately.")
        if not values["LITELLM_MASTER_KEY"].startswith("sk-"):
            raise CheckError("Administrator key must start with sk- and contain at least 32 characters.")
        passwords = {key: read_secret(values[key]) for key in SECRET_FILE_KEYS}
        if len(set(passwords.values()) | {values["LITELLM_MASTER_KEY"], values["LITELLM_SALT_KEY"]}) != 6:
            raise CheckError("Four database passwords, administrator key, and salt must all be independent.")
        for name, role, password_file in (("DATABASE_URL", "gateway_runtime", "POSTGRES_RUNTIME_PASSWORD_FILE"),
                                           ("MIGRATION_DATABASE_URL", "gateway_migrate", "POSTGRES_MIGRATION_PASSWORD_FILE")):
            try:
                url = urlsplit(values[name])
                password = url.password or ""
                valid = (url.scheme == "postgresql" and url.hostname == "postgres"
                         and url.port in {None, 5432} and url.username == role
                         and url.path == "/gateway" and not url.query and not url.fragment
                         and re.fullmatch(r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+", password)
                         and unquote(password, encoding="utf-8", errors="strict") == passwords[password_file])
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                raise CheckError("Database URLs must use the fixed local postgres service/database, the correct runtime/migration role, and its matching independently held password.")
    ports = []
    for key in ("CONSUMER_PORT", "ADMIN_PORT"):
        value = values[key]
        if not re.fullmatch(r"[0-9]{4,5}", value) or not 1024 <= int(value) <= 65535:
            raise CheckError("Both ports must be decimal integers between 1024 and 65535.")
        ports.append(int(value))
    if ports[0] == ports[1]:
        raise CheckError("Consumer and admin ports must be different.")
    return ports[0], ports[1]


def local_socket() -> tuple[str, str] | None:
    """Never follow a Docker context or use DOCKER_HOST from the caller."""
    for path, label in ((Path.home() / ".docker/run/docker.sock", "desktop"),
                        (Path("/var/run/docker.sock"), "system")):
        try:
            if stat.S_ISSOCK(path.stat().st_mode):
                return "unix://" + str(path), label
        except OSError:
            pass
    return None


def docker_probe(executable: str, args: list[str]) -> str | None:
    environment = {"HOME": str(Path.home()), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
    try:
        result = subprocess.run([executable, *args], env=environment, cwd="/", stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=PROBE_TIMEOUT, check=False, shell=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def port_available(port: int) -> bool | None:
    """No HTTP request or bind: only an IPv4 loopback TCP availability observation."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            result = probe.connect_ex(("127.0.0.1", port))
        if result == 0:
            return False
        return True if result == errno.ECONNREFUSED else None
    except OSError:
        return None


def startup_plan(socket_kind: str) -> list[str]:
    """Display only. No arbitrary paths/values from dotenv are included in shell text."""
    host = '"unix://$HOME/.docker/run/docker.sock"' if socket_kind == "desktop" else "unix:///var/run/docker.sock"
    prefix = 'env -i HOME="$HOME" PATH="$PATH" docker --host ' + host
    prefix += " compose --project-directory deploy --env-file .env.mock -p gateway-mock"
    prefix += " -f deploy/compose.yaml -f deploy/compose.mock.yaml "
    commands = [
        ["config", "--quiet"],
        ["build", "gateway", "mock-upstream"],
        ["up", "-d", "--wait", "postgres"],
        ["run", "--rm", "--no-deps", "--no-build", "--pull", "never", "migrate"],
        ["up", "-d", "--no-deps", "--wait", "mock-upstream", "gateway", "ingress"],
        ["exec", "-T", "ingress", "nginx", "-t"],
        ["ps"],
    ]
    return [prefix + shlex.join(command) for command in commands]


def inspect(root: Path = ROOT, *, include_plan: bool = False) -> dict:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    mac = platform.system() == "Darwin"
    architecture = platform.machine().lower()
    add("macos", "PASS" if mac else "FAIL", "macOS detected." if mac else "Not macOS; Mac acceptance has not run.")
    add("architecture", "PASS" if architecture in {"arm64", "x86_64"} else "FAIL",
        {"arm64": "Apple Silicon arm64.", "x86_64": "x86_64; on Apple Silicon, rerun with native Python."}.get(
            architecture, "Unsupported or unrecognized host architecture."))
    overrides = any(key.startswith(("DOCKER_", "COMPOSE_")) or key == "GATEWAY_CONFIG_DIR" for key in os.environ)
    add("shell_overrides", "FAIL" if overrides else "PASS",
        "Unset Docker/Compose/config override variables in this shell; none were used." if overrides
        else "No Docker/Compose/config overrides detected; inherited app secrets are never used.")

    for name, relative, private in (("template", ".env.example", False), ("private_env", ".env.mock", True)):
        try:
            values = parse_env(read_local(root, relative, private=private))
            ports = check_values(values, template=not private)
            add(name, "PASS", "Literal settings and file safety checks passed; values are withheld.")
            if private:
                configured_ports = ports
        except FileNotFoundError:
            add(name, "FAIL", "Required local file is missing; see docs/mac-setup.md.")
        except CheckError as exc:
            add(name, "FAIL", str(exc))
        except OSError:
            add(name, "FAIL", "Local file cannot be safely read; check ownership, permissions, and symlinks.")

    try:
        matches = all(hashlib.sha256(read_local(root, relative)).hexdigest() == expected
                      for relative, expected in BUNDLED_CONFIG.items())
        add("bundled_config", "PASS" if matches else "FAIL",
            "Reviewed mock-only starter configuration matches; production providers remain disabled." if matches
            else "Bundled configuration changed; this narrow starter cannot approve it. Review configuration separately.")
    except (OSError, CheckError):
        add("bundled_config", "FAIL", "Bundled configuration is missing or cannot be safely read.")

    for index, name in enumerate(("consumer_port", "admin_port")):
        if "configured_ports" not in locals():
            add(name, "NOT_RUN", "Private environment did not pass validation.")
        else:
            available = port_available(configured_ports[index])
            add(name, "PASS" if available is True else "FAIL",
                "IPv4 loopback port currently refuses connections; it is not reserved." if available is True
                else "IPv4 loopback port is occupied." if available is False
                else "Could not establish local port availability.")

    executable = shutil.which("docker")
    target = local_socket() if mac and not overrides else None
    if not executable or overrides or not mac:
        reason = "Docker executable not found." if not executable else "Probe withheld until Mac and shell checks pass."
        add("docker_client", "FAIL" if not executable else "NOT_RUN", reason)
        add("compose_v2", "NOT_RUN", "Docker client probe was not run.")
        add("docker_server", "NOT_RUN", "Docker client probe was not run.")
    else:
        version = docker_probe(executable, ["--version"])
        match = re.fullmatch(r"Docker version ([0-9]+\.[0-9]+\.[0-9]+)[^\r\n]*\n?", version or "")
        add("docker_client", "PASS" if match else "FAIL", "Docker client responds." if match else "Docker client failed or timed out.")
        compose = docker_probe(executable, ["compose", "version", "--short"])
        version_parts = re.fullmatch(r"v?([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3}(?:[-+][A-Za-z0-9_.-]+)?\s*", compose or "")
        # A conservative minimum for the emitted `up --wait` workflow, rather than
        # accepting every historical Compose 2.x release with differing flags.
        supported = version_parts is not None and (int(version_parts[1]), int(version_parts[2])) >= (2, 20)
        add("compose_v2", "PASS" if supported else "FAIL",
            "Docker Compose plugin (2.20+ or newer major) responds." if supported
            else "Compose plugin 2.20+ is required; missing, older, failed, or timed out.")
        if not target:
            add("docker_server", "NOT_RUN", "No local Docker Desktop socket found; open Docker Desktop and rerun.")
        else:
            response = docker_probe(executable, ["--host", target[0], "version", "--format", "{{json .Server}}"])
            try:
                server = json.loads(response or "null")
                healthy = (isinstance(server, dict) and server.get("Os") == "linux"
                           and server.get("Arch") in {"arm64", "aarch64", "amd64", "x86_64"}
                           and isinstance(server.get("Version"), str)
                           and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.-]+)?", server["Version"]))
            except (ValueError, TypeError):
                healthy = False
            add("docker_server", "PASS" if healthy else "FAIL",
                "Local Docker Linux engine responds; image architecture is not yet verified." if healthy
                else "Local Docker engine unavailable, incompatible, or timed out; private output withheld.")

    ready = all(check["status"] == "PASS" for check in checks)
    return {"mode": "read_only", "profile": "mock", "ready_for_manual_steps": ready,
            "mac_container_acceptance": "NOT_RUN", "checks": checks,
            "plan": startup_plan(target[1]) if include_plan and ready and target else [],
            "note": "No credentials, files, settings, containers, or databases were changed. "
                    "Passing checks does not validate a running stack. Run commands only after local review."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("check", "plan"), default="check")
    parser.add_argument("--json", action="store_true", help="Print credential-free machine-readable results.")
    args = parser.parse_args(argv)
    report = inspect(include_plan=args.command == "plan")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            print(f"{item['status']:7} {item['check']}: {item['detail']}")
        print(report["note"])
        if report["plan"]:
            print("\nMANUAL PLAN ONLY: run from the repository root, one command at a time; stop on failure.")
            print("Build/up/migrate create images, a separate gateway-mock project, and test database state.")
            print("\n".join(report["plan"]))
        elif args.command == "plan":
            print("Plan withheld until every prerequisite passes. See docs/mac-setup.md.")
    return 0 if report["ready_for_manual_steps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
