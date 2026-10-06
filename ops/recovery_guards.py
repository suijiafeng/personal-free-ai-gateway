"""Read-only schema identity checks shared by Docker maintenance and native tests."""
import json
import re

MIGRATION_SQL = '''SELECT json_build_object(
'finished', count(*) FILTER (WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL),
'unfinished', count(*) FILTER (WHERE finished_at IS NULL AND rolled_back_at IS NULL),
'signature', encode(sha256(convert_to(coalesce(string_agg(migration_name || ':' || checksum, ','
ORDER BY migration_name COLLATE "C") FILTER (WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL), ''), 'UTF8')), 'hex'))
FROM public."_prisma_migrations"'''


class RecoveryGuardError(RuntimeError):
    pass


def require_migrations(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RecoveryGuardError('Migration state is unreadable; do not activate or resume.') from None
    if (not isinstance(value, dict) or type(value.get('finished')) is not int or value['finished'] <= 0
            or type(value.get('unfinished')) is not int or value['unfinished'] != 0
            or not isinstance(value.get('signature'), str) or not re.fullmatch('[0-9a-f]{64}', value['signature'])):
        raise RecoveryGuardError('Database has incomplete or missing migrations; do not activate or resume.')
    return value


def migration_summary(connection):
    return require_migrations(connection.execute(MIGRATION_SQL).fetchone()[0])


def require_compatible(current, saved):
    if require_migrations(current) != require_migrations(saved):
        raise RecoveryGuardError('Migration baseline differs; automatic cross-schema rollback/restore is refused.')
