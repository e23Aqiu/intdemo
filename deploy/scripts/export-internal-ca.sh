#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="${1:-${repo_dir}/deploy/generated/intdemo-caddy-root.crt}"
mkdir -p "$(dirname "${output}")"
temporary="${output}.tmp"
for _ in $(seq 1 30); do
  if docker compose --project-directory "${repo_dir}" exec -T caddy \
    test -f /data/caddy/pki/authorities/local/root.crt; then
    break
  fi
  sleep 1
done
docker compose --project-directory "${repo_dir}" exec -T caddy \
  test -f /data/caddy/pki/authorities/local/root.crt
docker compose --project-directory "${repo_dir}" cp \
  caddy:/data/caddy/pki/authorities/local/root.crt "${temporary}"
openssl x509 -in "${temporary}" -noout -subject -issuer -fingerprint -sha256
mv "${temporary}" "${output}"
chmod 644 "${output}"
echo "exported root CA: ${output}"
