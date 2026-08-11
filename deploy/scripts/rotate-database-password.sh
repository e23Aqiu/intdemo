#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
secret_file="${repo_dir}/deploy/secrets/postgres_password.txt"
temporary_file="$(mktemp "${secret_file}.tmp.XXXXXX")"
trap 'rm -f "${temporary_file}"' EXIT

new_password="$(openssl rand -hex 32)"
printf '%s\n' "${new_password}" > "${temporary_file}"
chmod 600 "${temporary_file}"

cd "${repo_dir}"
printf "ALTER ROLE intdemo WITH PASSWORD '%s';\n" "${new_password}" \
  | docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U intdemo -d postgres >/dev/null

mv "${temporary_file}" "${secret_file}"
unset new_password
trap - EXIT
echo "database password rotated; recreate API and backup containers"
