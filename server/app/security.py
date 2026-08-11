from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pwdlib import PasswordHash

from .config import Settings, get_settings
from .errors import ApiError
from .models import Account, Device

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hasher.verify(password, encoded)
    except Exception:
        return False


def validate_new_password(username: str, password: str) -> None:
    if len(password) < 6:
        raise ApiError("weak_password", "密码至少需要 6 位", status_code=422)
    if not password.strip():
        raise ApiError("weak_password", "密码不能全为空白字符", status_code=422)
    if password.casefold() == username.casefold():
        raise ApiError("weak_password", "密码不能与用户名相同", status_code=422)
    if password == "123456":
        raise ApiError("weak_password", "新密码不能继续使用初始密码", status_code=422)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _private_key(settings: Settings) -> Ed25519PrivateKey:
    configured = settings.offline_private_key
    if configured:
        try:
            raw = base64.b64decode(configured)
            return Ed25519PrivateKey.from_private_bytes(raw)
        except Exception as exc:
            raise RuntimeError(
                "INTDEMO_OFFLINE_PRIVATE_KEY must be base64 raw Ed25519 key"
            ) from exc
    seed = hashlib.sha256(f"offline:{settings.jwt_secret}".encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime


def create_access_token(
    account: Account, device: Device, settings: Settings | None = None
) -> AccessToken:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    claims = {
        "iss": settings.jwt_issuer,
        "sub": str(account.id),
        "did": str(device.id),
        "dver": device.token_version,
        "ver": account.token_version,
        "role": account.role,
        "scope": account.stats_scope,
        "ent": account.entitlement_revision,
        "iat": now,
        "exp": expires,
        "jti": str(uuid.uuid4()),
    }
    return AccessToken(
        jwt.encode(claims, settings.jwt_secret, algorithm="HS256"),
        expires,
    )


def decode_access_token(value: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        return jwt.decode(
            value,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "sub", "did", "ver", "dver"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ApiError("access_token_expired", "访问令牌已过期", status_code=401) from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError("invalid_access_token", "访问令牌无效", status_code=401) from exc


@dataclass(frozen=True, slots=True)
class OfflineEntitlement:
    value: str
    expires_at: datetime
    public_key: str


def create_offline_entitlement(
    account: Account,
    device: Device,
    settings: Settings | None = None,
) -> OfflineEntitlement:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(days=settings.offline_entitlement_days)
    body = {
        "v": 1,
        "iss": settings.jwt_issuer,
        "account_id": str(account.id),
        "device_id": str(device.id),
        "device_uid": str(device.device_uid),
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "stats_scope": account.stats_scope,
        "entitlement_revision": account.entitlement_revision,
        "issued_at": int(now.timestamp()),
        "expires_at": int(expires.timestamp()),
    }
    encoded = _b64url(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    key = _private_key(settings)
    signature = _b64url(key.sign(encoded.encode("ascii")))
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return OfflineEntitlement(
        value=f"{encoded}.{signature}",
        expires_at=expires,
        public_key=base64.b64encode(public_raw).decode("ascii"),
    )


def verify_offline_entitlement(
    value: str,
    public_key_b64: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        encoded, signature = value.split(".", 1)
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public.verify(_b64url_decode(signature), encoded.encode("ascii"))
        payload = json.loads(_b64url_decode(encoded))
    except Exception as exc:
        raise ValueError("invalid offline entitlement") from exc
    now = now or datetime.now(UTC)
    if int(payload["expires_at"]) <= int(now.timestamp()):
        raise ValueError("offline entitlement expired")
    return payload
