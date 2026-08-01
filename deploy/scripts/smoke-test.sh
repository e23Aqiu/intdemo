#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${1:?usage: smoke-test.sh https://host [ca_bundle]}"
ca_bundle="${2:-}"
curl_args=(--fail --silent --show-error --connect-timeout 5 --max-time 15)
if [ -n "${ca_bundle}" ]; then
  curl_args+=(--cacert "${ca_bundle}")
fi
curl "${curl_args[@]}" "${base_url%/}/api/v1/health/live"
echo
curl "${curl_args[@]}" "${base_url%/}/api/v1/health/ready" || true
echo

