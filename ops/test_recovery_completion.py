"""Recovery guard unit tests: actual journal files, synthetic/stub HTTP only."""
from argparse import Namespace
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gateway_ops as ops
import key_admin
import revocations as ledger
from recovery_guards import RecoveryGuardError, require_compatible, require_migrations

KEY = 'sk-disposable-consumer-not-real'
MASTER = 'sk-disposable-administrator-not-real-0001'
TOKEN = ledger.key_hash(KEY)
MIGRATIONS = {'finished': 189, 'unfinished': 0, 'signature': 'a' * 64}


@pytest.fixture
def journal_path(tmp_path):
    path = tmp_path / 'revocations.jsonl'
    with ledger.locked(path, 'mock:4101', initialize=True):
        pass
    return path


def test_intent_is_fsynced_before_native_request_and_contains_no_plaintext(journal_path):
    def request(path, method='GET', payload=None):
        rows = journal_path.read_text()
        assert '"kind":"intent"' in rows
        assert KEY not in rows
        if method == 'POST':
            assert payload == {'keys': [TOKEN]}
            return {'deleted_keys': [TOKEN]}
        return {'info': {'status': 'deleted'}}
    with ledger.locked(journal_path, 'mock:4101') as journal:
        journal.revoke(TOKEN, request)
        assert journal.rows[-1]['kind'] == 'confirmed'
    assert journal_path.stat().st_mode & 0o777 == 0o600


def test_network_unknown_retains_intent_then_replays_after_restart(journal_path):
    def fail(*args):
        raise RuntimeError('response unknown')
    with ledger.locked(journal_path, 'mock:4101') as journal:
        with pytest.raises(RuntimeError):
            journal.revoke(TOKEN, fail)
    active = True
    def request(path, method='GET', payload=None):
        nonlocal active
        if method == 'POST':
            active = False
            return {}
        return {'info': {'status': 'active' if active else 'deleted'}}
    with ledger.locked(journal_path, 'mock:4101') as journal:
        assert journal.keys == {TOKEN}
        assert journal.reconcile(request) == {'revocation_intents_checked': 1, 'restored_keys_revoked': 1}
    assert not active


def test_snapshot_does_not_replace_newer_revocation_history(journal_path):
    with ledger.locked(journal_path, 'mock:4101') as journal:
        checkpoint = journal.checkpoint()
        journal.append('intent', TOKEN)
    with ledger.locked(journal_path, 'mock:4101') as journal:
        journal.require_checkpoint(checkpoint)
        assert TOKEN in journal.keys
        with pytest.raises(ledger.LedgerError):
            journal.require_checkpoint({**checkpoint, 'journal_id': 'f' * 32})


@pytest.mark.parametrize('damage', ['tail', 'partial', 'content', 'head_missing', 'head_corrupt'])
def test_corruption_and_complete_tail_truncation_fail_closed(journal_path, damage):
    with ledger.locked(journal_path, 'mock:4101') as journal:
        journal.append('intent', TOKEN)
    if damage == 'tail':
        journal_path.write_bytes(journal_path.read_bytes().splitlines(keepends=True)[0])
    elif damage == 'partial':
        journal_path.write_bytes(journal_path.read_bytes()[:-8])
    elif damage == 'content':
        journal_path.write_text(journal_path.read_text().replace(TOKEN, '0' * 64))
    elif damage == 'head_missing':
        journal_path.with_name(journal_path.name + '.head').unlink()
    else:
        journal_path.with_name(journal_path.name + '.head').write_text('bad json')
    with pytest.raises(ledger.LedgerError):
        with ledger.locked(journal_path, 'mock:4101'):
            pass


def test_profile_and_port_scope_mismatch_refused(journal_path):
    for target in ('production:4101', 'mock:4001'):
        with pytest.raises(ledger.LedgerError):
            with ledger.locked(journal_path, target):
                pass


def test_initialize_never_overwrites_existing_history(journal_path):
    original = journal_path.read_bytes()
    with pytest.raises((ledger.LedgerError, key_admin.OpsError)):
        with ledger.locked(journal_path, 'mock:4101', initialize=True):
            pass
    assert journal_path.read_bytes() == original


def test_missing_journal_never_initializes_implicitly(tmp_path):
    path = tmp_path / 'missing.jsonl'
    with pytest.raises(ledger.LedgerError):
        with ledger.locked(path, 'mock:4101'):
            pass
    assert not path.exists()


def test_concurrent_key_and_maintenance_operations_refused(journal_path):
    with ledger.locked(journal_path, 'mock:4101'):
        with pytest.raises(ledger.LedgerError, match='holds'):
            with ledger.locked(journal_path, 'mock:4101'):
                pass


@pytest.mark.parametrize('change', ['symlink', 'hardlink', 'public'])
def test_journal_path_and_permissions_are_private(journal_path, change):
    if change == 'symlink':
        link = journal_path.with_name('link.jsonl')
        link.symlink_to(journal_path)
        path = link
    else:
        path = journal_path
        if change == 'hardlink':
            os.link(path, path.with_suffix('.copy'))
        else:
            path.chmod(0o644)
    with pytest.raises((ledger.LedgerError, key_admin.OpsError)):
        with ledger.locked(path, 'mock:4101'):
            pass


def test_unknown_native_state_never_counts_as_reconciled(journal_path):
    with ledger.locked(journal_path, 'mock:4101') as journal:
        journal.append('intent', TOKEN)
        for response in ({}, {'info': None}, {'info': {'blocked': True}}):
            with pytest.raises(ledger.LedgerError):
                journal.reconcile(lambda *args: response)


def test_http_404_only_counts_as_absent_in_explicit_key_info_check():
    import urllib.error
    with patch.object(key_admin.urllib.request, 'build_opener') as opener:
        opener.return_value.open.side_effect = urllib.error.HTTPError('http://127.0.0.1:4101/key/info', 404, 'missing', {}, None)
        assert key_admin.private_admin('http://127.0.0.1:4101', MASTER, '/key/info?key=' + TOKEN, allow_missing=True)['info']['status'] == 'absent'
        with pytest.raises(key_admin.OpsError):
            key_admin.private_admin('http://127.0.0.1:4101', MASTER, '/gateway/status', allow_missing=True)


def test_old_key_503_is_not_a_verified_revocation():
    import urllib.error
    with patch.object(key_admin.urllib.request, 'build_opener') as opener:
        for status in (404, 429, 500, 503):
            opener.return_value.open.side_effect = urllib.error.HTTPError('http://127.0.0.1:4100/v1/models', status, 'error', {}, None)
            with pytest.raises(key_admin.OpsError):
                key_admin.verify_key_rejected('http://127.0.0.1:4100', KEY)


@pytest.mark.parametrize('value', [None, {}, {**MIGRATIONS, 'unfinished': 1}, {**MIGRATIONS, 'finished': 0},
                                    {**MIGRATIONS, 'unfinished': False}, {**MIGRATIONS, 'signature': 'unknown'}])
def test_incomplete_migration_baseline_blocks_operations(value):
    with pytest.raises(RecoveryGuardError):
        require_migrations(value)


def test_migration_signature_changes_cannot_be_acknowledged_away():
    with pytest.raises(RecoveryGuardError):
        require_compatible(MIGRATIONS, {**MIGRATIONS, 'signature': 'b' * 64})


def target_args(path, profile='mock'):
    return Namespace(target=profile, env_file=path, admin_url=None, docker_host='unix:///var/run/docker.sock')


@pytest.fixture
def selected_env(tmp_path):
    path = tmp_path / '.env.mock'
    path.write_text(f'LITELLM_MASTER_KEY={MASTER}\nADMIN_PORT=4101\nDATABASE_NAME=gateway\nBACKUP_DATABASE_USER=gateway_backup\nMAINTENANCE_DATABASE_USER=gateway_migrate\n')
    path.chmod(0o600)
    return path


def test_mock_and_production_use_distinct_projects_runtime_and_files(selected_env, tmp_path):
    with patch.object(ops, 'ROOT', tmp_path), patch.dict(os.environ, {'LITELLM_MASTER_KEY': 'bad', 'ADMIN_PORT': '4001', 'COMPOSE_FILE': 'evil', 'DOCKER_HOST': 'ssh://elsewhere'}):
        for target, project in [('mock', 'gateway-mock'), ('production', 'personal-free-ai-gateway')]:
            with ops.selected_target(target_args(selected_env, target)):
                env = ops.environment()
                assert env['LITELLM_MASTER_KEY'] == MASTER and env['ADMIN_PORT'] == '4101'
                assert 'COMPOSE_FILE' not in env and 'DOCKER_HOST' not in env
                assert ops.RUNTIME == tmp_path / '.runtime' / target
                with patch.object(ops, 'docker') as call:
                    ops.compose('ps')
                    command = call.call_args.args
                    assert command[command.index('-p') + 1] == project
                    assert (str(tmp_path / 'deploy/compose.mock.yaml') in command) is (target == 'mock')


def test_live_execution_requires_explicit_target_before_any_network():
    with patch.object(ops, 'admin', side_effect=AssertionError('network')), contextlib.redirect_stderr(io.StringIO()):
        assert ops.main(['status']) == 1


def test_postgres_uses_selected_least_privilege_roles_and_secret_files(selected_env):
    with ops.selected_target(target_args(selected_env)):
        for role, name, filename in [('backup', 'gateway_backup', 'postgres_backup_password'), ('maintenance', 'gateway_migrate', 'postgres_migration_password')]:
            with patch.object(ops, 'compose') as call:
                ops.postgres(role, 'psql', '-Atc', 'SELECT 1')
                command = call.call_args.args
                assert command[command.index('-U') + 1] == name
                assert command[command.index('-h') + 1] == '127.0.0.1'
                assert '/run/secrets/' + filename in command
                assert MASTER not in str(command)


def test_legacy_backup_can_never_restore_automatically():
    with pytest.raises(ops.OpsError, match='Legacy'):
        ops.require_restore_compatible({'format': 1})


def test_new_restore_rejects_image_mismatch(journal_path):
    with ledger.locked(journal_path, 'mock:4101') as journal, patch.object(ops, '_JOURNAL', journal), patch.object(ops, 'migration_state', return_value=MIGRATIONS), patch.object(ops, 'effective_stack', return_value={'fixture': True}), patch.object(ops, 'gateway_image_id', return_value='sha256:' + 'b' * 64):
        with pytest.raises(ops.OpsError, match='image differs'):
            ops.require_restore_compatible({'format': 2, 'migrations': MIGRATIONS, 'gateway_image_id': 'sha256:' + 'a' * 64,
                                           'revocation_checkpoint': journal.checkpoint(), 'stack_identity': {'fixture': True}})


def egress_args(path):
    args = target_args(path, 'production')
    args.reviewed_egress = True
    args.provider = ['gemini', 'groq']
    return args


def test_production_uses_all_and_only_explicit_reviewed_overlays(selected_env):
    with ops.selected_target(egress_args(selected_env)), patch.object(ops, 'docker') as call:
        ops.compose('up', '-d', '--no-deps', 'gateway')
        command = call.call_args.args
        files = [command[index + 1] for index, value in enumerate(command) if value == '-f']
        assert files == [str(ops.ROOT / name) for name in (
            'deploy/compose.yaml', 'deploy/compose.egress.yaml', 'deploy/compose.groq.yaml', 'deploy/compose.gemini.yaml')]
        assert not any('mock' in name for name in files)


def test_mock_cannot_mix_egress_flags_even_in_a_dry_run(capsys):
    assert ops.main(['backup', '--target', 'mock', '--reviewed-egress', '--provider', 'groq']) == 1
    assert 'mock cannot mix' in capsys.readouterr().err


def test_enabled_production_provider_cannot_fall_back_to_base_only(selected_env):
    with ops.selected_target(target_args(selected_env, 'production')):
        with pytest.raises(ops.OpsError, match='exactly match'):
            ops.validate_provider_selection({'candidates': [{'enabled': True, 'provider': 'groq'}]})


def test_provider_set_change_is_rejected_before_any_mutation(selected_env):
    from subprocess import CompletedProcess
    args = egress_args(selected_env)
    args.provider = ['groq']
    with ops.selected_target(args), patch.object(ops.subprocess, 'run', return_value=CompletedProcess([], 0, '["gemini"]', '')):
        with pytest.raises(ops.OpsError, match='separate reviewed'):
            ops.validate_provider_selection({'candidates': [{'enabled': True, 'provider': 'groq'}]}, Path('candidate.yaml'))


def test_egress_acl_must_match_exact_selected_providers(selected_env, tmp_path):
    args = egress_args(selected_env)
    args.provider = ['groq']
    (tmp_path / 'deploy/egress').mkdir(parents=True)
    (tmp_path / 'deploy/egress/approved-hosts.txt').write_text('api.groq.com\nextra.invalid\n')
    with ops.selected_target(args), patch.object(ops, 'ROOT', tmp_path):
        with pytest.raises(ops.OpsError, match='ACL'):
            ops.validate_provider_selection({'candidates': [{'enabled': True, 'provider': 'groq'}]})


def test_service_config_hash_and_gateway_overlay_provenance_are_verified(selected_env):
    from subprocess import CompletedProcess
    service_ids = {'gateway': '1' * 64, 'postgres': '2' * 64, 'ingress': '3' * 64, 'egress': '4' * 64}
    args = egress_args(selected_env)
    args.provider = ['groq']
    with ops.selected_target(args):
        stack_files = ','.join(str((ops.ROOT / name).resolve()) for name in ops.compose_files())
        def compose(*command, **kwargs):
            if command[0] == 'config':
                return CompletedProcess([], 0, ''.join(name + ' ' + 'a' * 64 + '\n' for name in service_ids), '')
            return CompletedProcess([], 0, service_ids[command[-1]] + '\n', '')
        def docker(*command, **kwargs):
            service = next(name for name, identity in service_ids.items() if identity == command[-1])
            return CompletedProcess([], 0, json.dumps({'com.docker.compose.project': 'personal-free-ai-gateway',
                'com.docker.compose.service': service, 'com.docker.compose.config-hash': 'a' * 64,
                'com.docker.compose.project.config_files': stack_files}), '')
        with patch.object(ops, 'compose', side_effect=compose), patch.object(ops, 'docker', side_effect=docker):
            assert set(ops.assert_running_stack()) == set(service_ids)
        with patch.object(ops, 'compose', side_effect=compose), patch.object(ops, 'docker', return_value=CompletedProcess([], 0, '{}', '')):
            with pytest.raises(ops.OpsError, match='stack differs'):
                ops.assert_running_stack()


def test_effective_stack_ignores_only_policy_release_path(selected_env):
    from subprocess import CompletedProcess
    import copy
    base = {'name': 'gateway-mock', 'services': {'gateway': {'image': 'fixed', 'environment': {'MASTER': MASTER},
        'volumes': [{'type': 'bind', 'source': '/old/policy', 'target': '/app/config', 'read_only': True}]}}}
    with ops.selected_target(target_args(selected_env)):
        with patch.object(ops, 'compose', return_value=CompletedProcess([], 0, json.dumps(base), '')):
            old = ops.effective_stack()
        changed = copy.deepcopy(base)
        changed['services']['gateway']['volumes'][0]['source'] = '/new/immutable-policy'
        with patch.object(ops, 'compose', return_value=CompletedProcess([], 0, json.dumps(changed), '')):
            assert ops.effective_stack() == old
        changed['services']['gateway']['environment']['MASTER'] = 'different'
        with patch.object(ops, 'compose', return_value=CompletedProcess([], 0, json.dumps(changed), '')):
            assert ops.effective_stack() != old
        assert MASTER not in json.dumps(old)


def test_cross_overlay_restore_is_refused(journal_path):
    with ledger.locked(journal_path, 'mock:4101') as journal, patch.object(ops, '_JOURNAL', journal), patch.object(ops, 'effective_stack', return_value={'current': 'egress'}):
        with pytest.raises(ops.OpsError, match='cross-stack'):
            ops.require_restore_compatible({'format': 2, 'revocation_checkpoint': journal.checkpoint(),
                                           'stack_identity': {'current': 'base-only'}})


def test_actual_maintenance_cli_sanitizes_shared_helper_errors(tmp_path):
    import subprocess
    env_file = tmp_path / 'private.env'
    env_file.write_text('LITELLM_MASTER_KEY=sk-short\nADMIN_PORT=4101\n')
    env_file.chmod(0o600)
    result = subprocess.run([sys.executable, str(ops.ROOT / 'ops/gateway_ops.py'), 'status',
                             '--target', 'mock', '--env-file', str(env_file)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stderr)['success'] is False
    assert 'Traceback' not in result.stderr and 'sk-short' not in result.stderr
