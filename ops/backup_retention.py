#!/usr/bin/env python3
"""Explicit opt-in backup lifecycle. Default preview never deletes or changes files."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from key_admin import private_parent
from gateway_ops import OpsError

MEMBERS = {'manifest.json', 'policy.yaml', 'database.dump'}
DIRECTORY = re.compile(r'\d{8}T\d{12}Z')


@contextmanager
def private_directory(path):
    with private_parent(path / 'unused', output=True) as (fd, _):
        yield fd


def inspect_backup(parent_fd, name, target):
    if not DIRECTORY.fullmatch(name):
        return None
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        attributes = os.fstat(fd)
        if attributes.st_uid != os.geteuid() or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise OpsError('Backup directory is not an owned private 0700 directory.')
        if set(os.listdir(fd)) != MEMBERS:
            raise OpsError('Backup has unknown, missing or incomplete files; retention refuses it.')
        values, identities = {}, {}
        for member in MEMBERS:
            child = os.open(member, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(child)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise OpsError('Backup member is not a private single-link owned regular file.')
                identities[member] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                with os.fdopen(os.dup(child), 'rb') as stream:
                    if member == 'manifest.json':
                        data = stream.read(128 * 1024 + 1)
                        if len(data) > 128 * 1024:
                            raise OpsError('Backup manifest is oversized.')
                        values[member] = json.loads(data)
                    else:
                        values[member] = hashlib.file_digest(stream, 'sha256').hexdigest()
            finally:
                os.close(child)
        manifest = values['manifest.json']
        if not isinstance(manifest, dict) or manifest.get('format') != 2 or manifest.get('managed_by') != 'gateway_ops':
            return None
        if manifest.get('target') != target or manifest.get('retention_managed') is not True:
            return None
        if manifest.get('files') != {member: values[member] for member in ('policy.yaml', 'database.dump')}:
            raise OpsError('Backup integrity failed; no retention deletion is permitted.')
        created = datetime.fromisoformat(manifest['created_at'])
        named = datetime.strptime(name, '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
        if created.utcoffset() is None or created.utcoffset().total_seconds() != 0 or abs((created - named).total_seconds()) > 60:
            raise OpsError('Backup creation time is not a valid UTC timestamp matching its directory.')
        return {'name': name, 'created_at': created, 'identities': identities}
    finally:
        os.close(fd)


def preview(root: Path, target: str, *, days: int = 30, keep: int = 2, now=None):
    if days < 1 or keep < 1:
        raise OpsError('Retention needs at least one day and must preserve at least one newest managed backup.')
    now = now or datetime.now(timezone.utc)
    candidates, ignored = [], []
    with private_directory(root) as parent:
        for name in os.listdir(parent):
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) or not DIRECTORY.fullmatch(name):
                ignored.append(name)
                continue
            value = inspect_backup(parent, name, target)
            if value is None:
                ignored.append(name)
            else:
                candidates.append(value)
    candidates.sort(key=lambda item: item['created_at'], reverse=True)
    selected = [item for item in candidates[keep:] if item['created_at'] < now - timedelta(days=days)]
    return selected, ignored


def remove_selected(root: Path, target: str, selected):
    removed = []
    with private_directory(root) as parent:
        # Revalidate every selected snapshot before deleting any file. A changed
        # file, unknown child or hardlink aborts the whole attempt at this stage.
        for item in selected:
            if inspect_backup(parent, item['name'], target) != item:
                raise OpsError('A selected backup changed since preview; deletion was refused.')
        for item in selected:
            fd = os.open(item['name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                # Remove completion marker first: interruption cannot leave an
                # incomplete dump falsely claiming to be a complete backup.
                for member in ('manifest.json', 'database.dump', 'policy.yaml'):
                    os.unlink(member, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rmdir(item['name'], dir_fd=parent)
            os.fsync(parent)
            removed.append(item['name'])
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--target', choices=['production', 'mock'], required=True)
    parser.add_argument('--older-than-days', type=int, default=30)
    parser.add_argument('--keep-newest', type=int, default=2)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--ack-permanent-delete', action='store_true', help='Explicitly approve irreversible removal of the selected managed backups')
    args = parser.parse_args(argv)
    try:
        if args.execute and not args.ack_permanent_delete:
            raise OpsError('Execution requires --ack-permanent-delete after reviewing these exact selected backups.')
        # Serialize deletion with releases/restores, without reading credentials.
        import gateway_ops
        selected, ignored = preview(args.root, args.target, days=args.older_than_days, keep=args.keep_newest)
        names = [item['name'] for item in selected]
        if args.execute:
            previous = gateway_ops.RUNTIME
            gateway_ops.RUNTIME = gateway_ops.ROOT / '.runtime' / args.target
            try:
                with gateway_ops.maintenance_lock():
                    removed = remove_selected(args.root, args.target, selected)
            finally:
                gateway_ops.RUNTIME = previous
            print(json.dumps({'mode': 'EXECUTED', 'removed': removed, 'ignored_count': len(ignored), 'journal_deleted': False}))
        else:
            print(json.dumps({'mode': 'DRY_RUN', 'executed': False, 'target': args.target, 'selected': names,
                              'ignored_count': len(ignored), 'keeps_at_least_newest': args.keep_newest,
                              'scope': 'format-2 backups explicitly created with --retention-managed only; no journals or manual backups'}, indent=2))
        return 0
    except (OpsError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'success': False, 'error': 'Backup retention failed or was not authorized. Inspect the private directory; no complete cleanup is claimed.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
