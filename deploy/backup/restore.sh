#!/usr/bin/env bash
set -Eeuo pipefail

backup_file="${1:-}"
target_database="${2:-intdemo_restore}"
if [ -z "${backup_file}" ] || [ ! -f "${backup_file}" ]; then
  echo "usage: restore.sh /backups/intdemo-YYYYMMDD-HHMMSS.dump [target_database]" >&2
  exit 2
fi
if ! [[ "${target_database}" =~ ^[a-zA-Z][a-zA-Z0-9_]{0,62}$ ]]; then
  echo "invalid target database name" >&2
  exit 2
fi
case "${target_database}" in
  postgres|template0|template1)
    echo "refusing to replace protected database ${target_database}" >&2
    exit 2
    ;;
esac
if [ "${CONFIRM_RESTORE:-}" != "YES" ]; then
  echo "set CONFIRM_RESTORE=YES; target database will be replaced" >&2
  exit 2
fi

checksum_file="${backup_file}.sha256"
if [ ! -f "${checksum_file}" ]; then
  echo "missing checksum: ${checksum_file}" >&2
  exit 1
fi
(cd "$(dirname "${backup_file}")" && sha256sum --check "$(basename "${checksum_file}")")

export PGPASSWORD
PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"
dropdb --maintenance-db=postgres --if-exists "${target_database}"
createdb --maintenance-db=postgres "${target_database}"
pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname="${target_database}" "${backup_file}"
echo "restored ${backup_file} into ${target_database}"
