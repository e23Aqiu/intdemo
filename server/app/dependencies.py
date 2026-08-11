from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .connection_test import connection_test_gate
from .database import get_db, utcnow
from .errors import ApiError
from .models import Account, Device
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthContext:
    account: Account
    device: Device
    claims: dict


def current_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError("authentication_required", "需要登录", status_code=401)
    claims = decode_access_token(credentials.credentials)
    try:
        account_id = uuid.UUID(claims["sub"])
        device_id = uuid.UUID(claims["did"])
    except (ValueError, KeyError) as exc:
        raise ApiError("invalid_access_token", "访问令牌无效", status_code=401) from exc
    account = db.get(Account, account_id)
    device = db.get(Device, device_id)
    if not account or not device or device.account_id != account.id:
        raise ApiError("session_revoked", "登录会话已失效", status_code=401)
    if int(claims.get("ver", -1)) != account.token_version:
        raise ApiError("session_revoked", "登录会话已失效", status_code=401)
    if int(claims.get("dver", -1)) != device.token_version:
        raise ApiError("session_revoked", "登录会话已失效", status_code=401)
    if account.is_archived or not account.is_active:
        raise ApiError("account_unavailable", "账号已停用或归档", status_code=403)
    if device.revoked_at is not None:
        raise ApiError("device_revoked", "当前设备已被撤销", status_code=403)
    if device.is_control_client:
        connection_test_gate.register_control_device(device.id)
    else:
        connection_test_gate.require_available(device.id)
    device.last_seen_at = utcnow()
    db.commit()
    return AuthContext(account=account, device=device, claims=claims)


def business_context(
    context: Annotated[AuthContext, Depends(current_context)],
) -> AuthContext:
    if context.account.must_change_password:
        raise ApiError(
            "password_change_required",
            "首次登录必须先修改密码",
            status_code=403,
        )
    return context


def admin_context(
    context: Annotated[AuthContext, Depends(business_context)],
) -> AuthContext:
    if context.account.role != "admin":
        raise ApiError("admin_required", "需要管理员权限", status_code=403)
    return context


def control_admin_context(
    context: Annotated[AuthContext, Depends(current_context)],
) -> AuthContext:
    if context.account.must_change_password:
        raise ApiError(
            "password_change_required",
            "首次登录必须先修改密码",
            status_code=403,
        )
    if context.account.role != "admin":
        raise ApiError("admin_required", "需要管理员权限", status_code=403)
    if not context.device.is_control_client:
        raise ApiError(
            "control_client_required",
            "连接测试操作需要使用控制客户端登录",
            status_code=403,
        )
    return context


Db = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[AuthContext, Depends(current_context)]
BusinessContext = Annotated[AuthContext, Depends(business_context)]
AdminContext = Annotated[AuthContext, Depends(admin_context)]
ControlAdminContext = Annotated[AuthContext, Depends(control_admin_context)]
