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
        data_body: bytes | None = None,
        content_type: str | None = None,
        params: dict | None = None,
        raw_response: bool = False,
    ) -> Any:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.session.request(
                method,
                f"{self.config.api_url}{path}",
                headers=headers,
                json=json_body if data_body is None else None,
                data=data_body,
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
        if raw_response:
            return response
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

    def captcha_policy(self, access_token: str) -> dict:
        return self._request("GET", "/captcha/policy", token=access_token)

    def submit_captcha_attempt(
        self,
        access_token: str,
        payload: dict,
    ) -> dict:
        return self._request(
            "POST",
            "/captcha/attempts",
            token=access_token,
            json_body=payload,
        )

    def current_captcha_model(
        self,
        access_token: str,
        captcha_type: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/captcha/models/{captcha_type}/current",
            token=access_token,
            raw_response=True,
        )
        return bytes(response.content)

    def admin_captcha_learning_overview(
        self,
        access_token: str,
        *,
        captcha_type: str | None = None,
        model_version: str | None = None,
    ) -> dict:
        params = {}
        if captcha_type:
            params["captcha_type"] = str(captcha_type)
        if model_version:
            params["model_version"] = str(model_version)
        return self._request(
            "GET",
            "/admin/ml/overview",
            token=access_token,
            params=params or None,
        )

    def admin_update_captcha_policy(
        self,
        access_token: str,
        upload_mode: str,
    ) -> dict:
        return self._request(
            "PATCH",
            "/admin/ml/policy",
            token=access_token,
            json_body={"upload_mode": str(upload_mode)},
        )

    def admin_export_captcha_dataset(
        self,
        access_token: str,
        captcha_type: str | None = None,
    ) -> bytes:
        response = self._request(
            "GET",
            "/admin/ml/dataset/export",
            token=access_token,
            params={"captcha_type": captcha_type} if captcha_type else None,
            raw_response=True,
        )
        return bytes(response.content)

    def admin_import_captcha_dataset(
        self,
        access_token: str,
        archive: bytes,
    ) -> dict:
        return self._request(
            "POST",
            "/admin/ml/dataset/import",
            token=access_token,
            data_body=bytes(archive),
            content_type="application/zip",
        )

    def admin_captcha_samples(
        self,
        access_token: str,
        *,
        captcha_type: str | None = None,
        model_version: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        params = {
            "limit": int(limit),
            "offset": int(offset),
        }
        if captcha_type:
            params["captcha_type"] = str(captcha_type)
        if model_version:
            params["model_version"] = str(model_version)
        return self._request(
            "GET",
            "/admin/ml/samples",
            token=access_token,
            params=params,
        )

    def admin_download_captcha_sample_image(
        self,
        access_token: str,
        sample_id: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/admin/ml/samples/{sample_id}/image",
            token=access_token,
            raw_response=True,
        )
        return bytes(response.content)

    def admin_delete_captcha_samples(
        self,
        access_token: str,
        sample_ids: list[str],
    ) -> dict:
        return self._request(
            "DELETE",
            "/admin/ml/samples",
            token=access_token,
            json_body={"sample_ids": [str(sample_id) for sample_id in sample_ids]},
        )

    def admin_create_captcha_model(
        self,
        access_token: str,
        payload: dict,
    ) -> dict:
        return self._request(
            "POST",
            "/admin/ml/models",
            token=access_token,
            json_body=payload,
        )

    def admin_activate_captcha_model(
        self,
        access_token: str,
        model_id: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/admin/ml/models/{model_id}/activate",
            token=access_token,
        )

    def admin_rename_captcha_model(
        self,
        access_token: str,
        model_id: str,
        display_name: str,
    ) -> dict:
        return self._request(
            "PATCH",
            f"/admin/ml/models/{model_id}",
            token=access_token,
            json_body={"display_name": str(display_name)},
        )

    def admin_use_builtin_captcha_model(
        self,
        access_token: str,
        captcha_type: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/admin/ml/models/{captcha_type}/use-builtin",
            token=access_token,
        )

    def admin_delete_captcha_model(
        self,
        access_token: str,
        model_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/admin/ml/models/{model_id}",
            token=access_token,
        )

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

    def admin_delete_archived_account(
        self,
        access_token: str,
        account_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/admin/accounts/{account_id}",
            token=access_token,
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

    def announcements(self, access_token: str, limit: int = 50) -> list[dict]:
        return self._request(
            "GET",
            "/announcements",
            token=access_token,
            params={"limit": limit},
        )

    def mark_announcement_read(
        self,
        access_token: str,
        announcement_id: str,
        *,
        startup_shown: bool = False,
        confirmed: bool = True,
    ) -> dict:
        payload = {"startup_shown": bool(startup_shown)}
        if not confirmed:
            payload["confirmed"] = False
        return self._request(
            "POST",
            f"/announcements/{announcement_id}/read",
            token=access_token,
            json_body=payload,
        )

    def send_admin_message(
        self,
        access_token: str,
        announcement_id: str,
        message: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        payload = {"message": str(message)}
        if attachments:
            payload["attachments"] = list(attachments)
        return self._request(
            "POST",
            f"/announcements/{announcement_id}/messages",
            token=access_token,
            json_body=payload,
        )

    def contact_conversations(
        self,
        access_token: str,
        *,
        announcement_id: str | None = None,
        limit: int = 50,
        mark_read: bool = True,
    ) -> dict:
        params = {"limit": int(limit), "mark_read": bool(mark_read)}
        if announcement_id:
            params["announcement_id"] = str(announcement_id)
        return self._request(
            "GET",
            "/messages",
            token=access_token,
            params=params,
        )

    def mark_contact_conversation_read(
        self,
        access_token: str,
        message_id: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/messages/{message_id}/read",
            token=access_token,
        )

    def contact_conversation_unread_count(self, access_token: str) -> int:
        result = self._request(
            "GET",
            "/messages/unread-count",
            token=access_token,
        )
        return int((result or {}).get("unread_count") or 0)

    def reply_admin_message(
        self,
        access_token: str,
        message_id: str,
        message: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        payload = {"message": str(message)}
        if attachments:
            payload["attachments"] = list(attachments)
        return self._request(
            "POST",
            f"/messages/{message_id}/replies",
            token=access_token,
            json_body=payload,
        )

    def update_contact_status(
        self,
        access_token: str,
        message_id: str,
        status: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/messages/{message_id}/status",
            token=access_token,
            json_body={"status": str(status)},
        )

    def download_announcement_attachment(
        self,
        access_token: str,
        attachment_id: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/announcements/attachments/{attachment_id}",
            token=access_token,
            raw_response=True,
        )
        return bytes(response.content)

    def download_contact_attachment(
        self,
        access_token: str,
        attachment_id: str,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/messages/attachments/{attachment_id}",
            token=access_token,
            raw_response=True,
        )
        return bytes(response.content)

    def admin_announcements(
        self,
        access_token: str,
        limit: int = 100,
    ) -> list[dict]:
        return self._request(
            "GET",
            "/admin/announcements",
            token=access_token,
            params={"limit": limit},
        )

    def admin_create_announcement(
        self,
        access_token: str,
        payload: dict,
    ) -> dict:
        return self._request(
            "POST",
            "/admin/announcements",
            token=access_token,
            json_body=payload,
        )

    def admin_update_announcement(
        self,
        access_token: str,
        announcement_id: str,
        payload: dict,
    ) -> dict:
        return self._request(
            "PATCH",
            f"/admin/announcements/{announcement_id}",
            token=access_token,
            json_body=payload,
        )

    def admin_delete_announcement(
        self,
        access_token: str,
        announcement_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/admin/announcements/{announcement_id}",
            token=access_token,
        )

    def admin_add_announcement_attachment(
        self,
        access_token: str,
        announcement_id: str,
        payload: dict,
    ) -> dict:
        return self._request(
            "POST",
            f"/admin/announcements/{announcement_id}/attachments",
            token=access_token,
            json_body=payload,
        )

    def admin_delete_announcement_attachment(
        self,
        access_token: str,
        announcement_id: str,
        attachment_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/admin/announcements/{announcement_id}/attachments/{attachment_id}",
            token=access_token,
        )

    def admin_messages(
        self,
        access_token: str,
        *,
        unread_only: bool = False,
        limit: int = 200,
    ) -> dict:
        return self._request(
            "GET",
            "/admin/messages",
            token=access_token,
            params={
                "unread_only": bool(unread_only),
                "limit": limit,
            },
        )

    def admin_mark_message_read(
        self,
        access_token: str,
        message_id: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/admin/messages/{message_id}/read",
            token=access_token,
        )

    def admin_reply_message(
        self,
        access_token: str,
        message_id: str,
        message: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        payload = {"message": str(message)}
        if attachments:
            payload["attachments"] = list(attachments)
        return self._request(
            "POST",
            f"/admin/messages/{message_id}/reply",
            token=access_token,
            json_body=payload,
        )

    def admin_delete_message(
        self,
        access_token: str,
        message_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/admin/messages/{message_id}",
            token=access_token,
        )
