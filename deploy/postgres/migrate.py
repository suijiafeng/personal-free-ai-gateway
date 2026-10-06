"""Explicit, fail-closed native schema maintenance with a separate owner DSN.

Never called by the runtime proxy. Does not create/rotate credentials, resolve a
failed migration, force reset, or run Prisma db push. Logs contain no DSN/SQL.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import psycopg

HERE = Path(__file__).resolve().parent


def validate_owner(connection):
    row = connection.execute(
        "SELECT current_user, current_database(), rolsuper, rolcreatedb, rolcreaterole, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if row != ('gateway_migrate', 'gateway', False, False, False, False, False):
        raise RuntimeError('Dedicated nonsuperuser migration owner required.')
    schemas = connection.execute(
        "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
        "WHERE nspname IN ('public', 'gateway_ext') ORDER BY nspname"
    ).fetchall()
    if schemas != [('gateway_ext', 'gateway_migrate'), ('public', 'gateway_migrate')]:
        raise RuntimeError('Schema ownership must be initialized before migration.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--grants-only', action='store_true')
    args = parser.parse_args()
    dsn = os.environ.get('DATABASE_URL', '')
    parsed = urlsplit(dsn)
    if parsed.scheme != 'postgresql' or parsed.username != 'gateway_migrate' or parsed.path != '/gateway' or parsed.query or parsed.fragment:
        raise RuntimeError('A dedicated migration URL without parameter overrides is required.')
    # Prisma must not silently select an unrelated privileged URL.
    env = dict(os.environ)
    for name in ('DIRECT_URL', 'SHADOW_DATABASE_URL'):
        env.pop(name, None)
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        validate_owner(connection)
    if not args.grants_only:
        extras = importlib.util.find_spec('litellm_proxy_extras')
        if not extras or not extras.origin:
            raise RuntimeError('Pinned native migration package is missing.')
        schema = Path(extras.origin).parent / 'schema.prisma'
        result = subprocess.run(
            [sys.executable, '-m', 'prisma', 'migrate', 'deploy', '--schema', str(schema)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if result.returncode:
            raise RuntimeError('Native migration failed; output suppressed, operator review required.')
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        validate_owner(connection)
        if not args.grants_only:
            connection.execute((HERE / '20-gateway-ext.sql').read_text())
        connection.execute((HERE / '30-grants.sql').read_text())
    print('Database schema/grants verified for dedicated migration owner.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Database maintenance failed; no credentials or raw database errors are displayed.', file=sys.stderr)
        raise SystemExit(1) from None
