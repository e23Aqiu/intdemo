#!/usr/bin/env bash
set -Eeuo pipefail

export PGPASSWORD
PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"
stamp="$(date '+%Y%m%d-%H%M%S')"
target="/backups/intdemo-${stamp}.dump"
temporary="${target}.tmp"
pg_dump --format=custom --compress=6 --no-owner --no-privileges \
  --file="${temporary}" "${PGDATABASE}"
pg_restore --list "${temporary}" >/dev/null
mv "${temporary}" "${target}"
sha256sum "${target}" > "${target}.sha256"
echo "${target}"

