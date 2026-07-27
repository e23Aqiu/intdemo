from __future__ import annotations

import base64
import json
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..config import APP_VERSION
from ..database import AuthenticationError, Database
from ..models import Account
from .api import ApiClient, ApiResponseError, NetworkUnavailable
from .secure import (
    DpapiProtector,
    PasswordVerifier,
    Protector,
    create_password_verifier,
    verify_password_verifier,
)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_offline_entitlement(
    token: str,
    public_key_b64: str,
    *,
    expected_username: str,
    expected_device_uid: str,
    now: datetime | None = None,
) -> dict:
    try:
        body, signature = token.split(".", 1)
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )
        public_key.verify(_b64url_decode(signature), body.encode("ascii"))
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception as exc:
        raise AuthenticationError("本机离线授权签名无效") from exc
    if payload.get("v") != 1:
        raise AuthenticationError("本机离线授权版本不受支持")
    if str(payload.get("username", "")).casefold() != expected_username.casefold():
        raise AuthenticationError("离线授权与登录账号不匹配")
    if str(payload.get("device_uid", "")).casefold() != expected_device_uid.casefold():
        raise AuthenticationError("离线授权与当前设备不匹配")
    now = now or datetime.now(timezone.utc)
    if int(payload.get("expires_at", 0)) <= int(now.timestamp()):
        raise AuthenticationError("离线授权已超过 7 天有效期，请联网重新登录")
    return payload


@dataclass(frozen=True)
class SessionState:
    account: Account
    mode: str
    device_uid: str
    offline_expires_at: str
    access_token: str | None = None
    refresh_token: str | None = None

    @property
    def is_online(self) -> bool:
        return self.mode == "online"


class OnlineSessionManager:
    PROFILE_VERSION = 1

    def __init__(
        self,
        database: Database,
        api: ApiClient,
        protector: Protector | None = None,
    ):
        self.database = database
        self.api = api
        self.protector = protector or DpapiProtector()
        self.device_uid = self.database.get_or_create_online_device_uid()
        self.state: SessionState | None = None
        self._bundle: dict | None = None

    def _encrypt_profile(self, payload: dict) -> bytes:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.protector.protect(encoded)

    def _decrypt_profile(self) -> dict | None:
        encrypted = self.database.load_secure_online_profile()
        if not encrypted:
            return None
        try:
            raw = self.protector.unprotect(encrypted)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise AuthenticationError("无法读取本机加密登录资料") from exc
        if payload.get("version") != self.PROFILE_VERSION:
            raise AuthenticationError("本机登录资料版本不受支持，请联网重新登录")
        return payload

    def _save_bundle(self, bundle: dict, password: str) -> Account:
        account = self.database.upsert_remote_account(bundle["account"])
        self.database.set_current_online_account(
            bundle["account"]["id"],
            bundle["account"].get("stats_scope"),
        )
        profile = {
            "version": self.PROFILE_VERSION,
            "device_uid": self.device_uid,
            "server_account_id": bundle["account"]["id"],
            "username": bundle["account"]["username"],
            "bundle": bundle,
            "password_verifier": create_password_verifier(password).as_dict(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self.database.save_secure_online_profile(self._encrypt_profile(profile))
        self._bundle = bundle
        self.state = SessionState(
            account=account,
            mode="online",
            device_uid=self.device_uid,
            offline_expires_at=bundle["offline_expires_at"],
            access_token=bundle["access_token"],
            refresh_token=bundle["refresh_token"],
        )
        return account

    def _state_from_bundle(self, bundle: dict, mode: str) -> Account:
        account = self.database.upsert_remote_account(bundle["account"])
        self.database.set_current_online_account(
            bundle["account"]["id"],
            bundle["account"].get("stats_scope"),
        )
        self._bundle = bundle
        self.state = SessionState(
            account=account,
            mode=mode,
            device_uid=self.device_uid,
            offline_expires_at=bundle["offline_expires_at"],
            access_token=bundle.get("access_token") if mode == "online" else None,
            refresh_token=bundle.get("refresh_token") if mode == "online" else None,
        )
        return account

    def login(self, username: str, password: str) -> Account:
        username = str(username or "").strip().lower()
        if not username or not password:
            raise AuthenticationError("请输入账号和密码")
        try:
            bundle = self.api.login(
                username,
                password,
                self.device_uid,
                platform.node() or "Windows device",
                APP_VERSION,
            )
        except NetworkUnavailable:
            return self._offline_login(username, password)
        except ApiResponseError as exc:
            if exc.retryable:
                return self._offline_login(username, password)
            raise AuthenticationError(exc.message) from exc

        account = self._state_from_bundle(bundle, "online")
        if not bundle["account"].get("must_change_password"):
            account = self._save_bundle(bundle, password)
        return account

    def _offline_login(self, username: str, password: str) -> Account:
        profile = self._decrypt_profile()
        if not profile:
            raise AuthenticationError("首次登录必须连接在线服务")
        if str(profile.get("username", "")).casefold() != username.casefold():
            raise AuthenticationError("本机没有该账号的有效离线资料")
        if profile.get("device_uid") != self.device_uid:
            raise AuthenticationError("本机离线资料与当前设备不匹配")
        verifier = PasswordVerifier.from_dict(profile["password_verifier"])
        if not verify_password_verifier(password, verifier):
            raise AuthenticationError("账号或密码错误")
        bundle = profile["bundle"]
        verify_offline_entitlement(
            bundle["offline_entitlement"],
            bundle["offline_public_key"],
            expected_username=username,
            expected_device_uid=self.device_uid,
        )
        return self._state_from_bundle(bundle, "offline")

    def change_password(self, current_password: str, new_password: str) -> Account:
        if not self._bundle or not self.state or not self.state.access_token:
            raise NetworkUnavailable("修改密码必须连接在线服务")
        try:
            bundle = self.api.change_password(
                self.state.access_token,
                current_password,
                new_password,
            )
        except ApiResponseError as exc:
            raise AuthenticationError(exc.message) from exc
        return self._save_bundle(bundle, new_password)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def access_token(self) -> str:
        if not self._bundle or not self.state:
            raise NetworkUnavailable("当前没有可用的在线会话")
        if not self.state.is_online:
            return self._refresh_access_token()
        expires = self._parse_time(self._bundle["access_expires_at"])
        if expires.timestamp() - time.time() > 60:
            return self._bundle["access_token"]
        return self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        try:
            bundle = self.api.refresh(
                self._bundle["refresh_token"],
                self.device_uid,
            )
        except ApiResponseError as exc:
            if exc.code in {
                "refresh_token_reuse",
                "session_revoked",
                "device_revoked",
                "account_unavailable",
                "refresh_token_expired",
            }:
                self.database.clear_secure_online_profile()
            raise
        profile = self._decrypt_profile()
        if not profile:
            raise AuthenticationError("本机加密登录资料不存在")
        profile["bundle"] = bundle
        profile["saved_at"] = datetime.now(timezone.utc).isoformat()
        self.database.save_secure_online_profile(self._encrypt_profile(profile))
        self._state_from_bundle(bundle, "online")
        return bundle["access_token"]

    def note_online(self) -> None:
        if self.state and self._bundle and not self.state.is_online:
            self._state_from_bundle(self._bundle, "online")

    def note_offline(self) -> None:
        if self.state and self._bundle:
            self._state_from_bundle(self._bundle, "offline")

    def invalidate_credentials(self) -> None:
        self.database.clear_secure_online_profile()
        self._bundle = None
        if self.state:
            self.state = SessionState(
                account=self.state.account,
                mode="reauth_required",
                device_uid=self.state.device_uid,
                offline_expires_at=self.state.offline_expires_at,
            )

    def logout(self) -> None:
        if self._bundle and self.state and self.state.access_token:
            try:
                self.api.logout(
                    self.state.access_token,
                    self._bundle.get("refresh_token"),
                )
            except NetworkUnavailable:
                pass
            except ApiResponseError:
                pass
        self.database.clear_secure_online_profile()
        self._bundle = None
        self.state = None
