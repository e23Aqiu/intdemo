#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
secret_dir="${repo_dir}/deploy/secrets"
mkdir -p "${secret_dir}"

create_secret() {
  local path="$1"
  local bytes="$2"
  local encoding="${3:-base64}"
  if [ -e "${path}" ]; then
    echo "preserving existing secret: ${path}"
    return
  fi
  if [ "${encoding}" = "hex" ]; then
    openssl rand -hex "${bytes}" > "${path}"
  else
    openssl rand -base64 "${bytes}" | tr -d '\r\n' > "${path}"
  fi
  chmod 600 "${path}"
}

create_secret "${secret_dir}/postgres_password.txt" 32 hex
create_secret "${secret_dir}/jwt_secret.txt" 48
create_secret "${secret_dir}/offline_private_key.txt" 32
echo "secrets initialized in ${secret_dir}"
