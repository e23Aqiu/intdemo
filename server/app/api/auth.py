from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db, utcnow
from ..dependencies import CurrentContext
from ..errors import ApiError
from ..models import Account, Device, LoginThrottle, RefreshSession
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenBundle,
)
from ..security import (
    create_access_token,
    create_offline_entitlement,
    hash_password,
    new_refresh_token,
    token_hash,
    validate_new_password,
    verify_password,
)
from ..services import (
    account_view,
    aware,
    device_view,
    latest_revision,
    revoke_refresh_sessions,
    throttle_key,
)

router = APIRouter(prefix="/auth", tags=["authentication"])
Db = Annotated[Session, Depends(get_db)]


def _client_ip(request: Request) -> str:
    if get_settings().trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _record_login_failure(db: Session, username: str, ip_address: str) -> None:
    now = utcnow()
    key = throttle_key(username, ip_address)
    row = db.get(LoginThrottle, key)
    if row is None:
        row = LoginThrottle(
            key=key,
            username=username,
            ip_address=ip_address,
            failed_count=1,
            window_started_at=now,
        )
        db.add(row)
    else:
        window_start = aware(row.window_started_at)
        if window_start is None or now - window_start > timedelta(minutes=15):
            row.failed_count = 1
            row.window_started_at = now
            row.locked_until = None
        else:
            row.failed_count += 1
    if row.failed_count >= 5:
        row.locked_until = now + timedelta(minutes=15)
    db.commit()


def _check_login_throttle(db: Session, username: str, ip_address: str) -> None:
    row = db.get(LoginThrottle, throttle_key(username, ip_address))
    if row and aware(row.locked_until) and aware(row.locked_until) > utcnow():
        retry_after = max(1, int((aware(row.locked_until) - utcnow()).total_seconds()))
        raise ApiError(
            "login_locked",
            "登录失败次数过多，请稍后重试",
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )


def _new_refresh_session(
    db: Session,
    account: Account,
    device: Device,
    *,
    family_uid: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> tuple[str, RefreshSession]:
    settings = get_settings()
    raw = new_refresh_token()
    row = RefreshSession(
        account_id=account.id,
        device_id=device.id,
        family_uid=family_uid or uuid.uuid4(),
        parent_id=parent_id,
        token_hash=token_hash(raw),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
    )
    db.add(row)
    db.flush()
    return raw, row


def _bundle(
    db: Session,
    account: Account,
    device: Device,
    raw_refresh: str,
    refresh_session: RefreshSession,
) -> TokenBundle:
    access = create_access_token(account, device)
    offline = create_offline_entitlement(account, device)
    return TokenBundle(
        access_token=access.value,
        access_expires_at=access.expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=refresh_session.expires_at,
        offline_entitlement=offline.value,
        offline_expires_at=offline.expires_at,
        offline_public_key=offline.public_key,
        account=account_view(db, account),
        device=device_view(device),
        server_revision=latest_revision(db),
    )


@router.post("/login", response_model=TokenBundle)
def login(payload: LoginRequest, request: Request, db: Db) -> TokenBundle:
    ip_address = _client_ip(request)
    _check_login_throttle(db, payload.username, ip_address)
    account = db.scalar(select(Account).where(Account.username == payload.username))
    if not account or not verify_password(payload.password, account.password_hash):
        _record_login_failure(db, payload.username, ip_address)
        raise ApiError(
            "invalid_credentials",
            "账号或密码错误",
            status_code=401,
        )
    if account.is_archived:
        raise ApiError("account_archived", "账号已归档", status_code=403)
    if not account.is_active:
        raise ApiError("account_disabled", "账号尚未启用", status_code=403)

    device = db.scalar(
        select(Device).where(
            Device.account_id == account.id,
            Device.device_uid == payload.device_uid,
        )
    )
    if device and device.revoked_at is not None:
        raise ApiError("device_revoked", "当前设备已被撤销", status_code=403)
    if device is None:
        device = Device(
            account_id=account.id,
            device_uid=payload.device_uid,
            name=payload.device_name,
            client_version=payload.client_version,
        )
        db.add(device)
        db.flush()
    else:
        device.name = payload.device_name
        device.client_version = payload.client_version
        device.last_seen_at = utcnow()

    account.last_login_at = utcnow()
    raw_refresh, session = _new_refresh_session(db, account, device)
    db.execute(
        delete(LoginThrottle).where(LoginThrottle.key == throttle_key(payload.username, ip_address))
    )
    db.commit()
    return _bundle(db, account, device, raw_refresh, session)


@router.post("/refresh", response_model=TokenBundle)
def refresh(payload: RefreshRequest, db: Db) -> TokenBundle:
    hashed = token_hash(payload.refresh_token)
    row = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == hashed))
    if row is None:
        raise ApiError("invalid_refresh_token", "刷新令牌无效", status_code=401)
    if row.used_at is not None:
        revoke_refresh_sessions(db, family_uid=row.family_uid, reason="token_reuse")
        account = db.get(Account, row.account_id)
        if account:
            account.token_version += 1
            account.entitlement_revision += 1
        db.commit()
        raise ApiError(
            "refresh_token_reuse",
            "检测到刷新令牌重复使用，相关会话已撤销",
            status_code=401,
        )
    if row.revoked_at is not None or aware(row.expires_at) <= utcnow():
        raise ApiError("refresh_token_expired", "刷新令牌已失效", status_code=401)

    account = db.get(Account, row.account_id)
    device = db.get(Device, row.device_id)
    if not account or not device or device.device_uid != payload.device_uid:
        raise ApiError("invalid_refresh_token", "刷新令牌与设备不匹配", status_code=401)
    if account.is_archived or not account.is_active:
        raise ApiError("account_unavailable", "账号已停用或归档", status_code=403)
    if device.revoked_at is not None:
        raise ApiError("device_revoked", "当前设备已被撤销", status_code=403)

    row.used_at = utcnow()
    raw_refresh, replacement = _new_refresh_session(
        db,
        account,
        device,
        family_uid=row.family_uid,
        parent_id=row.id,
    )
    db.flush()
    row.replaced_by_id = replacement.id
    device.last_seen_at = utcnow()
    db.commit()
    return _bundle(db, account, device, raw_refresh, replacement)


@router.post("/change-password", response_model=TokenBundle)
def change_password(
    payload: ChangePasswordRequest,
    context: CurrentContext,
    db: Db,
) -> TokenBundle:
    account = context.account
    if not verify_password(payload.current_password, account.password_hash):
        raise ApiError("invalid_current_password", "当前密码错误", status_code=400)
    validate_new_password(account.username, payload.new_password)
    account.password_hash = hash_password(payload.new_password)
    account.must_change_password = False
    account.token_version += 1
    account.entitlement_revision += 1
    revoke_refresh_sessions(db, account_id=account.id, reason="password_changed")
    raw_refresh, session = _new_refresh_session(db, account, context.device)
    db.commit()
    return _bundle(db, account, context.device, raw_refresh, session)


@router.post("/logout", status_code=204)
def logout(payload: LogoutRequest, context: CurrentContext, db: Db) -> None:
    revoke_refresh_sessions(
        db,
        device_id=context.device.id,
        reason="logout",
    )
    context.device.token_version += 1
    db.commit()
