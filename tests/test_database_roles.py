"""Static deployment boundaries; genuine PostgreSQL checks use verify_database_roles.py."""
from pathlib import Path
import ast
import os
import shutil
import subprocess

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_database_credentials_are_isolated_by_service():
    compose = yaml.safe_load((ROOT / 'deploy/compose.yaml').read_text())
    services = compose['services']
    assert services['postgres']['environment']['POSTGRES_USER'] == 'gateway_bootstrap'
    assert 'POSTGRES_PASSWORD' not in services['postgres']['environment']
    assert services['postgres']['environment']['POSTGRES_PASSWORD_FILE'] == '/run/secrets/postgres_bootstrap_password'
    assert len(services['postgres']['secrets']) == 4
    assert set(services['gateway']['environment']).isdisjoint({'MIGRATION_DATABASE_URL', 'POSTGRES_PASSWORD', 'POSTGRES_PASSWORD_FILE'})
    assert services['gateway']['environment']['DATABASE_URL'].startswith('${DATABASE_URL:')
    assert services['migrate']['environment']['DATABASE_URL'].startswith('${MIGRATION_DATABASE_URL:')
    assert not services['gateway'].get('secrets')
    assert set(services['migrate']['environment']).isdisjoint({'LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY'})
    assert services['gateway']['depends_on']['migrate']['condition'] == 'service_completed_successfully'
    assert services['migrate']['entrypoint'] == ['python', '/app/deploy/postgres/migrate.py']
    assert services['migrate']['command'] == []
    assert services['migrate']['networks'] == ['backend']
    assert services['postgres']['networks'] == ['backend']
    assert compose['networks']['backend']['internal'] is True


def test_secret_example_contains_no_values_and_fixed_roles():
    values = dict(line.split('=', 1) for line in (ROOT / '.env.example').read_text().splitlines() if line and not line.startswith('#'))
    for name in ('POSTGRES_BOOTSTRAP_PASSWORD_FILE', 'POSTGRES_MIGRATION_PASSWORD_FILE',
                 'POSTGRES_RUNTIME_PASSWORD_FILE', 'POSTGRES_BACKUP_PASSWORD_FILE',
                 'DATABASE_URL', 'MIGRATION_DATABASE_URL', 'LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY'):
        assert values[name] == ''
    assert values['DATABASE_NAME'] == 'gateway'
    assert values['MAINTENANCE_DATABASE_USER'] == 'gateway_migrate'
    assert values['BACKUP_DATABASE_USER'] == 'gateway_backup'


def test_runtime_initialization_contains_no_ddl():
    module = ast.parse((ROOT / 'gateway/state.py').read_text())
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'PostgresState')
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'initialize')
    constants = [node.value for node in ast.walk(method) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not any(value.upper().startswith(('CREATE ', 'ALTER ', 'DROP ', 'GRANT ')) for value in constants)
    assert 'DISABLE_SCHEMA_UPDATE' in (ROOT / 'gateway/bootstrap.py').read_text()


def test_migration_does_not_force_schema_or_repair_history():
    source = (ROOT / 'deploy/postgres/migrate.py').read_text()
    assert "'-m', 'prisma', 'migrate', 'deploy'" in source
    assert "'db', 'push'" not in source
    assert 'accept-data-loss' not in source
    assert 'migrate resolve' not in source
    assert "env.pop(name, None)" in source
    assert '--grants-only' in source


def test_secret_bridge_is_only_in_database_service():
    services = yaml.safe_load((ROOT / 'deploy/compose.yaml').read_text())['services']
    postgres = services['postgres']
    assert postgres['entrypoint'] == ['bash', '/gateway-postgres-entrypoint.sh']
    assert postgres['command'] == ['postgres']
    assert '/run/gateway-init-secrets:rw,noexec,nosuid,size=64k,mode=0700' in postgres['tmpfs']
    assert './postgres/entrypoint.sh:/gateway-postgres-entrypoint.sh:ro' in postgres['volumes']
    for name in ('gateway', 'migrate'):
        assert not any('gateway-init-secrets' in str(value) for value in services[name].values())
    for name in ('entrypoint.sh', '10-roles.sh'):
        assert (ROOT / 'deploy/postgres' / name).stat().st_mode & 0o777 == 0o755
    initializer = (ROOT / 'deploy/postgres/10-roles.sh').read_text()
    assert '--file /docker-entrypoint-initdb.d/roles.psql' in initializer
    assert '$(dirname "$0")' not in initializer
    assert 'cat /run/secrets/' not in initializer
    assert initializer.index('rm -f -- /run/gateway-init-secrets/') < initializer.index('psql --no-psqlrc')
    assert '>/dev/null 2>&1' in initializer
    bridge = (ROOT / 'deploy/postgres/entrypoint.sh').read_text()
    assert '! -s "${PGDATA:?PGDATA is required}/PG_VERSION"' in bridge
    assert 'export GATEWAY_' not in bridge
    assert 'exec /usr/local/bin/docker-entrypoint.sh "$@"' in bridge
    ddl = (ROOT / 'deploy/postgres/roles.psql').read_text()
    assert ddl.index("SET log_min_error_statement = 'panic'") < ddl.index('\nCREATE ROLE')


@pytest.mark.parametrize('case', ['success', 'non_tmpfs', 'symlink_target', 'public_secret', 'missing_secret', 'source_symlink'])
def test_root_namespace_tmpfs_secret_bridge(tmp_path, case):
    """Actual private tmpfs/root-userns shell test, not a Docker/cross-UID pass.

    Only the invoking host UID can be mapped in this environment. Owner 0 is
    deliberate; a second postgres UID and the official image remain external.
    """
    if not shutil.which('unshare') or not shutil.which('mount'):
        pytest.skip('Root user namespace/mount tools unavailable; bridge not exercised.')
    probe = subprocess.run(['unshare', '--user', '--map-root-user', '--mount', 'true'],
                           capture_output=True)
    if probe.returncode:
        pytest.skip('Root user namespace unavailable; bridge not exercised.')
    source = tmp_path / 'source'; source.mkdir(mode=0o700)
    stage = tmp_path / 'stage'; stage.mkdir(mode=0o700)
    for name in ('migration', 'runtime', 'backup'):
        file = source / ('postgres_' + name + '_password')
        file.write_text('synthetic-tmpfs-fixture-' + name)
        file.chmod(0o600)
    if case == 'public_secret':
        (source / 'postgres_runtime_password').chmod(0o644)
    elif case == 'missing_secret':
        (source / 'postgres_runtime_password').unlink()
    elif case == 'source_symlink':
        (source / 'postgres_runtime_password').unlink()
        (source / 'postgres_runtime_password').symlink_to(source / 'postgres_backup_password')
    script = r'''set -eu
source "$1"
if [ "$4" != non_tmpfs ]; then mount -t tmpfs -o mode=0700,size=64k tmpfs "$2"; fi
stage="$2"
if [ "$4" = symlink_target ]; then ln -s "$2" "$2-link"; stage="$2-link"; fi
# Do not wrap the function in an if/! construct: bash disables errexit inside
# conditional functions, unlike the real unconditional production call.
set +e
stage_init_secrets "$stage" "$3" 0 0
status=$?
set -e
if [ "$4" = success ]; then
  [ "$status" = 0 ]
  [ "$(stat -c %a "$2")" = 700 ]
  for name in migration runtime backup; do
    [ "$(stat -c %a "$2/postgres_${name}_password")" = 400 ]
    [ "$(stat -c %u "$2/postgres_${name}_password")" = 0 ]
    cmp "$3/postgres_${name}_password" "$2/postgres_${name}_password"
    [ "$(stat -c %a "$3/postgres_${name}_password")" = 600 ]
  done
else
  [ "$status" != 0 ]
  [ -z "$(find "$2" -mindepth 1 -maxdepth 1 -print -quit)" ]
fi
if [ "$4" != non_tmpfs ]; then umount "$2"; fi
'''
    result = subprocess.run(['unshare', '--user', '--map-root-user', '--mount', 'bash', '-c', script,
                             'bridge-fixture', str(ROOT / 'deploy/postgres/entrypoint.sh'),
                             str(stage), str(source), case], capture_output=True, timeout=30)
    assert result.returncode == 0, 'Private tmpfs bridge fixture failed; secret-bearing output suppressed.'
