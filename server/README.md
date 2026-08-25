# IntDemo Sync API

FastAPI service for the v0.2 online test. Production is started by the root
`docker-compose.yml`; this directory can also run against SQLite for API tests.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
$env:INTDEMO_ENVIRONMENT = "test"
.\.venv\Scripts\python.exe -m pytest
```

All routes are under `/api/v1`. A development process exposes OpenAPI at
`/api/v1/docs`; production disables the interactive documentation.

Important environment variables:

- `INTDEMO_DATABASE_URL`
- `INTDEMO_JWT_SECRET` or `INTDEMO_JWT_SECRET_FILE`
- `INTDEMO_OFFLINE_PRIVATE_KEY` or `INTDEMO_OFFLINE_PRIVATE_KEY_FILE`
- `INTDEMO_BOOTSTRAP_ENABLED`
- `INTDEMO_MAINTENANCE_MODE`

The Docker deployment uses secret files and runs `alembic upgrade head` before
starting exactly one Uvicorn worker.

## Account hierarchy compatibility

Version 1.2.0 adds `roads` and the authoritative `account_type`, `data_scope`,
and `road_id` account fields. The legacy `role` (`admin/user`) and
`stats_scope` (`own/all`) columns remain in place for v1.1.0/v1.1.1 clients.
Road administrators are projected as legacy users, and a road scope is
downgraded to own for devices older than 1.2.0. Migration 0014 assigns existing
non-admin accounts to `广深高速` without changing passwords, token versions,
active state, or refresh sessions. Migration 0015 detaches test accounts from
all roads while preserving those same security and session fields. Test accounts
are managed directly by a global administrator and are never visible to road
administrators. A road is assignable only while a same-named road-administrator
account owns it. Unassigned stations are managed directly by a global
administrator. Permanently deleting the last manager releases every station on
that road and removes the road; archiving keeps the relationship so it can be
restored. Legacy v1.1.x account creation without a road remains valid and creates
an unassigned station. Road-administrator accounts do not contribute business
statistics: legacy client uploads are acknowledged for outbox compatibility but
do not create metric or workflow rows, and existing rows are omitted from pull
and snapshot results. Their permitted road/all scopes still expose centre-station
statistics in the assigned data range.

## UOS update compatibility

Version 1.2.1 adds optional `uos-deb-xdelta-v1` capability negotiation to the
update-manifest endpoint. UOS clients without the
`X-IntDemo-Update-Capabilities` header—including v1.1.0, v1.1.1 and v1.2.0—
continue to receive the complete DEB. This routing does not read or mutate
access tokens, refresh sessions, account permissions or synchronization data.
