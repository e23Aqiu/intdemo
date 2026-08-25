from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, CaptchaLearningPolicy, MetricDefinition, Road
from .permissions import (
    ACCOUNT_TYPES,
    DATA_SCOPES,
    DEFAULT_ROAD_NAME,
    GLOBAL_ADMIN,
    SCOPE_ALL,
    SCOPE_OWN,
    STATION,
    legacy_stats_scope,
)
from .security import hash_password
from .services import append_change

BOOTSTRAP_ACCOUNTS = (
    ("admin", "系统管理员", "admin", "all", True),
    ("luogang", "萝岗中心站", "user", "own", True),
    ("taiping", "太平中心站", "user", "own", True),
    ("daojiao", "道滘中心站", "user", "own", True),
    ("baoan", "宝安中心站", "user", "own", True),
    ("nantou", "南头中心站", "user", "own", True),
)

DEFAULT_METRICS = (
    ("workflow_detail_total", "总计数", "条", 10),
    ("workflow_detail_empty", "空", "条", 20),
    ("workflow_detail_no_transport", "无运输证号", "条", 30),
    ("workflow_detail_no_operation", "无营运信息", "条", 40),
    ("workflow_detail_individual", "个体经营", "条", 50),
    ("workflow_detail_company_no_phone", "无电话（有公司名）", "条", 60),
    ("workflow_detail_company_has_phone", "有电话（有公司名）", "条", 70),
)


def bootstrap_database(db: Session) -> None:
    default_road = db.scalar(select(Road).where(Road.name == DEFAULT_ROAD_NAME))
    if default_road is None:
        default_road = Road(name=DEFAULT_ROAD_NAME)
        db.add(default_road)
        db.flush()
    if db.get(CaptchaLearningPolicy, 1) is None:
        db.add(
            CaptchaLearningPolicy(
                id=1,
                upload_mode="off",
                upload_enabled=False,
                revision=1,
            )
        )
    existing_accounts = list(db.scalars(select(Account)))
    for existing in existing_accounts:
        if existing.role == "admin":
            existing.account_type = GLOBAL_ADMIN
            existing.data_scope = SCOPE_ALL
            existing.stats_scope = SCOPE_ALL
            existing.road_id = None
            continue
        if (
            existing.account_type not in ACCOUNT_TYPES
            or existing.account_type == GLOBAL_ADMIN
        ):
            existing.account_type = STATION
        legacy_scope = legacy_stats_scope(existing.data_scope)
        if (
            existing.data_scope not in DATA_SCOPES
            or existing.stats_scope != legacy_scope
        ):
            # During a rolling upgrade the v1.1 API may write the legacy
            # role/scope columns after migration 0014 has added the new ones.
            # A mismatch therefore means the legacy write is newer and must
            # win once before the v1.2 API takes over.
            existing.data_scope = (
                SCOPE_ALL if existing.stats_scope == SCOPE_ALL else SCOPE_OWN
            )
        if existing.is_test:
            existing.account_type = STATION
            if existing.data_scope == "road":
                existing.data_scope = SCOPE_OWN
            existing.stats_scope = legacy_stats_scope(existing.data_scope)
            existing.road_id = None
            continue
        existing.stats_scope = legacy_stats_scope(existing.data_scope)
        if existing.road_id is None:
            existing.road_id = default_road.id

    existing_usernames = {account.username for account in existing_accounts}
    for username, display_name, role, scope, active in BOOTSTRAP_ACCOUNTS:
        if username in existing_usernames:
            continue
        account = Account(
            username=username,
            display_name=display_name,
            password_hash=hash_password("123456"),
            role=role,
            account_type=GLOBAL_ADMIN if role == "admin" else STATION,
            stats_scope=scope,
            data_scope=scope,
            road_id=None if role == "admin" else default_road.id,
            device_limit=10000,
            is_active=active,
            must_change_password=True,
        )
        db.add(account)
        db.flush()
        append_change(
            db,
            account_id=account.id,
            kind="account",
            entity_id=str(account.id),
            entity_revision=account.entitlement_revision,
            payload={
                "username": account.username,
                "display_name": account.display_name,
                "role": account.role,
                "account_type": account.account_type,
                "stats_scope": account.stats_scope,
                "data_scope": account.data_scope,
                "road_id": str(account.road_id) if account.road_id else None,
                "road_name": default_road.name if account.road_id else None,
                "is_active": account.is_active,
                "is_archived": account.is_archived,
            },
        )
    for key, label, unit, order in DEFAULT_METRICS:
        if db.get(MetricDefinition, key) is None:
            db.add(
                MetricDefinition(
                    metric_key=key,
                    label=label,
                    unit=unit,
                    sort_order=order,
                )
            )
    db.commit()
