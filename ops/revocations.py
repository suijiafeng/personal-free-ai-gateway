"""Private append-only revocation intentions, separate from restorable databases.

Only native SHA-256 identifiers are stored; native APIs own authentication writes.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

MAX_BYTES = 16 * 1024 * 1024
ZERO = '0' * 64
HASH = re.compile(r'[0-9a-f]{64}')


class LedgerError(RuntimeError):
    pass


def key_hash(key: str) -> str:
    if not isinstance(key, str) or not re.fullmatch(r'sk-[!-~]{1,509}', key):
        raise LedgerError('Expected a native consumer key; no journal entry was written.')
    return hashlib.sha256(key.encode()).hexdigest()


def scope(profile: str, url: str) -> str:
    from urllib.parse import urlsplit
    if profile not in {'production', 'mock'}:
        raise LedgerError('An explicit production or mock journal target is required.')
    return f'{profile}:{urlsplit(url).port}'


def _hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Journal:
    def __init__(self, fd: int, target: str):
        self.fd, self.target, self.rows = fd, target, []
        self.persist_head = lambda checkpoint: None
        os.lseek(fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= MAX_BYTES:
            part = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        if len(data) > MAX_BYTES or not data or not data.endswith(b'\n'):
            raise LedgerError('Revocation journal is empty, truncated, or oversized; do not resume.')
        try:
            rows = [json.loads(line) for line in data.splitlines()]
        except (ValueError, RecursionError):
            raise LedgerError('Revocation journal is unreadable; do not resume.') from None
        previous = ZERO
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get('seq') != index or row.get('previous') != previous:
                raise LedgerError('Revocation journal sequence is invalid; do not resume.')
            if row.get('digest') != _hash({k: v for k, v in row.items() if k != 'digest'}) or row.get('target') != target:
                raise LedgerError('Revocation journal integrity or target mismatch; do not resume.')
            try:
                when = datetime.fromisoformat(row['at'])
                if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
                    raise ValueError
            except (KeyError, ValueError, TypeError, AttributeError):
                raise LedgerError('Revocation journal timestamp is invalid; do not resume.') from None
            fields = {'seq', 'previous', 'digest', 'target', 'at', 'kind'}
            if index == 0:
                if row.get('kind') != 'initialized' or not isinstance(row.get('journal_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', row['journal_id']):
                    raise LedgerError('Revocation journal header is invalid; do not resume.')
                fields.add('journal_id')
            else:
                if row.get('kind') not in {'intent', 'confirmed', 'reconciled'} or not isinstance(row.get('key_hash'), str) or not HASH.fullmatch(row['key_hash']):
                    raise LedgerError('Revocation journal record is invalid; do not resume.')
                if row['kind'] != 'intent' and row['key_hash'] not in self.keys:
                    raise LedgerError('Revocation journal confirmation lacks an intent; do not resume.')
                fields.add('key_hash')
            if set(row) != fields:
                raise LedgerError('Revocation journal has unsupported fields; do not resume.')
            self.rows.append(row)
            previous = row['digest']

    @property
    def keys(self) -> set[str]:
        return {row['key_hash'] for row in self.rows if row['kind'] == 'intent'}

    def checkpoint(self) -> dict:
        row = self.rows[-1]
        return {'journal_id': self.rows[0]['journal_id'], 'seq': row['seq'], 'digest': row['digest'], 'target': self.target}

    def require_checkpoint(self, checkpoint: dict):
        if (not isinstance(checkpoint, dict) or checkpoint.get('journal_id') != self.rows[0]['journal_id']
                or checkpoint.get('target') != self.target or type(checkpoint.get('seq')) is not int
                or not 0 <= checkpoint['seq'] < len(self.rows)
                or self.rows[checkpoint['seq']]['digest'] != checkpoint.get('digest')):
            raise LedgerError('Current revocation journal does not extend this backup checkpoint; do not restore or resume.')

    def append(self, kind: str, token_hash: str):
        if kind not in {'intent', 'confirmed', 'reconciled'} or not HASH.fullmatch(token_hash):
            raise LedgerError('Unsupported revocation journal event.')
        if kind != 'intent' and token_hash not in self.keys:
            raise LedgerError('Revocation confirmation requires a durable intent first.')
        row = {'seq': len(self.rows), 'previous': self.rows[-1]['digest'], 'target': self.target,
               'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, 'key_hash': token_hash}
        row['digest'] = _hash(row)
        encoded = (json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if os.fstat(self.fd).st_size + len(encoded) > MAX_BYTES:
            raise LedgerError('Revocation journal size limit reached; retain all history and review offline.')
        os.lseek(self.fd, 0, os.SEEK_END)
        offset = 0
        while offset < len(encoded):
            written = os.write(self.fd, encoded[offset:])
            if written <= 0:
                raise LedgerError('Revocation journal write was incomplete; do not resume.')
            offset += written
        os.fsync(self.fd)
        self.rows.append(row)
        self.persist_head(self.checkpoint())

    def revoke(self, token_hash: str, request):
        self.append('intent', token_hash)
        request('/key/delete', 'POST', {'keys': [token_hash]})
        self._require_absent(token_hash, request)
        self.append('confirmed', token_hash)

    def _require_absent(self, token_hash: str, request):
        info = request('/key/info?key=' + token_hash).get('info')
        if not isinstance(info, dict) or info.get('status') not in {'deleted', 'absent'}:
            raise LedgerError('Native key revocation was not verified; journal intent remains pending.')

    def reconcile(self, request) -> dict:
        count = 0
        for token_hash in sorted(self.keys):
            info = request('/key/info?key=' + token_hash).get('info')
            if not isinstance(info, dict) or not isinstance(info.get('status'), str):
                raise LedgerError('Native key state is unknown; do not resume.')
            if info['status'] not in {'deleted', 'absent'}:
                request('/key/delete', 'POST', {'keys': [token_hash]})
                self._require_absent(token_hash, request)
                self.append('reconciled', token_hash)
                count += 1
        return {'revocation_intents_checked': len(self.keys), 'restored_keys_revoked': count}


@contextmanager
def locked(path: Path, target: str, *, initialize: bool = False):
    from key_admin import private_parent
    with private_parent(path, output=True) as (directory, name):
        lock = os.open(name + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        fd = None
        try:
            _private_descriptor(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerError('Another key or maintenance operation holds the revocation journal lock.') from None
            if initialize:
                try:
                    os.stat(name + ".head", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise LedgerError("Revocation journal head already exists; initialization never replaces prior history.")
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                row = {'seq': 0, 'previous': ZERO, 'target': target, 'at': datetime.now(timezone.utc).isoformat(),
                       'kind': 'initialized', 'journal_id': uuid.uuid4().hex}
                row['digest'] = _hash(row)
                with os.fdopen(os.dup(fd), 'w') as output:
                    output.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(directory)
            else:
                try:
                    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    raise LedgerError('Revocation journal is missing. Explicitly initialize it after reviewing historical revocations; do not resume.') from None
            _private_descriptor(fd)
            journal = Journal(fd, target)
            def persist_head(checkpoint):
                temporary = name + ".head." + uuid.uuid4().hex
                head_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(head_fd, "w") as output:
                        json.dump(checkpoint, output, sort_keys=True)
                        output.write("\n")
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, name + ".head", src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            if not initialize:
                try:
                    head_fd = os.open(name + ".head", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    raise LedgerError("Revocation journal high-water mark is missing; do not resume.") from None
                try:
                    _private_descriptor(head_fd)
                    head = os.read(head_fd, 4097)
                    if len(head) > 4096:
                        raise LedgerError("Revocation journal high-water mark is invalid; do not resume.")
                    try:
                        journal.require_checkpoint(json.loads(head))
                    except (ValueError, TypeError):
                        raise LedgerError("Revocation journal high-water mark is invalid; do not resume.") from None
                finally:
                    os.close(head_fd)
            journal.persist_head = persist_head
            # If a crash followed fsync(journal) but preceded fsync(head), replay
            # the intact prefix and advance the head. A shortened tail is refused.
            if initialize or json.loads(head) != journal.checkpoint():
                persist_head(journal.checkpoint())
            yield journal
        finally:
            if fd is not None:
                os.close(fd)
            os.close(lock)


def _private_descriptor(fd):
    value = os.fstat(fd)
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600):
        raise LedgerError('Revocation journal and lock must be owned single-link regular files with mode 0600.')
