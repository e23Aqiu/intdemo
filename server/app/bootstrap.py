from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, MetricDefinition
from .security import hash_password
from .services import append_change

BOOTSTRAP_ACCOUNTS = (
    ("admin", "系统管理员", "admin", "all", True),
    ("luogang", "萝岗中心站", "user", "own", False),
    ("taiping", "太平中心站", "user", "own", False),
    ("daojiao", "道滘中心站", "user", "own", False),
    ("baoan", "宝安中心站", "user", "own", False),
    ("nantou", "南头中心站", "user", "own", False),
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
    existing_usernames = set(db.scalars(select(Account.username)))
    for username, display_name, role, scope, active in BOOTSTRAP_ACCOUNTS:
        if username in existing_usernames:
            continue
        account = Account(
            username=username,
            display_name=display_name,
            password_hash=hash_password("123456"),
            role=role,
            stats_scope=scope,
            device_limit=1,
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
                "stats_scope": account.stats_scope,
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
