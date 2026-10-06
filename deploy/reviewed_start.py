#!/usr/bin/env python3
"""Minimal file-only provider credential bridge; no shell or environment fallback."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

PROVIDERS = {
    'groq': ('api.groq.com', 'GROQ_API_KEY', 'groq_api_key'),
    'gemini': ('generativelanguage.googleapis.com', 'GEMINI_API_KEY', 'gemini_api_key'),
}


def approved_hosts(path):
    lines = [s.strip() for s in Path(path).read_text().splitlines()
             if s.strip() and not s.lstrip().startswith('#')]
    if lines == ['deny-all.invalid']:
        return set()
    if len(lines) != len(set(lines)) or not set(lines) <= {v[0] for v in PROVIDERS.values()}:
        raise ValueError('Egress list must contain exact supported hosts only.')
    return set(lines)


def validate_policy(policy, hosts):
    if policy.profile != 'production':
        raise ValueError('Reviewed egress accepts production policy only.')
    enabled = [d for d in policy.deployments if d.enabled]
    for d in enabled:
        if d.provider not in PROVIDERS or d.credential_env != PROVIDERS[d.provider][1]:
            raise ValueError('Unsupported credential mapping.')
        if policy.exclusion(d) is not None:
            raise ValueError('Provider review is no longer valid.')
    required = {urlsplit(d.api_base).hostname for d in enabled}
    if required != hosts:
        raise ValueError('Egress hosts must exactly equal approved enabled provider hosts.')
    return {d.provider for d in enabled}


def secret_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def read_secret(path, expected=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > 4096
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)):
            raise ValueError('Provider secret must be a private bounded regular file.')
        if expected is not None and secret_identity(before) != expected:
            raise ValueError('Provider secret identity changed before read.')
        raw = os.read(fd, 4097)
        after = os.fstat(fd)
        if secret_identity(before) != secret_identity(after) or len(raw) != before.st_size:
            raise ValueError('Provider secret changed during read.')
    finally:
        os.close(fd)
    value = raw.decode('ascii').removesuffix('\n')
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('Provider secret format is invalid.')
    return value


def read_secret_as_owner(path):
    # Root without DAC_OVERRIDE cannot read another UID's 0400/0600 file.
    # Temporarily assume its owner while retaining saved UID0; restore before
    # the irreversible application drop. No threads or app imports run here.
    if os.geteuid() != 0:
        raise RuntimeError('Provider secret bridge must start as root.')
    info = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            or info.st_nlink != 1 or info.st_size > 4096):
        raise ValueError('Provider secret host mode must remain 0400 or 0600.')
    expected = secret_identity(info)
    try:
        os.seteuid(info.st_uid)
        return read_secret(path, expected=expected)
    finally:
        os.seteuid(0)


def prepare_environment(policy, hosts_path, secrets_dir=Path('/run/secrets'), inherited=None,
                        secret_reader=read_secret):
    environment = dict(os.environ if inherited is None else inherited)
    for _, key, _ in PROVIDERS.values():
        environment.pop(key, None)
    # Never inherit a provider key or caller-selected routing/proxy override.
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY',
                'all_proxy', 'NO_PROXY', 'no_proxy'):
        environment.pop(key, None)
    environment.update({k: 'http://egress:3128' for k in
                        ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')})
    environment.update({'NO_PROXY':'localhost,127.0.0.1,postgres',
                        'no_proxy':'localhost,127.0.0.1,postgres'})
    enabled = validate_policy(policy, approved_hosts(hosts_path))
    for provider in enabled:
        _, key, filename = PROVIDERS[provider]
        environment[key] = secret_reader(Path(secrets_dir) / filename)
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check-bootstrap', action='store_true', help='Read approved secret files, drop privileges, report identity, and exit without starting the app.')
    modes.add_argument('--healthcheck', action='store_true', help='Drop privileges first, then perform loopback liveness check; do not read provider secrets.')
    args = parser.parse_args()
    from deploy.privilege_drop import check_bootstrap_capabilities, drop_to_application
    try:
        check_bootstrap_capabilities()
        environment = None
        if not args.healthcheck:
            from gateway.config import load_policy
            environment = prepare_environment(load_policy('/app/config/policy.yaml'),
                '/etc/squid/approved-hosts.txt', secret_reader=read_secret_as_owner)
        identity = drop_to_application()
        if args.check_bootstrap:
            # No environment values, file paths, provider key fragments, or keys.
            print(json.dumps({'bootstrap_readable':True,'application_started':False,
                              'identity':identity}))
            return 0
        if args.healthcheck:
            import urllib.request
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                'http://127.0.0.1:4000/health/liveliness', timeout=3).read()
            return 0
    except Exception:
        print('Reviewed egress startup blocked: check policy, private secret files and privilege transition.', file=sys.stderr)
        return 2
    os.execve(sys.executable, [sys.executable, '-m', 'gateway.bootstrap', '--config',
              '/app/config/policy.yaml', '--profile', 'production', '--port', '4000'], environment)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
