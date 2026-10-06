#!/usr/bin/env bash
# Minimal root-before-drop bridge for private Compose file-backed secrets.
# The official PostgreSQL entrypoint remains responsible for the DB lifecycle.
set -Eeuo pipefail

stage_init_secrets() (
  set -Eeuo pipefail
  local stage_dir="$1" source_dir="$2" owner_uid="$3" owner_gid="$4"
  export LC_ALL=C
  [[ "$owner_uid" =~ ^[0-9]+$ && "$owner_gid" =~ ^[0-9]+$ ]]
  [[ "$(id -u)" == 0 ]]
  [[ -d "$stage_dir" && ! -L "$stage_dir" ]]
  [[ "$(realpath -e -- "$stage_dir")" == "$stage_dir" ]]
  mountpoint -q -- "$stage_dir"
  [[ "$(stat -f -c %T -- "$stage_dir")" == tmpfs ]]
  [[ "$(stat -c %u -- "$stage_dir")" == 0 ]]
  [[ "$(stat -c %a -- "$stage_dir")" == 700 ]]
  [[ -z "$(find "$stage_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]
  # Keep the directory root-owned until every complete file is private and has
  # its final owner. Failure cannot invoke initdb or leave a usable partial set.
  trap 'rm -f -- "$stage_dir/postgres_migration_password" "$stage_dir/postgres_runtime_password" "$stage_dir/postgres_backup_password"' EXIT
  umask 077
  set -C
  local name source before after mode size
  for name in migration runtime backup; do
    source="$source_dir/postgres_${name}_password"
    [[ -f "$source" && ! -L "$source" ]]
    # Open once, then inspect/copy that descriptor. A pathname swap must not
    # redirect a later copy to another file; reject any inode/metadata change.
    exec 3< "$source"
    [[ "$(stat -L -c %F -- /proc/self/fd/3)" == 'regular file' ]]
    mode="$(stat -L -c %a -- /proc/self/fd/3)"
    [[ "$mode" == 400 || "$mode" == 600 ]]
    size="$(stat -L -c %s -- /proc/self/fd/3)"
    [[ "$size" -ge 1 && "$size" -le 4096 ]]
    before="$(stat -L -c '%d:%i:%a:%s:%y:%z' -- /proc/self/fd/3)"
    [[ ! -L "$source" && "$before" == "$(stat -L -c '%d:%i:%a:%s:%y:%z' -- "$source")" ]]
    cat <&3 > "$stage_dir/postgres_${name}_password"
    after="$(stat -L -c '%d:%i:%a:%s:%y:%z' -- /proc/self/fd/3)"
    [[ "$before" == "$after" && ! -L "$source" ]]
    [[ "$before" == "$(stat -L -c '%d:%i:%a:%s:%y:%z' -- "$source")" ]]
    exec 3<&-
    [[ "$(stat -c %s -- "$stage_dir/postgres_${name}_password")" == "$size" ]]
    chmod 0400 -- "$stage_dir/postgres_${name}_password"
    chown "$owner_uid:$owner_gid" -- "$stage_dir/postgres_${name}_password"
  done
  chown "$owner_uid:$owner_gid" -- "$stage_dir"
  trap - EXIT
)

main() {
  if [[ "$#" == 0 ]]; then set -- postgres; fi
  if [[ "${1:0:1}" == '-' ]]; then set -- postgres "$@"; fi
  if [[ "$1" == postgres && ! -s "${PGDATA:?PGDATA is required}/PG_VERSION" ]]; then
    [[ "$(id -u)" == 0 ]] || { echo 'Private secret bootstrap requires the container root entrypoint.' >&2; exit 1; }
    # No secret content is added to this parent process environment. The three
    # password files exist only in this dedicated, verified memory filesystem.
    local owner_uid owner_gid
    owner_uid="$(id -u postgres)"
    owner_gid="$(id -g postgres)"
    stage_init_secrets /run/gateway-init-secrets /run/secrets "$owner_uid" "$owner_gid"
  fi
  exec /usr/local/bin/docker-entrypoint.sh "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
