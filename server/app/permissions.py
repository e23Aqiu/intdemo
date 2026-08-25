from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account

GLOBAL_ADMIN = "admin"
ROAD_ADMIN = "road_admin"
STATION = "station"
ACCOUNT_TYPES = {GLOBAL_ADMIN, ROAD_ADMIN, STATION}

SCOPE_OWN = "own"
SCOPE_ROAD = "road"
SCOPE_ALL = "all"
DATA_SCOPES = {SCOPE_OWN, SCOPE_ROAD, SCOPE_ALL}

DEFAULT_ROAD_NAME = "广深高速"
HIERARCHICAL_SCOPE_MIN_VERSION = (1, 2, 0)


def account_type(account: Account) -> str:
    value = str(getattr(account, "account_type", "") or "").strip()
    if value in ACCOUNT_TYPES:
        return value
    return GLOBAL_ADMIN if account.role == "admin" else STATION


def data_scope(account: Account) -> str:
    value = str(getattr(account, "data_scope", "") or "").strip()
    if value in DATA_SCOPES:
        return value
    legacy = str(getattr(account, "stats_scope", "own") or "own")
    return SCOPE_ALL if legacy == SCOPE_ALL else SCOPE_OWN


def legacy_stats_scope(value: str) -> str:
    """Return the only scope values understood by v1.1.x clients."""
    return SCOPE_ALL if str(value) == SCOPE_ALL else SCOPE_OWN


def is_global_admin(account: Account) -> bool:
    return account_type(account) == GLOBAL_ADMIN


def is_road_admin(account: Account) -> bool:
    return account_type(account) == ROAD_ADMIN


def is_account_manager(account: Account) -> bool:
    return account_type(account) in {GLOBAL_ADMIN, ROAD_ADMIN}


def can_manage_account(actor: Account, target: Account) -> bool:
    if is_global_admin(actor):
        return True
    return bool(
        is_road_admin(actor)
        and actor.road_id is not None
        and target.id != actor.id
        and account_type(target) == STATION
        and target.road_id == actor.road_id
    )


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def supports_hierarchical_scope(client_version: str) -> bool:
    version = _version_tuple(client_version)
    return version is not None and version >= HIERARCHICAL_SCOPE_MIN_VERSION


def effective_data_scope(account: Account, client_version: str) -> str:
    if is_global_admin(account):
        return SCOPE_ALL
    value = data_scope(account)
    if value == SCOPE_ROAD and not supports_hierarchical_scope(client_version):
        # A v1.1.x local database cannot represent a road scope.  Serving only
        # the account's own rows keeps its legacy own/all cache invariant true.
        return SCOPE_OWN
    return value


def scoped_account_ids(
    db: Session,
    account: Account,
    client_version: str,
) -> list[uuid.UUID]:
    scope = effective_data_scope(account, client_version)
    if scope == SCOPE_ALL:
        return list(db.scalars(select(Account.id)))
    if scope == SCOPE_ROAD and account.road_id is not None:
        return list(
            db.scalars(select(Account.id).where(Account.road_id == account.road_id))
        )
    return [account.id]
