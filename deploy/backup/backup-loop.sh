#!/usr/bin/env bash
set -Eeuo pipefail

export PGPASSWORD
PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"
retention_days="${BACKUP_RETENTION_DAYS:-30}"

run_backup() {
  local stamp target temporary
  stamp="$(date '+%Y%m%d-%H%M%S')"
  target="/backups/intdemo-${stamp}.dump"
  temporary="${target}.tmp"
  pg_dump --format=custom --compress=6 --no-owner --no-privileges \
    --file="${temporary}" "${PGDATABASE}"
  pg_restore --list "${temporary}" >/dev/null
  mv "${temporary}" "${target}"
  sha256sum "${target}" > "${target}.sha256"
  find /backups -maxdepth 1 -type f \
    \( -name 'intdemo-*.dump' -o -name 'intdemo-*.dump.sha256' \) \
    -mtime "+${retention_days}" -delete
  echo "backup complete: ${target}"
}

while true; do
  now_epoch="$(date +%s)"
  next_epoch="$(date -d 'today 02:00' +%s)"
  if [ "${next_epoch}" -le "${now_epoch}" ]; then
    next_epoch="$(date -d 'tomorrow 02:00' +%s)"
  fi
  sleep_seconds="$((next_epoch - now_epoch))"
  echo "next backup in ${sleep_seconds}s"
  sleep "${sleep_seconds}"
  until run_backup; do
    echo "backup failed; retrying in 300 seconds" >&2
    sleep 300
  done
done
