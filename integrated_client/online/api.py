from __future__ import annotations

from typing import Any

import requests

from ..config import APP_VERSION
from .config import OnlineConfig


class OnlineApiError(RuntimeError):
    pass


class NetworkUnavailable(OnlineApiError):
    pass


class TlsVerificationError(NetworkUnavailable):
    pass


class ApiResponseError(OnlineApiError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        details: Any = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details
        self.request_id = request_id


class ApiClient:
    def __init__(self, config: OnlineConfig, session: requests.Session | None = None):
        self.config = config.validate()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"IntDemoClientOnlineTest/{APP_VERSION}",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method,
                f"{self.config.api_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                verify=self.config.ca_bundle or True,
            )
        except requests.exceptions.SSLError as exc:
            raise TlsVerificationError(f"TLS 证书验证失败：{exc}") from exc
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            raise NetworkUnavailable(f"无法连接在线服务：{exc}") from exc
        except requests.RequestException as exc:
            raise NetworkUnavailable(f"在线请求失败：{exc}") from exc

        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {}
            raise ApiResponseError(
                str(error.get("code") or "http_error"),
                str(error.get("message") or f"服务器返回 HTTP {response.status_code}"),
                status_code=response.status_code,
                retryable=bool(error.get("retryable")),
                details=error.get("details"),
                request_id=error.get("request_id"),
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OnlineApiError("服务器返回了无效的 JSON") from exc

    def login(
        self,
        username: str,
        password: str,
        device_uid: str,
        device_name: str,
        client_version: str,
    ) -> dict:
        return self._request(
            "POST",
            "/auth/login",
            json_body={
                "username": username,
                "password": password,
                "device_uid": device_uid,
                "device_name": device_name,
                "client_version": client_version,
            },
        )

    def refresh(self, refresh_token: str, device_uid: str) -> dict:
        return self._request(
            "POST",
            "/auth/refresh",
            json_body={
                "refresh_token": refresh_token,
                "device_uid": device_uid,
            },
        )

    def change_password(
        self,
        access_token: str,
        current_password: str,
        new_password: str,
    ) -> dict:
        return self._request(
            "POST",
            "/auth/change-password",
            token=access_token,
            json_body={
                "current_password": current_password,
                "new_password": new_password,
            },
        )

    def logout(self, access_token: str, refresh_token: str | None) -> None:
        self._request(
            "POST",
            "/auth/logout",
            token=access_token,
            json_body={"refresh_token": refresh_token},
        )

    def push(self, access_token: str, items: list[dict]) -> dict:
        return self._request(
            "POST",
            "/sync/push",
            token=access_token,
            json_body={"items": items},
        )

    def pull(self, access_token: str, after_revision: int, limit: int = 500) -> dict:
        return self._request(
            "GET",
            "/sync/pull",
            token=access_token,
            params={"after_revision": after_revision, "limit": limit},
        )

    def snapshot(self, access_token: str) -> dict:
        return self._request("GET", "/sync/snapshot", token=access_token)

    def admin_accounts(self, access_token: str) -> list[dict]:
        return self._request("GET", "/admin/accounts", token=access_token)

    def admin_create_account(self, access_token: str, payload: dict) -> dict:
        return self._request(
            "POST", "/admin/accounts", token=access_token, json_body=payload
        )

    def admin_update_account(
        self, access_token: str, account_id: str, payload: dict
    ) -> dict:
        return self._request(
            "PATCH",
            f"/admin/accounts/{account_id}",
            token=access_token,
            json_body=payload,
        )

    def admin_archive_account(self, access_token: str, account_id: str) -> dict:
        return self._request(
            "POST", f"/admin/accounts/{account_id}/archive", token=access_token
        )

    def admin_restore_account(self, access_token: str, account_id: str) -> dict:
        return self._request(
            "POST", f"/admin/accounts/{account_id}/restore", token=access_token
        )

    def admin_reset_password(self, access_token: str, account_id: str) -> None:
        self._request(
            "POST", f"/admin/accounts/{account_id}/reset-password", token=access_token
        )

    def admin_devices(self, access_token: str, account_id: str) -> list[dict]:
        return self._request(
            "GET", f"/admin/accounts/{account_id}/devices", token=access_token
        )

    def admin_revoke_device(self, access_token: str, device_id: str) -> None:
        self._request("POST", f"/admin/devices/{device_id}/revoke", token=access_token)

    def admin_reset_data(self, access_token: str, account_id: str) -> None:
        self._request(
            "POST",
            "/admin/data-reset",
            token=access_token,
            json_body={"account_id": account_id, "confirmation": "RESET"},
        )

    def admin_audit(self, access_token: str, limit: int = 100) -> dict:
        return self._request(
            "GET", "/admin/audit", token=access_token, params={"limit": limit}
        )
