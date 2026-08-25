from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import get_data_dir
from .online.secure import (
    Protector,
    SecureStorageUnavailable,
    get_default_protector,
)


@dataclass(frozen=True)
class RememberedCredentials:
    username: str
    password: str
    auto_login: bool = False


class LoginCredentialStore:
    """Persist login details with the current OS user-scoped encryption."""

    VERSION = 1

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        protector: Protector | None = None,
    ):
        self.path = Path(directory or get_data_dir()) / "login-credentials.bin"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if protector is not None:
            self.protector = protector
        else:
            try:
                self.protector = get_default_protector()
            except SecureStorageUnavailable:
                self.protector = None

    @property
    def is_available(self) -> bool:
        return self.protector is not None

    def load(self) -> RememberedCredentials | None:
        if self.protector is None or not self.path.is_file():
            return None
        try:
            encrypted = self.path.read_bytes()
            raw = self.protector.unprotect(encrypted)
            payload = json.loads(raw.decode("utf-8"))
            if int(payload.get("version") or 0) != self.VERSION:
                return None
            username = str(payload.get("username") or "").strip().lower()
            password = str(payload.get("password") or "")
            if not username or not password:
                return None
            return RememberedCredentials(
                username=username,
                password=password,
                auto_login=bool(payload.get("auto_login", False)),
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            KeyError,
            SecureStorageUnavailable,
        ):
            return None

    def save(self, username: str, password: str, *, auto_login: bool) -> None:
        if self.protector is None:
            raise SecureStorageUnavailable(
                "当前系统无法安全保存密码，记住密码和自动登录不可用"
            )
        username = str(username or "").strip().lower()
        if not username or not password:
            raise ValueError("账号和密码不能为空")
        payload = {
            "version": self.VERSION,
            "username": username,
            "password": str(password),
            "auto_login": bool(auto_login),
        }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self.protector.protect(raw)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def disable_auto_login(self) -> None:
        remembered = self.load()
        if remembered is None:
            return
        self.save(
            remembered.username,
            remembered.password,
            auto_login=False,
        )

    def update_password(self, username: str, password: str) -> None:
        remembered = self.load()
        if (
            remembered is None
            or remembered.username.casefold()
            != str(username or "").strip().casefold()
        ):
            return
        self.save(
            remembered.username,
            password,
            auto_login=remembered.auto_login,
        )


class ClientPreferences:
    VERSION = 1

    def __init__(self, directory: str | Path | None = None):
        self.path = Path(directory or get_data_dir()) / "preferences.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return {}
        if int(payload.get("version") or 0) != self.VERSION:
            return {}
        return payload

    def _save(self, payload: dict) -> None:
        document = {"version": self.VERSION, **payload}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def ignored_update_version(self) -> str:
        return str(self._load().get("ignored_update_version") or "").strip()

    def ignore_update(self, version: str) -> None:
        payload = self._load()
        payload["ignored_update_version"] = str(version or "").strip()
        self._save(payload)

    def clear_ignored_update(self) -> None:
        payload = self._load()
        if "ignored_update_version" not in payload:
            return
        payload.pop("ignored_update_version", None)
        self._save(payload)

    @staticmethod
    def _account_key(username: str) -> str:
        return str(username or "").strip().casefold()

    def tencent_document_url(self, username: str) -> str:
        account_key = self._account_key(username)
        if not account_key:
            return ""
        links = self._load().get("tencent_document_urls")
        if not isinstance(links, dict):
            return ""
        return str(links.get(account_key) or "").strip()

    def set_tencent_document_url(self, username: str, url: str) -> None:
        account_key = self._account_key(username)
        if not account_key:
            raise ValueError("无法识别当前登录账号")
        payload = self._load()
        links = payload.get("tencent_document_urls")
        if not isinstance(links, dict):
            links = {}
        links[account_key] = str(url or "").strip()
        payload["tencent_document_urls"] = links
        self._save(payload)

    def dashboard_station_view(self, username: str) -> str:
        account_key = self._account_key(username)
        if not account_key:
            return "table"
        views = self._load().get("dashboard_station_views")
        if not isinstance(views, dict):
            return "table"
        view = str(views.get(account_key) or "").strip().lower()
        return view if view in {"table", "chart"} else "table"

    def set_dashboard_station_view(self, username: str, view: str) -> None:
        account_key = self._account_key(username)
        if not account_key:
            raise ValueError("无法识别当前登录账号")
        normalized = str(view or "").strip().lower()
        if normalized not in {"table", "chart"}:
            raise ValueError("仪表盘视图必须是 table 或 chart")
        payload = self._load()
        views = payload.get("dashboard_station_views")
        if not isinstance(views, dict):
            views = {}
        views[account_key] = normalized
        payload["dashboard_station_views"] = views
        self._save(payload)

    def statistics_yellow_only(self, username: str) -> bool:
        account_key = self._account_key(username)
        if not account_key:
            return True
        settings = self._load().get("statistics_yellow_only")
        if not isinstance(settings, dict):
            return True
        value = settings.get(account_key)
        return value if isinstance(value, bool) else True

    def set_statistics_yellow_only(self, username: str, checked: bool) -> None:
        account_key = self._account_key(username)
        if not account_key:
            raise ValueError("无法识别当前登录账号")
        payload = self._load()
        settings = payload.get("statistics_yellow_only")
        if not isinstance(settings, dict):
            settings = {}
        settings[account_key] = bool(checked)
        payload["statistics_yellow_only"] = settings
        self._save(payload)

    def workflow_run_settings(self, username: str) -> dict:
        account_key = self._account_key(username)
        if not account_key:
            return {}
        settings_by_account = self._load().get("workflow_run_settings")
        if not isinstance(settings_by_account, dict):
            return {}
        settings = settings_by_account.get(account_key)
        return dict(settings) if isinstance(settings, dict) else {}

    def set_workflow_run_settings(self, username: str, settings: dict) -> None:
        account_key = self._account_key(username)
        if not account_key:
            raise ValueError("无法识别当前登录账号")
        if not isinstance(settings, dict):
            raise ValueError("运行设置格式无效")

        sanitized = {
            "auto_mode": bool(settings.get("auto_mode", False)),
            "aiqicha_compatibility_mode": bool(
                settings.get("aiqicha_compatibility_mode", True)
            ),
            "manual_captcha": bool(settings.get("manual_captcha", True)),
            "auto_continue": bool(settings.get("auto_continue", True)),
            "only_yellow": bool(settings.get("only_yellow", True)),
            "infinite_captcha": bool(settings.get("infinite_captcha", False)),
            "page_retry": max(1, min(99, int(settings.get("page_retry", 5)))),
            "captcha_retry": max(
                1,
                min(9999, int(settings.get("captcha_retry", 10))),
            ),
        }
        payload = self._load()
        settings_by_account = payload.get("workflow_run_settings")
        if not isinstance(settings_by_account, dict):
            settings_by_account = {}
        settings_by_account[account_key] = sanitized
        payload["workflow_run_settings"] = settings_by_account
        self._save(payload)
