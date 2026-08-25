from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import delete, or_, select, update

from ..connection_test import connection_test_gate
from ..database import utcnow
from ..dependencies import AccountManagerContext, ControlAdminContext, Db
from ..errors import ApiError
from ..models import (
    Account,
    ActivityEvent,
    AuditLog,
    Device,
    Road,
    WorkflowBatch,
    WorkflowRun,
)
from ..permissions import (
    DEFAULT_ROAD_NAME,
    GLOBAL_ADMIN,
    ROAD_ADMIN,
    SCOPE_ALL,
    SCOPE_OWN,
    SCOPE_ROAD,
    STATION,
    account_type,
    can_manage_account,
    data_scope,
    is_global_admin,
    is_road_admin,
    legacy_stats_scope,
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
    RoadView,
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
    road_view,
)

router = APIRouter(prefix="/admin", tags=["administration"])


def _account_or_404(db: Db, account_id: uuid.UUID) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise ApiError("account_not_found", "账号不存在", status_code=404)
    return account


def _default_road(db: Db) -> Road:
    road = db.scalar(select(Road).where(Road.name == DEFAULT_ROAD_NAME))
    if road is None:
        road = Road(name=DEFAULT_ROAD_NAME)
        db.add(road)
        db.flush()
    return road


def _road_or_404(db: Db, road_id: uuid.UUID | None) -> Road:
    if road_id is None:
        raise ApiError("road_required", "账号尚未分配所属路段", status_code=422)
    road = db.get(Road, road_id)
    if road is None:
        raise ApiError("road_not_found", "所属路段不存在", status_code=422)
    return road


def _resolve_road(
    db: Db,
    *,
    road_id: uuid.UUID | None,
    road_name: str | None,
    allow_create: bool,
) -> Road | None:
    if road_id is not None:
        return _road_or_404(db, road_id)
    if road_name is None:
        return None
    road = db.scalar(select(Road).where(Road.name == road_name.strip()))
    if road is None and allow_create:
        road = Road(name=road_name.strip())
        db.add(road)
        db.flush()
    if road is None:
        raise ApiError("road_not_found", "所属路段不存在", status_code=422)
    return road


def _managed_account_or_404(
    db: Db,
    actor: Account,
    account_id: uuid.UUID,
) -> Account:
    account = _account_or_404(db, account_id)
    if not can_manage_account(actor, account):
        raise ApiError(
            "account_outside_management_scope",
            "只能管理本路段的中心站账号",
            status_code=403,
        )
    return account


def _account_change_payload(db: Db, account: Account) -> dict:
    scope = data_scope(account)
    road = db.get(Road, account.road_id) if account.road_id is not None else None
    return {
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "account_type": account_type(account),
        "is_test": account.is_test,
        "stats_scope": legacy_stats_scope(scope),
        "data_scope": scope,
        "road_id": str(account.road_id) if account.road_id else None,
        "road_name": road.name if road is not None else None,
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
def list_accounts(context: AccountManagerContext, db: Db) -> list[dict]:
    statement = select(Account).order_by(Account.username)
    if is_road_admin(context.account):
        statement = statement.where(
            Account.account_type == STATION,
            Account.is_test.is_(False),
            Account.road_id == context.account.road_id,
        )
    accounts = db.scalars(statement).all()
    return [account_view(db, account) for account in accounts]


@router.get("/roads", response_model=list[RoadView])
def list_roads(context: AccountManagerContext, db: Db) -> list[dict]:
    statement = select(Road).order_by(Road.name)
    if is_road_admin(context.account):
        statement = statement.where(Road.id == context.account.road_id)
    return [road_view(road) for road in db.scalars(statement).all()]


@router.post("/accounts", response_model=AccountView, status_code=201)
async def create_account(
    payload: AccountCreate,
    request: Request,
    context: AccountManagerContext,
    db: Db,
) -> dict:
    if db.scalar(select(Account.id).where(Account.username == payload.username)):
        raise ApiError("username_exists", "登录名已存在", status_code=409)
    requested_type = payload.account_type or (
        GLOBAL_ADMIN if payload.role == "admin" else STATION
    )
    if is_road_admin(context.account) and requested_type != STATION:
        raise ApiError(
            "account_type_not_allowed",
            "路段管理员只能新增本路段中心站账号",
            status_code=403,
        )
    if is_road_admin(context.account) and (
        payload.road_name is not None
        or (
            payload.road_id is not None
            and payload.road_id != context.account.road_id
        )
    ):
        raise ApiError(
            "road_assignment_not_allowed",
            "路段管理员不能把中心站分配到其他路段",
            status_code=403,
        )
    is_test = bool(payload.is_test)
    if is_road_admin(context.account) and is_test:
        raise ApiError(
            "test_account_not_allowed",
            "测试账号只能由管理员直接管理",
            status_code=403,
        )
    if is_test and requested_type != STATION:
        raise ApiError(
            "invalid_test_account_role",
            "测试账号必须属于中心站账号",
            status_code=422,
        )

    if payload.data_scope is not None:
        requested_scope = payload.data_scope
    elif "stats_scope" in payload.model_fields_set:
        requested_scope = payload.stats_scope
    else:
        requested_scope = SCOPE_ROAD if requested_type == ROAD_ADMIN else SCOPE_OWN

    road = None
    if requested_type == GLOBAL_ADMIN:
        requested_scope = SCOPE_ALL
        is_test = False
    elif is_test:
        if payload.road_id is not None or payload.road_name is not None:
            raise ApiError(
                "test_account_has_no_road",
                "测试账号不分配所属路段",
                status_code=422,
            )
        if requested_scope == SCOPE_ROAD:
            raise ApiError(
                "test_account_scope_not_allowed",
                "测试账号不能使用本路段数据范围",
                status_code=422,
            )
    elif is_road_admin(context.account):
        road = _road_or_404(db, context.account.road_id)
        if requested_scope not in {SCOPE_OWN, SCOPE_ROAD}:
            raise ApiError(
                "data_scope_not_allowed",
                "路段管理员只能设置本人或本路段数据范围",
                status_code=403,
            )
    else:
        road = _resolve_road(
            db,
            road_id=payload.road_id,
            road_name=payload.road_name,
            allow_create=requested_type == ROAD_ADMIN,
        )
        new_protocol = bool(
            payload.account_type is not None
            or payload.data_scope is not None
            or payload.road_id is not None
            or payload.road_name is not None
        )
        if road is None:
            if new_protocol:
                raise ApiError(
                    "road_required",
                    "中心站和路段管理员账号必须分配所属路段",
                    status_code=422,
                )
            # Legacy administrators never send a road field.  Assigning the
            # compatibility road keeps their existing create-account flow valid.
            road = _default_road(db)
    if requested_scope == SCOPE_ROAD and road is None:
        raise ApiError("road_required", "本路段数据范围需要所属路段", status_code=422)

    account = Account(
        username=payload.username,
        display_name=payload.display_name.strip(),
        password_hash=hash_password("123456"),
        role="admin" if requested_type == GLOBAL_ADMIN else "user",
        account_type=requested_type,
        is_test=is_test,
        stats_scope=legacy_stats_scope(requested_scope),
        data_scope=requested_scope,
        road_id=road.id if road is not None else None,
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
        payload=_account_change_payload(db, account),
    )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="account.create",
        target_type="account",
        target_id=str(account.id),
        details={
            "username": account.username,
            "account_type": requested_type,
            "data_scope": requested_scope,
            "road_id": str(account.road_id) if account.road_id else None,
        },
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return account_view(db, account)


@router.patch("/accounts/{account_id}", response_model=AccountView)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    request: Request,
    context: AccountManagerContext,
    db: Db,
) -> dict:
    account = _managed_account_or_404(db, context.account, account_id)
    changes = payload.model_dump(exclude_unset=True)
    current_type = account_type(account)
    resulting_type = str(changes.get("account_type", current_type))
    if "account_type" not in changes and "role" in changes:
        resulting_type = GLOBAL_ADMIN if changes["role"] == "admin" else STATION
    resulting_is_test = bool(changes.get("is_test", account.is_test))
    if resulting_is_test:
        if "account_type" in changes and resulting_type != STATION:
            raise ApiError(
                "invalid_test_account_role",
                "测试账号必须属于中心站账号",
                status_code=422,
            )
        resulting_type = STATION
    if is_road_admin(context.account):
        if resulting_type != STATION or {"road_id", "road_name"} & changes.keys():
            raise ApiError(
                "account_type_not_allowed",
                "路段管理员不能修改账号层级或所属路段",
                status_code=403,
            )
    if resulting_type != STATION:
        resulting_is_test = False

    resulting_road = (
        db.get(Road, account.road_id) if account.road_id is not None else None
    )
    if resulting_is_test:
        if {"road_id", "road_name"} & changes.keys():
            raise ApiError(
                "test_account_has_no_road",
                "测试账号不分配所属路段",
                status_code=422,
            )
        resulting_road = None
    elif is_global_admin(context.account) and {"road_id", "road_name"} & changes.keys():
        resulting_road = _resolve_road(
            db,
            road_id=changes.get("road_id"),
            road_name=changes.get("road_name"),
            allow_create=resulting_type == ROAD_ADMIN,
        )
    if resulting_type == GLOBAL_ADMIN:
        resulting_road = None
    elif not resulting_is_test and resulting_road is None:
        if "account_type" in changes:
            raise ApiError(
                "road_required",
                "中心站和路段管理员账号必须分配所属路段",
                status_code=422,
            )
        # This path keeps legacy role demotions compatible while all new
        # hierarchical requests are required to name a road explicitly.
        resulting_road = _default_road(db)

    if "data_scope" in changes:
        resulting_scope = str(changes["data_scope"])
    elif "stats_scope" in changes:
        resulting_scope = str(changes["stats_scope"])
    else:
        resulting_scope = data_scope(account)
    if resulting_type == GLOBAL_ADMIN:
        resulting_scope = SCOPE_ALL
    scope_was_requested = bool({"data_scope", "stats_scope"} & changes.keys())
    if resulting_is_test and resulting_scope == SCOPE_ROAD:
        if scope_was_requested:
            raise ApiError(
                "test_account_scope_not_allowed",
                "测试账号不能使用本路段数据范围",
                status_code=422,
            )
        resulting_scope = SCOPE_OWN
    if (
        is_road_admin(context.account)
        and scope_was_requested
        and resulting_scope not in {SCOPE_OWN, SCOPE_ROAD}
    ):
        raise ApiError(
            "data_scope_not_allowed",
            "路段管理员只能设置本人或本路段数据范围",
            status_code=403,
        )
    if resulting_scope == SCOPE_ROAD and resulting_road is None:
        raise ApiError("road_required", "本路段数据范围需要所属路段", status_code=422)

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
    if account.id == context.account.id and resulting_type != GLOBAL_ADMIN:
        raise ApiError("cannot_demote_self", "不能降低当前管理员权限", status_code=409)
    if account.id == context.account.id and resulting_is_test:
        raise ApiError("cannot_mark_self_test", "不能将当前管理员设为测试账号", status_code=409)

    security_before = (
        current_type,
        bool(account.is_test),
        data_scope(account),
        account.road_id,
        bool(account.is_active),
    )
    for name in ("display_name", "device_limit", "is_active"):
        if name in changes:
            value = changes[name]
            setattr(account, name, value.strip() if name == "display_name" else value)
    account.account_type = resulting_type
    account.role = "admin" if resulting_type == GLOBAL_ADMIN else "user"
    account.is_test = resulting_is_test
    account.data_scope = resulting_scope
    account.stats_scope = legacy_stats_scope(resulting_scope)
    account.road_id = resulting_road.id if resulting_road is not None else None
    security_changed = security_before != (
        account.account_type,
        bool(account.is_test),
        account.data_scope,
        account.road_id,
        bool(account.is_active),
    )
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
        payload=_account_change_payload(db, account),
    )
    previous_road_id = security_before[3]
    if previous_road_id != account.road_id:
        change = append_change(
            db,
            account_id=None,
            kind="road_membership_changed",
            entity_id=str(account.id),
            entity_revision=account.entitlement_revision,
            payload={
                "old_road_id": str(previous_road_id) if previous_road_id else None,
                "new_road_id": str(account.road_id) if account.road_id else None,
            },
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
    context: AccountManagerContext,
    db: Db,
) -> dict:
    account = _managed_account_or_404(db, context.account, account_id)
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
        payload=_account_change_payload(db, account),
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
    context: AccountManagerContext,
    db: Db,
) -> dict:
    account = _managed_account_or_404(db, context.account, account_id)
    account.is_archived = False
    account.entitlement_revision += 1
    change = append_change(
        db,
        account_id=account.id,
        kind="account",
        entity_id=str(account.id),
        entity_revision=account.entitlement_revision,
        payload=_account_change_payload(db, account),
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
    context: AccountManagerContext,
    db: Db,
) -> Response:
    account = _managed_account_or_404(db, context.account, account_id)
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
    deleted_account_type = account_type(account)
    deleted_road_id = account.road_id
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
        payload={
            "username": username,
            "account_type": deleted_account_type,
            "road_id": str(deleted_road_id) if deleted_road_id else None,
        },
    )
    db.commit()
    await update_hub.broadcast_revision(change.revision)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/reset-password", status_code=204)
async def reset_password(
    account_id: uuid.UUID,
    request: Request,
    context: AccountManagerContext,
    db: Db,
) -> None:
    account = _managed_account_or_404(db, context.account, account_id)
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
        payload=_account_change_payload(db, account),
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
    context: AccountManagerContext,
    db: Db,
) -> list[dict]:
    _managed_account_or_404(db, context.account, account_id)
    devices = db.scalars(
        select(Device).where(Device.account_id == account_id).order_by(Device.created_at.desc())
    ).all()
    return [device_view(device) for device in devices]


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    context: AccountManagerContext,
    db: Db,
) -> None:
    device = db.get(Device, device_id)
    if not device:
        raise ApiError("device_not_found", "设备不存在", status_code=404)
    account = _managed_account_or_404(db, context.account, device.account_id)
    if device.id == context.device.id:
        raise ApiError("cannot_revoke_current_device", "不能撤销当前登录设备", status_code=409)
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        device.revoked_reason = "revoked_by_admin"
        revoke_refresh_sessions(db, device_id=device.id, reason="device_revoked")
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
    context: AccountManagerContext,
    db: Db,
) -> None:
    account = _managed_account_or_404(db, context.account, payload.account_id)
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
    context: AccountManagerContext,
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
) -> dict:
    query = select(AuditLog)
    if is_road_admin(context.account):
        managed_account_ids = list(
            db.scalars(
                select(Account.id).where(
                    Account.account_type == STATION,
                    Account.is_test.is_(False),
                    Account.road_id == context.account.road_id,
                )
            )
        )
        managed_device_ids = list(
            db.scalars(
                select(Device.id).where(Device.account_id.in_(managed_account_ids))
            )
        )
        visible_target_ids = [
            *(str(account_id) for account_id in managed_account_ids),
            *(str(device_id) for device_id in managed_device_ids),
        ]
        query = query.where(
            or_(
                AuditLog.actor_account_id == context.account.id,
                AuditLog.target_id.in_(visible_target_ids),
            )
        )
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
