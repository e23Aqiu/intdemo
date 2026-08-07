from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class OnlineConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class OnlineConfig:
    base_url: str
    ca_bundle: str | None = None
    channel: str = "test"
    connect_timeout: float = 5.0
    read_timeout: float = 20.0

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1"

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.base_url)
        return parsed._replace(
            scheme="wss",
            path=f"{parsed.path.rstrip('/')}/api/v1/ws/updates",
            params="",
            query="",
            fragment="",
        ).geturl()

    def validate(self) -> OnlineConfig:
        parsed = urlparse(self.base_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise OnlineConfigurationError("在线服务地址必须是有效的 HTTPS 地址")
        try:
            is_ip = ipaddress.ip_address(parsed.hostname) is not None
        except ValueError:
            is_ip = False
        if is_ip and not self.ca_bundle:
            raise OnlineConfigurationError(
                "使用公网 IP 时必须配置 Caddy 私有 CA 根证书"
            )
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise OnlineConfigurationError(f"CA 根证书不存在：{self.ca_bundle}")
        if self.channel != "test":
            raise OnlineConfigurationError("v0.2 客户端只允许 test 发布通道")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> OnlineConfig:
        configured_path = path or os.environ.get("INTDEMO_CONNECTION_CONFIG")
        if configured_path:
            candidate = Path(configured_path).expanduser()
        else:
            executable_dir = Path(sys.executable).resolve().parent
            package_default = Path(__file__).with_name("client-online.json")
            external = executable_dir / "client-online.json"
            user_config = None
            if sys.platform.startswith("linux"):
                configured_root = os.environ.get("XDG_CONFIG_HOME", "").strip()
                user_config_root = (
                    Path(configured_root).expanduser()
                    if configured_root
                    else Path.home() / ".config"
                )
                user_config = (
                    user_config_root / "intdemo-client" / "client-online.json"
                )
            working_copy = Path.cwd() / "client-online.json"
            if external.is_file():
                candidate = external
            elif user_config is not None and user_config.is_file():
                candidate = user_config
            elif working_copy.is_file():
                candidate = working_copy
            else:
                candidate = package_default
        if not candidate.is_file():
            raise OnlineConfigurationError(
                "缺少 client-online.json，请先配置在线测试服务器地址"
            )
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OnlineConfigurationError(f"无法读取在线配置：{exc}") from exc
        base_url = str(
            os.environ.get("INTDEMO_SERVER_URL") or raw.get("base_url") or ""
        )
        ca_value = os.environ.get("INTDEMO_CA_BUNDLE")
        if ca_value is None:
            ca_value = raw.get("ca_bundle")
        if ca_value:
            ca_path = Path(str(ca_value)).expanduser()
            if not ca_path.is_absolute():
                ca_path = candidate.parent / ca_path
            ca_value = str(ca_path.resolve())
        return cls(
            base_url=base_url.strip().rstrip("/"),
            ca_bundle=ca_value or None,
            channel=str(raw.get("channel") or "test"),
            connect_timeout=float(raw.get("connect_timeout", 5)),
            read_timeout=float(raw.get("read_timeout", 20)),
        ).validate()
