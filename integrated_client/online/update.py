from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from ..config import APP_VERSION, get_data_dir
from .config import OnlineConfig

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INSTALLER_PATH_PATTERN = re.compile(
    r"^/updates/files/[A-Za-z0-9][A-Za-z0-9._-]*\.exe$",
    re.IGNORECASE,
)
_MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, int, int, int]:
    normalized = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(normalized):
        raise UpdateError(f"无效的版本号：{normalized or '-'}")
    parts = [int(part) for part in normalized.split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    installer_url: str
    installer_name: str
    sha256: str
    size: int
    notes: str
    mandatory: bool = False


class UpdateClient:
    def __init__(
        self,
        config: OnlineConfig,
        session: requests.Session | None = None,
    ):
        self.config = config.validate()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"IntDemoUpdater/{APP_VERSION}",
            }
        )

    @property
    def manifest_url(self) -> str:
        return (
            f"{self.config.base_url.rstrip('/')}/updates/"
            f"{self.config.channel}.json"
        )

    def _request_kwargs(self) -> dict:
        return {
            "timeout": (
                self.config.connect_timeout,
                self.config.read_timeout,
            ),
            "verify": self.config.ca_bundle or True,
        }

    def check(self) -> UpdateInfo | None:
        try:
            response = self.session.get(
                self.manifest_url,
                allow_redirects=False,
                **self._request_kwargs(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UpdateError(f"检查更新失败：{exc}") from exc

        if int(payload.get("schema_version") or 0) != 1:
            raise UpdateError("服务器更新清单版本不受支持")
        if str(payload.get("channel") or "") != self.config.channel:
            raise UpdateError("服务器更新通道与客户端不匹配")

        version = str(payload.get("version") or "").strip()
        if version_key(version) <= version_key(APP_VERSION):
            return None

        installer_path = str(payload.get("installer_path") or "").strip()
        if not _INSTALLER_PATH_PATTERN.fullmatch(installer_path):
            raise UpdateError("服务器更新包路径无效")
        installer_url = urljoin(
            f"{self.config.base_url.rstrip('/')}/",
            installer_path,
        )
        base = urlparse(self.config.base_url)
        target = urlparse(installer_url)
        if (
            target.scheme.lower() != "https"
            or target.netloc.casefold() != base.netloc.casefold()
        ):
            raise UpdateError("更新包必须来自当前 HTTPS 服务器")

        sha256 = str(payload.get("sha256") or "").strip().lower()
        if not _HASH_PATTERN.fullmatch(sha256):
            raise UpdateError("服务器更新包校验值无效")
        try:
            size = int(payload.get("size"))
        except (TypeError, ValueError) as exc:
            raise UpdateError("服务器更新包大小无效") from exc
        if not 0 < size <= _MAX_INSTALLER_BYTES:
            raise UpdateError("服务器更新包大小超出允许范围")

        return UpdateInfo(
            version=version,
            installer_url=installer_url,
            installer_name=installer_path.rsplit("/", 1)[-1],
            sha256=sha256,
            size=size,
            notes=str(payload.get("notes") or "").strip(),
            mandatory=bool(payload.get("mandatory", False)),
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def download(self, update: UpdateInfo) -> Path:
        update_dir = get_data_dir() / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        destination = update_dir / update.installer_name
        partial = destination.with_suffix(destination.suffix + ".part")
        if (
            destination.is_file()
            and destination.stat().st_size == update.size
            and self._file_sha256(destination) == update.sha256
        ):
            return destination

        partial.unlink(missing_ok=True)
        try:
            with self.session.get(
                update.installer_url,
                stream=True,
                allow_redirects=False,
                **self._request_kwargs(),
            ) as response:
                response.raise_for_status()
                total = 0
                digest = hashlib.sha256()
                with partial.open("wb") as stream:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if not block:
                            continue
                        total += len(block)
                        if total > update.size or total > _MAX_INSTALLER_BYTES:
                            raise UpdateError("下载内容超过更新清单声明的大小")
                        digest.update(block)
                        stream.write(block)
            if total != update.size:
                raise UpdateError(
                    f"更新包大小不一致（应为 {update.size}，实际 {total}）"
                )
            if digest.hexdigest() != update.sha256:
                raise UpdateError("更新包 SHA-256 校验失败，文件已丢弃")
            os.replace(partial, destination)
            return destination
        except requests.RequestException as exc:
            raise UpdateError(f"下载更新失败：{exc}") from exc
        finally:
            partial.unlink(missing_ok=True)
