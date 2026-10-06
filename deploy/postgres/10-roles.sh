#!/bin/sh
# First-volume bootstrap only. Never echo credentials or run with shell tracing.
set -eu
[ "${POSTGRES_USER:-}" = gateway_bootstrap ] && [ "${POSTGRES_DB:-}" = gateway ] || {
  echo 'Expected dedicated gateway_bootstrap and gateway database.' >&2; exit 1;
}
export GATEWAY_MIGRATION_PASSWORD="$(cat /run/gateway-init-secrets/postgres_migration_password)"
export GATEWAY_RUNTIME_PASSWORD="$(cat /run/gateway-init-secrets/postgres_runtime_password)"
export GATEWAY_BACKUP_PASSWORD="$(cat /run/gateway-init-secrets/postgres_backup_password)"
# Remove transient copies before any SQL or validation can fail. The original
# bind-mounted files remain private and available only to authorized root ops.
rm -f -- /run/gateway-init-secrets/postgres_migration_password /run/gateway-init-secrets/postgres_runtime_password /run/gateway-init-secrets/postgres_backup_password
bootstrap_password="${POSTGRES_PASSWORD:?Official root entrypoint must load the bootstrap password}"
# Reject empty/reused database credentials. Values never enter process arguments.
for name in GATEWAY_MIGRATION_PASSWORD GATEWAY_RUNTIME_PASSWORD GATEWAY_BACKUP_PASSWORD; do
  eval 'value=${'"$name"'}'
  [ -n "$value" ] && [ "$value" != "$bootstrap_password" ] || {
    echo 'Database credentials must be nonempty and independent.' >&2; exit 1;
  }
done
[ "$GATEWAY_MIGRATION_PASSWORD" != "$GATEWAY_RUNTIME_PASSWORD" ] &&
[ "$GATEWAY_MIGRATION_PASSWORD" != "$GATEWAY_BACKUP_PASSWORD" ] &&
[ "$GATEWAY_RUNTIME_PASSWORD" != "$GATEWAY_BACKUP_PASSWORD" ] || {
  echo 'Database credentials must be independent.' >&2; exit 1;
}
unset bootstrap_password value
if ! psql --no-psqlrc --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/roles.psql >/dev/null 2>&1; then
  unset GATEWAY_MIGRATION_PASSWORD GATEWAY_RUNTIME_PASSWORD GATEWAY_BACKUP_PASSWORD
  echo 'Database role initialization failed; raw SQL and credentials are suppressed.' >&2
  exit 1
fi
unset GATEWAY_MIGRATION_PASSWORD GATEWAY_RUNTIME_PASSWORD GATEWAY_BACKUP_PASSWORD
