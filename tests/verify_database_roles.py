#!/usr/bin/env python3
"""Disposable SCRAM PostgreSQL 17 role/migration/native-auth/backup acceptance.

Creates and destroys its own loopback-only cluster. Accepts no DSN and never
connects to an existing server. All credentials are fixed synthetic test inputs.
No user secrets or provider accounts are used. Docker/Mac acceptance is separate.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import quote

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROLES = ('gateway_bootstrap', 'gateway_migrate', 'gateway_runtime', 'gateway_backup')
PASSWORDS = {role: 'synthetic-disposable-only-' + role for role in ROLES}


def require(condition, label):
    if not condition:
        raise RuntimeError('Role verification failed: ' + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', required=True)
    parser.add_argument('--evidence', default='evidence/database-roles.json')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run disposable PostgreSQL as a non-root user.')
    binaries = {name: Path(args.postgres_bin).resolve() / name for name in ('initdb', 'pg_ctl', 'createdb', 'psql', 'pg_dump', 'pg_restore')}
    require(all(path.is_file() for path in binaries.values()), 'official PostgreSQL executables present')
    require(shutil.which('node') is not None, 'Node is available')
    temp = Path(tempfile.mkdtemp(prefix='gateway-roles-disposable-'))
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG') and key not in ('DATABASE_URL', 'DIRECT_URL', 'SHADOW_DATABASE_URL')}
    env.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False',
               PGCLIENTENCODING='UTF8', PGCONNECT_TIMEOUT='5', PGSSLMODE='disable', PGGSSENCMODE='disable',
               PGPASSFILE=str(temp / 'unused-passfile'))
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env['PATH']
    env.setdefault('PRISMA_BINARY_CACHE_DIR', str(temp / 'prisma'))
    env.setdefault('PRISMA_NODEENV_CACHE_DIR', str(temp / 'prisma-nodeenv'))
    env.setdefault('PRISMA_USE_GLOBAL_NODE', 'true')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    data, started = temp / 'data', False
    pw = temp / 'bootstrap-password'
    pw.write_text(PASSWORDS['gateway_bootstrap']); pw.chmod(0o600)

    def run(name, arguments, *, role=None, extra=None):
        child = dict(env)
        if role:
            child['PGPASSWORD'] = PASSWORDS[role]
        if extra:
            child.update(extra)
        executable = str(binaries[name]) if name in binaries else name
        result = subprocess.run([executable, *arguments], env=child, cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if result.returncode:
            raise RuntimeError('Role verification command failed: ' + Path(executable).name + '; output suppressed.')
        return result.stdout.decode()

    def connect(role, database='gateway'):
        return psycopg.connect(host='127.0.0.1', port=port, dbname=database, user=role,
                              password=PASSWORDS[role], connect_timeout=5, autocommit=True,
                              options='-c timezone=UTC', client_encoding='UTF8')

    def dsn(role):
        return f'postgresql://{role}:{quote(PASSWORDS[role], safe="")}@127.0.0.1:{port}/gateway'

    def denied(role, statement):
        with connect(role) as connection:
            # ACLs must prevent mutation even after the role disables the session
            # read-only preference; default_transaction_read_only is not security.
            if role == 'gateway_backup':
                connection.execute('SET default_transaction_read_only=off')
            try:
                connection.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                return
            raise RuntimeError('Role privilege denial missing: ' + role)

    try:
        version = run('initdb', ['--version']).strip()
        require(' 17.' in version, 'PostgreSQL 17')
        run('initdb', ['-D', str(data), '-U', 'gateway_bootstrap', '--auth-local=trust',
                       '--auth-host=scram-sha-256', '--pwfile', str(pw), '--encoding=UTF8', '--no-locale'])
        run('pg_ctl', ['-D', str(data), '-l', str(temp / 'postgres.log'), '-o',
                       f"-h 127.0.0.1 -p {port} -c unix_socket_directories='' -c timezone=UTC", 'start'])
        started = True
        client = ['-h', '127.0.0.1', '-p', str(port)]
        run('createdb', [*client, '-U', 'gateway_bootstrap', 'gateway'], role='gateway_bootstrap')
        run('psql', [*client, '-U', 'gateway_bootstrap', '-d', 'gateway', '--no-psqlrc',
                     '--set=ON_ERROR_STOP=1', '--file', str(ROOT / 'deploy/postgres/roles.psql')],
            role='gateway_bootstrap', extra={
                'GATEWAY_MIGRATION_PASSWORD': PASSWORDS['gateway_migrate'],
                'GATEWAY_RUNTIME_PASSWORD': PASSWORDS['gateway_runtime'],
                'GATEWAY_BACKUP_PASSWORD': PASSWORDS['gateway_backup'],
            })
        # A deliberate duplicate-role failure must not echo any substituted
        # password into PostgreSQL statement logs or change global log policy.
        failed_env = dict(env)
        failed_env.update(PGPASSWORD=PASSWORDS['gateway_bootstrap'],
                          GATEWAY_MIGRATION_PASSWORD=PASSWORDS['gateway_migrate'],
                          GATEWAY_RUNTIME_PASSWORD=PASSWORDS['gateway_runtime'],
                          GATEWAY_BACKUP_PASSWORD=PASSWORDS['gateway_backup'])
        failed_ddl = subprocess.run(
            [str(binaries['psql']), *client, '-U', 'gateway_bootstrap', '-d', 'gateway',
             '--no-psqlrc', '--set=ON_ERROR_STOP=1', '--file', str(ROOT / 'deploy/postgres/roles.psql')],
            env=failed_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        require(failed_ddl.returncode != 0, 'failed role initialization remains failed')
        error_output = failed_ddl.stdout + failed_ddl.stderr + (temp / 'postgres.log').read_bytes()
        require(not any(value.encode() in error_output for value in PASSWORDS.values()),
                'password-bearing DDL failure is absent from client and server logs')
        with connect('gateway_bootstrap') as connection:
            require(connection.execute('SHOW log_min_error_statement').fetchone()[0] == 'error',
                    'session-only log protection leaves global diagnostics unchanged')
        # Confirm host login actually enforces SCRAM and rejects wrong passwords.
        for role in ROLES:
            with connect(role) as connection:
                require(connection.execute('SELECT current_user').fetchone()[0] == role, 'distinct authenticated identity')
            try:
                psycopg.connect(host='127.0.0.1', port=port, dbname='gateway', user=role,
                                 password='deliberately-wrong-synthetic-password', connect_timeout=3)
            except psycopg.OperationalError:
                pass
            else:
                raise RuntimeError('SCRAM rejected-password verification failed.')
        run(sys.executable, [str(ROOT / 'deploy/postgres/migrate.py')], extra={'DATABASE_URL': dsn('gateway_migrate')})
        # Repetition is safe and does not force-resolve/overwrite migration history.
        run(sys.executable, [str(ROOT / 'deploy/postgres/migrate.py')], extra={'DATABASE_URL': dsn('gateway_migrate')})
        with connect('gateway_bootstrap') as connection:
            actual_version = connection.execute('SHOW server_version').fetchone()[0]
            require(connection.execute("SELECT current_setting('data_directory')").fetchone()[0] == str(data), 'fixture ownership')
            require(connection.execute("SELECT count(*) FROM pg_authid WHERE rolname = ANY(%s) AND rolpassword LIKE 'SCRAM-SHA-256$%%'", (list(ROLES),)).fetchone()[0] == 4, 'SCRAM password storage')
        with connect('gateway_migrate') as connection:
            role_flags = connection.execute("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname", (list(ROLES[1:]),)).fetchall()
            require(all(not any(row[1:]) for row in role_flags), 'no privileged runtime/migration/backup flags')
            require(connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('public','gateway_ext') AND pg_get_userbyid(c.relowner) <> 'gateway_migrate'").fetchone()[0] == 0, 'migration owner owns all schema objects')
            migrations = connection.execute('SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL').fetchone()[0]
            require(migrations > 0, 'native migrations applied')
        for statement in (
            'CREATE TABLE public.forbidden_runtime(id int)',
            'CREATE SCHEMA forbidden_runtime',
            'CREATE TEMP TABLE forbidden_runtime(id int)',
            'ALTER TABLE gateway_ext.events ADD COLUMN forbidden text',
            'DROP TABLE gateway_ext.observations',
            'TRUNCATE gateway_ext.events',
            'SET ROLE gateway_migrate',
            'SET ROLE gateway_bootstrap',
            "CREATE ROLE forbidden_runtime LOGIN",
            "UPDATE _prisma_migrations SET checksum='forbidden' WHERE false",
            'SELECT * FROM pg_authid',
        ):
            denied('gateway_runtime', statement)
        with connect('gateway_runtime') as connection:
            connection.execute("INSERT INTO gateway_ext.pools(pool_id,payload) VALUES ('role-test','{}')")
            connection.execute("UPDATE gateway_ext.pools SET payload='{\"status\":\"available\"}' WHERE pool_id='role-test'")
            require(connection.execute("SELECT payload->>'status' FROM gateway_ext.pools WHERE pool_id='role-test'").fetchone()[0] == 'available', 'runtime read/write')
            connection.execute("DELETE FROM gateway_ext.pools WHERE pool_id='role-test'")
            connection.execute("INSERT INTO gateway_ext.events(payload) VALUES ('{\"event\":\"role_test\"}')")
        for statement in (
            "INSERT INTO gateway_ext.events(payload) VALUES ('{}')",
            "UPDATE gateway_ext.pools SET payload='{}'",
            'DELETE FROM gateway_ext.events',
            'TRUNCATE gateway_ext.events',
            'CREATE TABLE public.forbidden_backup(id int)',
            'SET ROLE gateway_migrate',
        ):
            denied('gateway_backup', statement)
        from gateway.state import PostgresState
        asyncio.run(PostgresState(dsn('gateway_runtime'), expected_runtime_role='gateway_runtime').initialize())
        from gateway.errors import GatewayError
        for role in ('gateway_migrate', 'gateway_bootstrap'):
            try:
                asyncio.run(PostgresState(dsn(role), expected_runtime_role='gateway_runtime').ready())
            except GatewayError as error:
                require(error.code == 'unsafe_database_role', 'elevated DSN rejected')
            else:
                raise RuntimeError('Elevated runtime DSN was not rejected.')
        # A later TEMP grant must also fail the live production-role guard.
        with connect('gateway_bootstrap') as connection:
            connection.execute('GRANT TEMP ON DATABASE gateway TO gateway_runtime')
        try:
            try:
                asyncio.run(PostgresState(dsn('gateway_runtime'), expected_runtime_role='gateway_runtime').ready())
            except GatewayError as error:
                require(error.code == 'unsafe_database_role', 'TEMP privilege drift rejected')
            else:
                raise RuntimeError('Runtime TEMP privilege drift was not rejected.')
        finally:
            with connect('gateway_bootstrap') as connection:
                connection.execute('REVOKE TEMP ON DATABASE gateway FROM gateway_runtime')
        asyncio.run(PostgresState(dsn('gateway_runtime'), expected_runtime_role='gateway_runtime').ready())
        from tests.mock_upstream import MockState, serve_mock
        from tests.verify_native_recovery import NativeProcess
        with serve_mock(MockState(capture_payloads=False)) as mock:
            policy = yaml.safe_load((ROOT / 'config/policy.mock.yaml').read_text())
            for deployment in policy['deployments']:
                deployment['api_base'] = mock.base_url
            policy_path = temp / 'policy.yaml'; policy_path.write_text(yaml.safe_dump(policy))
            with NativeProcess(policy_path, dsn('gateway_runtime'), temp / 'native', temp / 'state') as proxy:
                key = proxy.create_key('disposable-least-privilege')
                proxy.auth(key, True)
                response = proxy.generate(key)
                require(response.status_code == 200, 'native completion with runtime DML role')
                proxy.revoke(key)
                proxy.auth(key, False)
        archive = temp / 'gateway.dump'
        run('pg_dump', [*client, '-U', 'gateway_backup', '-d', 'gateway', '--format=custom',
                        '--no-owner', '--no-acl', '--file', str(archive)], role='gateway_backup')
        require(archive.stat().st_size > 0, 'read-only full database backup')
        # Test actual restore with nonsuperuser owner, not only dump generation.
        restored = 'gateway_roles_restored'
        run('createdb', [*client, '-U', 'gateway_bootstrap', restored], role='gateway_bootstrap')
        with connect('gateway_bootstrap', restored) as connection:
            connection.execute('REVOKE ALL ON DATABASE gateway_roles_restored FROM PUBLIC')
            connection.execute('GRANT CONNECT ON DATABASE gateway_roles_restored TO gateway_migrate, gateway_runtime, gateway_backup')
            connection.execute('GRANT CREATE ON DATABASE gateway_roles_restored TO gateway_migrate')
            connection.execute('ALTER SCHEMA public OWNER TO gateway_migrate')
        run('pg_restore', [*client, '-U', 'gateway_migrate', '-d', restored, '--no-owner', '--no-acl',
                           '--exit-on-error', '--single-transaction', str(archive)], role='gateway_migrate')
        with connect('gateway_migrate', restored) as connection:
            connection.execute((ROOT / 'deploy/postgres/30-grants.sql').read_text())
        with connect('gateway_runtime', restored) as connection:
            require(connection.execute('SELECT count(*) FROM _prisma_migrations').fetchone()[0] == migrations, 'restored migration history readable')
            connection.execute("INSERT INTO gateway_ext.events(payload) VALUES ('{\"event\":\"restored_role_test\"}')")
            require(not connection.execute("SELECT has_schema_privilege('public','CREATE')").fetchone()[0], 'restored runtime remains no-DDL')
        with connect('gateway_backup', restored) as connection:
            require(connection.execute('SELECT count(*) FROM gateway_ext.events').fetchone()[0] > 0, 'restored backup remains readable')
        evidence = {
            'status': 'passed', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'environment': 'disposable loopback PostgreSQL; no Docker, Mac or production deployment',
            'postgres_version': actual_version, 'native_migrations': migrations,
            'checks': ['four distinct SCRAM identities; wrong password rejected', 'failed password-bearing role DDL produces no credential logs; global logging unchanged', 'nonsuperuser migration owner; repeatable native deploy',
                       'runtime CRUD; CREATE/ALTER/DROP/TEMP/TRUNCATE/SET ROLE/role creation denied',
                       'runtime migration history mutation denied', 'runtime guard rejects elevated DSNs and TEMP privilege drift',
                       'native proxy startup, virtual-key creation/authentication/completion/revocation with runtime role',
                       'backup SELECT-only even with read-only preference disabled', 'backup-role pg_dump; migration-role pg_restore; grants restored'],
            'external_gates': ['authorized independent real secrets', 'Docker Compose build/secret ownership/startup',
                               'selected Mac/container acceptance', 'provider qualification and zero-billing verification'],
            'credentials_recorded': False,
        }
        destination = ROOT / args.evidence
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n')
        print('Disposable PostgreSQL least-privilege roles: passed.')
    finally:
        stopped = not started
        if started:
            stopped = subprocess.run([str(binaries['pg_ctl']), '-D', str(data), '-m', 'fast', 'stop'],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0
        if stopped:
            shutil.rmtree(temp)
        else:
            raise RuntimeError('Disposable server did not stop; temporary directory retained for inspection.')


if __name__ == '__main__':
    main()
