#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  : "${INTDEMO_DATABASE_PASSWORD_FILE:?database password secret is required}"
  : "${INTDEMO_JWT_SECRET_FILE:?JWT secret is required}"
  : "${INTDEMO_OFFLINE_PRIVATE_KEY_FILE:?offline signing secret is required}"

  export INTDEMO_DATABASE_PASSWORD
  INTDEMO_DATABASE_PASSWORD="$(cat "${INTDEMO_DATABASE_PASSWORD_FILE}")"
  export INTDEMO_JWT_SECRET
  INTDEMO_JWT_SECRET="$(cat "${INTDEMO_JWT_SECRET_FILE}")"
  export INTDEMO_OFFLINE_PRIVATE_KEY
  INTDEMO_OFFLINE_PRIVATE_KEY="$(cat "${INTDEMO_OFFLINE_PRIVATE_KEY_FILE}")"

  exec setpriv --reuid=intdemo --regid=intdemo --init-groups "$@"
fi

exec "$@"
