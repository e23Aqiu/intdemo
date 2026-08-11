from __future__ import annotations

import getpass
import platform
import uuid
from typing import Any
from urllib.parse import urlparse

import requests


class ConnectionControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "connection_control_error",
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def control_device_uid() -> str:
    identity = (
        f"{platform.node().strip().casefold()}|"
        f"{getpass.getuser().strip().casefold()}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"intdemo-publisher:{identity}"))


class ConnectionControlClient:
    """Transient admin client used only by the developer release publisher."""

    def __init__(
        self,
        base_url: str,
        *,
        ca_bundle: str = "",
        client_version: str = "developer",
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        device_uid: str | None = None,
    ):
        normalized = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConnectionControlError("客户端连接测试要求有效的 HTTPS 服务地址")
        self.api_url = f"{normalized}/api/v1"
        self.ca_bundle = str(ca_bundle or "").strip()
        self.client_version = str(client_version or "developer").strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.device_uid = str(device_uid or control_device_uid())
        self.access_token = ""
        self.refresh_token = ""
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": (
                    f"IntDemoReleasePublisher/{self.client_version}"
                ),
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        token: bool = True,
    ) -> Any:
        headers = {}
        if token:
            if not self.access_token:
                raise ConnectionControlError("请先验证管理员账号")
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            response = self.session.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                json=json_body,
                timeout=self.timeout,
                verify=self.ca_bundle or True,
            )
        except requests.exceptions.SSLError as exc:
            raise ConnectionControlError(
                f"TLS 证书验证失败：{exc}",
                code="tls_verification_failed",
            ) from exc
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            raise ConnectionControlError(
                f"无法连接服务器：{exc}",
                code="network_unavailable",
            ) from exc
        except requests.RequestException as exc:
            raise ConnectionControlError(
                f"服务器请求失败：{exc}",
                code="request_failed",
            ) from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ConnectionControlError(
                str(
                    payload.get("message")
                    or f"服务器返回 HTTP {response.status_code}"
                ),
                code=str(payload.get("code") or "http_error"),
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectionControlError("服务器返回了无效的 JSON") from exc

    def login(self, username: str, password: str) -> dict:
        username = str(username or "").strip().lower()
        if not username or not password:
            raise ConnectionControlError("请输入服务器管理员账号和密码")
        bundle = self._request(
            "POST",
            "/auth/login",
            token=False,
            json_body={
                "username": username,
                "password": password,
                "device_uid": self.device_uid,
                "device_name": "IntDemo 打包器连接测试控制台",
                "client_version": (
                    f"release-publisher-control/{self.client_version}"
                ),
                "control_client": True,
            },
        )
        account = bundle.get("account") or {}
        if account.get("role") != "admin":
            raise ConnectionControlError(
                "连接测试控制仅允许服务器管理员使用",
                code="admin_required",
                status_code=403,
            )
        if account.get("must_change_password"):
            raise ConnectionControlError(
                "该管理员必须先在业务客户端修改初始密码",
                code="password_change_required",
                status_code=403,
            )
        self.access_token = str(bundle.get("access_token") or "")
        self.refresh_token = str(bundle.get("refresh_token") or "")
        if not self.access_token:
            raise ConnectionControlError("服务器登录响应缺少访问令牌")
        return bundle

    def list_clients(self) -> dict:
        return self._request("GET", "/admin/connection-test/clients")

    def disconnect_device(self, device_id: str) -> dict:
        return self._request(
            "POST",
            f"/admin/connection-test/devices/{device_id}/disconnect",
        )

    def restore_device(self, device_id: str) -> dict:
        return self._request(
            "POST",
            f"/admin/connection-test/devices/{device_id}/restore",
        )

    def disconnect_all(self) -> dict:
        return self._request(
            "POST",
            "/admin/connection-test/all/disconnect",
        )

    def restore_all(self) -> dict:
        return self._request(
            "POST",
            "/admin/connection-test/all/restore",
        )

    def logout(self) -> None:
        if not self.access_token:
            return
        try:
            self._request(
                "POST",
                "/auth/logout",
                json_body={"refresh_token": self.refresh_token or None},
            )
        except ConnectionControlError:
            pass
        finally:
            self.access_token = ""
            self.refresh_token = ""
