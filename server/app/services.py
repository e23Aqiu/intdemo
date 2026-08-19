from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .database import utcnow
from .models import (
    Account,
    AuditLog,
    ChangeLog,
    Device,
    RefreshSession,
)


def aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def latest_revision(db: Session) -> int:
    return int(db.scalar(select(func.max(ChangeLog.revision))) or 0)


def active_device_count(db: Session, account_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count(Device.id)).where(
                Device.account_id == account_id,
                Device.revoked_at.is_(None),
                Device.is_control_client.is_(False),
            )
        )
        or 0
    )


def account_view(db: Session, account: Account) -> dict[str, Any]:
    online_cutoff = utcnow() - timedelta(seconds=90)
    online_count = int(
        db.scalar(
            select(func.count(Device.id)).where(
                Device.account_id == account.id,
                Device.revoked_at.is_(None),
                Device.is_control_client.is_(False),
                Device.last_seen_at >= online_cutoff,
            )
        )
        or 0
    )
    return {
        "id": account.id,
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "is_test": account.is_test,
        "stats_scope": account.stats_scope,
        "device_limit": account.device_limit,
        "is_active": account.is_active,
        "is_archived": account.is_archived,
        "must_change_password": account.must_change_password,
        "entitlement_revision": account.entitlement_revision,
        "active_device_count": active_device_count(db, account.id),
        "online_device_count": online_count,
        "last_login_at": account.last_login_at,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def device_view(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "device_uid": device.device_uid,
        "name": device.name,
        "client_version": device.client_version,
        "created_at": device.created_at,
        "last_seen_at": device.last_seen_at,
        "revoked_at": device.revoked_at,
        "revoked_reason": device.revoked_reason,
    }


def append_change(
    db: Session,
    *,
    account_id: uuid.UUID | None,
    kind: str,
    entity_id: str,
    entity_revision: int,
    operation: str = "upsert",
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ChangeLog:
    row = ChangeLog(
        account_id=account_id,
        kind=kind,
        entity_id=entity_id,
        entity_revision=entity_revision,
        operation=operation,
        payload=payload or {},
        occurred_at=occurred_at or utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def audit(
    db: Session,
    request: Request,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip_address = forwarded or (request.client.host if request.client else None)
    db.add(
        AuditLog(
            actor_account_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=ip_address,
            details=details or {},
        )
    )


def revoke_refresh_sessions(
    db: Session,
    *,
    account_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    family_uid: uuid.UUID | None = None,
    reason: str,
) -> None:
    clauses = [RefreshSession.revoked_at.is_(None)]
    if account_id is not None:
        clauses.append(RefreshSession.account_id == account_id)
    if device_id is not None:
        clauses.append(RefreshSession.device_id == device_id)
    if family_uid is not None:
        clauses.append(RefreshSession.family_uid == family_uid)
    db.execute(
        update(RefreshSession).where(*clauses).values(revoked_at=utcnow(), revoke_reason=reason)
    )


def throttle_key(username: str, ip_address: str) -> str:
    return hashlib.sha256(f"{username.casefold()}\0{ip_address}".encode()).hexdigest()
