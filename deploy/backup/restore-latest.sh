#!/usr/bin/env bash
set -Eeuo pipefail

export PGPASSWORD
PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"

latest="$(find /backups -maxdepth 1 -type f -name 'intdemo-*.dump' | sort | tail -n 1)"
if [ -z "${latest}" ]; then
  echo "no backup found" >&2
  exit 1
fi
signature_sql="
SELECT json_build_object(
  'accounts', (SELECT COUNT(*) FROM accounts),
  'devices', (SELECT COUNT(*) FROM devices),
  'events', (SELECT COUNT(*) FROM activity_events),
  'batches', (SELECT COUNT(*) FROM workflow_batches),
  'runs', (SELECT COUNT(*) FROM workflow_runs),
  'changes', (SELECT COUNT(*) FROM change_log),
  'latest_revision', (SELECT COALESCE(MAX(revision), 0) FROM change_log)
)::text;
"
source_signature="$(
  psql --dbname="${PGDATABASE}" --tuples-only --no-align \
    --command "${signature_sql}"
)"
export CONFIRM_RESTORE=YES
/bin/bash /scripts/restore.sh "${latest}" intdemo_restore
restored_signature="$(
  psql --dbname=intdemo_restore --tuples-only --no-align \
    --command "${signature_sql}"
)"
if [ "${source_signature}" != "${restored_signature}" ]; then
  echo "restore verification mismatch" >&2
  echo "source:   ${source_signature}" >&2
  echo "restored: ${restored_signature}" >&2
  exit 1
fi
echo "restore drill passed: ${latest}; ${restored_signature}"
