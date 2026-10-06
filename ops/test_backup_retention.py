"""Real temporary backup files only; never touches a production archive."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).parent))
import backup_retention as retention

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def snapshot(root, days, *, managed=True, target='mock'):
    created = NOW - timedelta(days=days)
    folder = root / created.strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(mode=0o700)
    files = {}
    for name, contents in [('policy.yaml', b'no-secret-fixture'), ('database.dump', b'synthetic-dump')]:
        path = folder / name
        path.write_bytes(contents)
        path.chmod(0o600)
        files[name] = hashlib.sha256(contents).hexdigest()
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps({'format': 2, 'managed_by': 'gateway_ops', 'retention_managed': managed,
                                    'target': target, 'created_at': created.isoformat(), 'files': files}))
    manifest.chmod(0o600)
    return folder


def test_preview_is_nonmutating_and_preserves_newest_manual_and_other_target(tmp_path):
    newest = snapshot(tmp_path, 40)
    old = snapshot(tmp_path, 80)
    manual = snapshot(tmp_path, 90, managed=False)
    other = snapshot(tmp_path, 100, target='production')
    before = sorted(str(p) for p in tmp_path.rglob('*'))
    selected, ignored = retention.preview(tmp_path, 'mock', days=30, keep=1, now=NOW)
    assert [item['name'] for item in selected] == [old.name]
    assert set(ignored) == {manual.name, other.name}
    assert sorted(str(p) for p in tmp_path.rglob('*')) == before
    assert newest.exists()


def test_only_explicit_selected_fixture_is_removed(tmp_path):
    newest = snapshot(tmp_path, 1)
    old = snapshot(tmp_path, 80)
    selected, _ = retention.preview(tmp_path, 'mock', days=30, keep=1, now=NOW)
    assert retention.remove_selected(tmp_path, 'mock', selected) == [old.name]
    assert newest.exists() and not old.exists()


@pytest.mark.parametrize('change', ['hash', 'unknown_file', 'symlink', 'hardlink', 'mode'])
def test_changed_or_unsafe_backup_prevents_any_deletion(tmp_path, change):
    newest = snapshot(tmp_path, 1)
    old = snapshot(tmp_path, 80)
    selected, _ = retention.preview(tmp_path, 'mock', days=30, keep=1, now=NOW)
    dump = old / 'database.dump'
    if change == 'hash':
        dump.write_bytes(b'changed')
    elif change == 'unknown_file':
        (old / 'manual-notes').write_text('retain')
    elif change == 'symlink':
        dump.unlink()
        dump.symlink_to(newest / 'database.dump')
    elif change == 'hardlink':
        import os
        os.link(dump, tmp_path / 'other-link')
    else:
        dump.chmod(0o644)
    with pytest.raises((retention.OpsError, OSError)):
        retention.remove_selected(tmp_path, 'mock', selected)
    assert old.exists() and (old / 'manifest.json').exists()


def test_missing_completion_manifest_is_not_deleted(tmp_path):
    old = snapshot(tmp_path, 80)
    (old / 'manifest.json').unlink()
    with pytest.raises(retention.OpsError):
        retention.preview(tmp_path, 'mock', now=NOW)
    assert old.exists()


def test_actual_execution_requires_its_own_acknowledgement(tmp_path, capsys):
    snapshot(tmp_path, 80)
    assert retention.main(['--root', str(tmp_path), '--target', 'mock', '--execute']) == 1
    assert 'success' in capsys.readouterr().err
    assert len(list(tmp_path.iterdir())) == 1
