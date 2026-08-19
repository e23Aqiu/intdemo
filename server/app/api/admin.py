from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import delete, select, update

from ..connection_test import connection_test_gate
from ..database import utcnow
from ..dependencies import AdminContext, ControlAdminContext, Db
from ..errors import ApiError
from ..models import (
    Account,
    ActivityEvent,
    AuditLog,
    Device,
    WorkflowBatch,
    WorkflowRun,
)
from ..realtime import update_hub
from ..schemas import (
    AccountCreate,
    AccountUpdate,
    AccountView,
    ConnectionTestActionResult,
    ConnectionTestOverview,
    DataResetRequest,
    DeviceView,
)
from ..security import hash_password
from ..services import (
    account_view,
    active_device_count,
    append_change,
    audit,
    device_view,
    latest_revision,
    revoke_refresh_sessions,
)

router = APIRouter(prefix="/admin", tags=["administration"])


def _account_or_404(db: Db, account_id: uuid.UUID) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise ApiError("account_not_found", "账号不存在", status_code=404)
    return account


def _account_change_payload(account: Account) -> dict:
    return {
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "is_test": account.is_test,
        "stats_scope": account.stats_scope,
        "device_limit": account.device_limit,
        "is_active": account.is_active,
        "is_archived": account.is_archived,
        "entitlement_revision": account.entitlement_revision,
    }


def _connection_test_device_or_404(db: Db, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if (
        device is None
        or device.revoked_at is not None
        or device.is_control_client
    ):
        raise ApiError("connection_test_client_not_found", "业务客户端不存在", status_code=404)
    return device


@router.get("/accounts", response_model=list[AccountView])
def list_accounts(context: AdminContext, db: Db) -> list[dict]:
    accounts = db.scalars(select(Account).order_by(Account.username)).all()
    return [account_view(db, account) for account in accounts]


@router.post("/accounts", response_model=AccountView, status_code=201)
async def create_account(
    payload: AccountCreate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    if db.scalar(select(Account.id).where(Account.username == payload.username)):
        raise ApiError("username_exists", "登录名已存在", status_code=409)
    account = Account(
        username=payload.username,
        display_name=payload.display_name.strip(),
        password_hash=hash_password("123456"),
        role=payload.role,
        is_test=payload.is_test,
        stats_scope=payload.stats_scope,
        device_limit=payload.device_limit,
        is_active=payload.is_active,
        must_change_password=True,
    )
    db.add(account)
    db.flush()
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        payload=_account_change_payload(account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.create",
        target_type="account",
        target_id=str(account.id),
        details={"username": account.username},
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return account_view(db, account)


@router.patch("/accounts/{account_id}", response_model=AccountView)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    account = _account_or_404(db, account_id)
    changes = payload.model_dump(exclude_unset=True)
    resulting_role = str(changes.get("role", account.role))
    resulting_is_test = bool(changes.get("is_test", account.is_test))
    if resulting_role == "admin":
        if changes.get("is_test") is True:
            raise ApiError(
                "invalid_test_account_role",
                "测试账号必须使用普通用户权限",
                status_code=422,
            )
        changes["is_test"] = False
    elif resulting_is_test:
        changes["role"] = "user"
    if "device_limit" in changes:
        current_active_devices = active_device_count(db, account.id)
        if changes["device_limit"] < current_active_devices:
            raise ApiError(
                "device_limit_below_active_count",
                "设备上限不能低于当前有效设备数，请先在设备列表中撤销多余设备",
                status_code=409,
                details={"active_device_count": current_active_devices},
            )
    if account.id == context.account.id and changes.get("is_active") is False:
        raise ApiError("cannot_disable_self", "不能停用当前管理员账号", status_code=409)
    if account.id == context.account.id and changes.get("role") == "user":
        raise ApiError("cannot_demote_self", "不能降低当前管理员权限", status_code=409)
    if account.id == context.account.id and changes.get("is_test") is True:
        raise ApiError("cannot_mark_self_test", "不能将当前管理员设为测试账号", status_code=409)

    security_changed = any(
        name in changes and changes[name] != getattr(account, name)
        for name in ("role", "is_test", "stats_scope", "is_active")
    )
    for name, value in changes.items():
        if name == "display_name":
            value = value.strip()
        setattr(account, name, value)
    account.entitlement_revision += 1
    if security_changed:
        account.token_version += 1
        revoke_refresh_sessions(db, account_id=account.id, reason="account_updated")
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        payload=_account_change_payload(account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.update",
        target_type="account",
        target_id=str(account.id),
        details={"fields": sorted(changes)},
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return account_view(db, account)


@router.post("/accounts/{account_id}/archive", response_model=AccountView)
async def archive_account(
    account_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    account = _account_or_404(db, account_id)
    if account.id == context.account.id:
        raise ApiError("cannot_archive_self", "不能归档当前管理员账号", status_code=409)
    account.is_archived = True
    account.is_active = False
    account.entitlement_revision += 1
    account.token_version += 1
    revoke_refresh_sessions(db, account_id=account.id, reason="account_archived")
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        operation="upsert",
        payload=_account_change_payload(account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.archive",
        target_type="account",
        target_id=str(account.id),
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return account_view(db, account)


@router.post("/accounts/{account_id}/restore", response_model=AccountView)
async def restore_account(
    account_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    account = _account_or_404(db, account_id)
    account.is_archived = False
    account.entitlement_revision += 1
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        payload=_account_change_payload(account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.restore",
        target_type="account",
        target_id=str(account.id),
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return account_view(db, account)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_archived_account(
    account_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> Response:
    account = _account_or_404(db, account_id)
    if account.id == context.account.id:
        raise ApiError(
            "cannot_delete_self",
            "不能删除当前管理员账号",
            status_code=409,
        )
    if not account.is_archived:
        raise ApiError(
            "account_not_archived",
            "仅已归档账号可以永久删除",
            status_code=409,
        )
    username = account.username
    entity_revision = account.entitlement_revision + 1
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.delete",
        target_type="account",
        target_id=str(account.id),
        details={"username": username, "permanent": True},
    )
    # Delete the business rows explicitly before the account.  This keeps the
    # permanent-delete contract reliable even on an older deployment whose
    # account foreign keys were created without ON DELETE CASCADE.
    for model in (ActivityEvent, WorkflowBatch, WorkflowRun):
        db.execute(delete(model).where(model.account_id == account.id))
    # Use a SQL delete so the remaining database-level ON DELETE rules remove
    # device, session, receipt and account-targeted rows without ORM nulling
    # children.
    db.execute(delete(Account).where(Account.id == account.id))
    change = append_change(
        db,
        account_id=None,
        kind="account",
        entity_id=str(account_id),
        entity_revision=entity_revision,
        operation="delete",
        payload={"username": username},
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/reset-password", status_code=204)
async def reset_password(
    account_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> None:
    account = _account_or_404(db, account_id)
    account.password_hash = hash_password("123456")
    account.must_change_password = True
    account.token_version += 1
    account.entitlement_revision += 1
    revoke_refresh_sessions(db, account_id=account.id, reason="password_reset")
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        payload=_account_change_payload(account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.password_reset",
        target_type="account",
        target_id=str(account.id),
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)


@router.get("/accounts/{account_id}/devices", response_model=list[DeviceView])
def list_devices(
    account_id: uuid.UUID,
    context: AdminContext,
    db: Db,
) -> list[dict]:
    _account_or_404(db, account_id)
    devices = db.scalars(
        select(Device).where(Device.account_id == account_id).order_by(Device.created_at.desc())
    ).all()
    return [device_view(device) for device in devices]


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> None:
    device = db.get(Device, device_id)
    if not device:
        raise ApiError("device_not_found", "设备不存在", status_code=404)
    if device.id == context.device.id:
        raise ApiError("cannot_revoke_current_device", "不能撤销当前登录设备", status_code=409)
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        device.revoked_reason = "revoked_by_admin"
        revoke_refresh_sessions(db, device_id=device.id, reason="device_revoked")
        account = db.get(Account, device.account_id)
        account.entitlement_revision += 1
        change = append_change(
            db,
            account_id=account.id,
            kind="device_revoked",
            entity_id=str(device.device_uid),
            entity_revision=account.entitlement_revision,
            operation="delete",
            payload={"device_uid": str(device.device_uid)},
        )
        audit(
            db,
            request,
            actor_id=context.account.id,
            action="device.revoke",
            target_type="device",
            target_id=str(device.id),
        )
        db.commit()
        await update_hub.broadcast_revision(change.revision)


@router.get(
    "/connection-test/clients",
    response_model=ConnectionTestOverview,
)
async def list_connection_test_clients(
    context: ControlAdminContext,
    db: Db,
) -> dict:
    connected_device_ids = await update_hub.connected_device_ids()
    rows = db.execute(
        select(Device, Account)
        .join(Account, Account.id == Device.account_id)
        .where(Device.revoked_at.is_(None))
        .order_by(Device.last_seen_at.desc())
    ).all()
    clients = [
        {
            "id": device.id,
            "account_id": account.id,
            "username": account.username,
            "display_name": account.display_name,
            "device_uid": device.device_uid,
            "name": device.name,
            "client_version": device.client_version,
            "last_seen_at": device.last_seen_at,
            "connected": device.id in connected_device_ids,
            "blocked": connection_test_gate.is_blocked(device.id),
        }
        for device, account in rows
        if not device.is_control_client
    ]
    return {
        "global_blocked": connection_test_gate.global_blocked,
        "clients": clients,
    }


@router.post(
    "/connection-test/all/disconnect",
    response_model=ConnectionTestActionResult,
)
async def disconnect_all_clients(
    request: Request,
    context: ControlAdminContext,
    db: Db,
) -> dict:
    connection_test_gate.disconnect_all()
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="connection_test.disconnect_all",
        target_type="client_connection",
        details={"scope": "all"},
    )
    db.commit()
    closed = await update_hub.close_blocked(
        lambda device_id: (
            not connection_test_gate.is_control_device(device_id)
            and connection_test_gate.is_blocked(device_id)
        )
    )
    return {
        "global_blocked": True,
        "blocked": True,
        "closed_websockets": closed,
    }


@router.post(
    "/connection-test/all/restore",
    response_model=ConnectionTestActionResult,
)
async def restore_all_clients(
    request: Request,
    context: ControlAdminContext,
    db: Db,
) -> dict:
    connection_test_gate.restore_all()
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="connection_test.restore_all",
        target_type="client_connection",
        details={"scope": "all"},
    )
    db.commit()
    return {
        "global_blocked": False,
        "blocked": False,
        "closed_websockets": 0,
    }


@router.post(
    "/connection-test/devices/{device_id}/disconnect",
    response_model=ConnectionTestActionResult,
)
async def disconnect_client(
    device_id: uuid.UUID,
    request: Request,
    context: ControlAdminContext,
    db: Db,
) -> dict:
    device = _connection_test_device_or_404(db, device_id)
    connection_test_gate.disconnect_device(device.id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="connection_test.disconnect_device",
        target_type="device",
        target_id=str(device.id),
    )
    db.commit()
    closed = await update_hub.close_device(device.id)
    return {
        "global_blocked": connection_test_gate.global_blocked,
        "device_id": device.id,
        "blocked": True,
        "closed_websockets": closed,
    }


@router.post(
    "/connection-test/devices/{device_id}/restore",
    response_model=ConnectionTestActionResult,
)
async def restore_client(
    device_id: uuid.UUID,
    request: Request,
    context: ControlAdminContext,
    db: Db,
) -> dict:
    device = _connection_test_device_or_404(db, device_id)
    connection_test_gate.restore_device(device.id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="connection_test.restore_device",
        target_type="device",
        target_id=str(device.id),
    )
    db.commit()
    return {
        "global_blocked": connection_test_gate.global_blocked,
        "device_id": device.id,
        "blocked": False,
        "closed_websockets": 0,
    }


@router.post("/data-reset", status_code=204)
async def reset_data(
    payload: DataResetRequest,
    request: Request,
    context: AdminContext,
    db: Db,
) -> None:
    account = _account_or_404(db, payload.account_id)
    reset_at = utcnow()
    account.data_reset_at = reset_at
    account.entitlement_revision += 1
    for model in (ActivityEvent, WorkflowBatch, WorkflowRun):
        db.execute(
            update(model)
            .where(model.account_id == account.id, model.deleted_at.is_(None))
            .values(deleted_at=reset_at)
        )
    change = append_change(
        db,
        account_id=account.id,
        kind="account_data_reset",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        operation="delete",
        payload={"reset_at": reset_at.isoformat()},
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.data_reset",
        target_type="account",
        target_id=str(account.id),
        details={"reset_at": reset_at.isoformat()},
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)


@router.get("/audit")
def list_audit_logs(
    context: AdminContext,
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
) -> dict:
    query = select(AuditLog)
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    rows = db.scalars(query.order_by(AuditLog.id.desc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [
            {
                "id": row.id,
                "actor_account_id": row.actor_account_id,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "request_id": row.request_id,
                "ip_address": row.ip_address,
                "details": row.details,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "has_more": has_more,
        "latest_revision": latest_revision(db),
    }
