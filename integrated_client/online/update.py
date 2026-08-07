from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from ..config import APP_VERSION, get_data_dir
from ..platform_support import (
    UOS_UPDATE_PLATFORM,
    WINDOWS_UPDATE_PLATFORM,
    update_platform_key,
)
from .config import OnlineConfig

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_PATH_PATTERN = re.compile(
    r"^/updates/files/[A-Za-z0-9][A-Za-z0-9._-]*\.(?:exe|deb)$",
    re.IGNORECASE,
)
_MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    """Raised when the user stops an in-progress update download."""


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
    package_kind: str = "full"
    from_version: str | None = None
    platform_key: str = ""

    @property
    def is_delta(self) -> bool:
        return self.package_kind == "delta"


class UpdateClient:
    def __init__(
        self,
        config: OnlineConfig,
        session: requests.Session | None = None,
        current_version: str | None = None,
        platform_key: str | None = None,
    ):
        self.config = config.validate()
        self.current_version = str(current_version or APP_VERSION).strip()
        version_key(self.current_version)
        self.platform_key = str(
            platform_key or update_platform_key()
        ).strip().casefold()
        if self.platform_key not in {
            WINDOWS_UPDATE_PLATFORM,
            UOS_UPDATE_PLATFORM,
        }:
            raise UpdateError("当前操作系统或处理器架构不支持在线更新")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"IntDemoUpdater/{self.current_version}",
                "X-IntDemo-Version": self.current_version,
                "X-IntDemo-Platform": self.platform_key,
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

    def _validated_package(self, payload: dict, *, label: str) -> dict:
        installer_path = str(payload.get("installer_path") or "").strip()
        if not _PACKAGE_PATH_PATTERN.fullmatch(installer_path):
            raise UpdateError(f"服务器{label}路径无效")
        expected_suffix = (
            ".exe"
            if self.platform_key == WINDOWS_UPDATE_PLATFORM
            else ".deb"
        )
        if not installer_path.casefold().endswith(expected_suffix):
            raise UpdateError(f"服务器{label}与当前平台不匹配")
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
            raise UpdateError(f"{label}必须来自当前 HTTPS 服务器")

        sha256 = str(payload.get("sha256") or "").strip().lower()
        if not _HASH_PATTERN.fullmatch(sha256):
            raise UpdateError(f"服务器{label}校验值无效")
        try:
            size = int(payload.get("size"))
        except (TypeError, ValueError) as exc:
            raise UpdateError(f"服务器{label}大小无效") from exc
        if not 0 < size <= _MAX_INSTALLER_BYTES:
            raise UpdateError(f"服务器{label}大小超出允许范围")
        return {
            "installer_url": installer_url,
            "installer_name": installer_path.rsplit("/", 1)[-1],
            "sha256": sha256,
            "size": size,
        }

    def _platform_manifest(self, payload: dict) -> dict:
        selected_platform = str(
            payload.get("selected_platform") or ""
        ).strip().casefold()
        if selected_platform and selected_platform != self.platform_key:
            raise UpdateError("服务器返回了其他平台的更新包")

        platforms = payload.get("platforms")
        if isinstance(platforms, dict):
            platform_payload = platforms.get(self.platform_key)
            if not isinstance(platform_payload, dict):
                raise UpdateError("服务器尚未发布当前平台的更新包")
            selected = dict(payload)
            for key in (
                "installer_path",
                "sha256",
                "size",
                "full",
                "delta",
                "deltas",
                "primary_kind",
                "primary_from_version",
            ):
                selected.pop(key, None)
            selected.update(platform_payload)
            selected["selected_platform"] = self.platform_key
            return selected

        # Schema-v1 manifests without a platform map were Windows-only.  A
        # server may still flatten a Linux package, but it must label it.
        if (
            self.platform_key != WINDOWS_UPDATE_PLATFORM
            and selected_platform != self.platform_key
        ):
            raise UpdateError("服务器更新清单不包含 UOS ARM64 安装包")
        return payload

    @staticmethod
    def _matching_delta(payload: dict, current_version: str) -> dict | None:
        candidates = payload.get("deltas")
        if candidates is None and isinstance(payload.get("delta"), dict):
            candidates = [payload["delta"]]
        if not isinstance(candidates, list):
            return None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            from_versions = candidate.get("from_versions")
            if from_versions is None:
                from_versions = [candidate.get("from_version")]
            if not isinstance(from_versions, list):
                continue
            if current_version in {
                str(item or "").strip() for item in from_versions
            }:
                return candidate
        return None

    def check(self) -> UpdateInfo | None:
        try:
            response = self.session.get(
                self.manifest_url,
                allow_redirects=False,
                **self._request_kwargs(),
            )
            response.raise_for_status()
            if response.status_code == 204:
                return None
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UpdateError(f"检查更新失败：{exc}") from exc

        if payload.get("paused") is True:
            return None
        if int(payload.get("schema_version") or 0) != 1:
            raise UpdateError("服务器更新清单版本不受支持")
        if str(payload.get("channel") or "") != self.config.channel:
            raise UpdateError("服务器更新通道与客户端不匹配")

        version = str(payload.get("version") or "").strip()
        if version_key(version) <= version_key(self.current_version):
            return None

        payload = self._platform_manifest(payload)

        full_payload = (
            payload.get("full")
            if isinstance(payload.get("full"), dict)
            else payload
        )
        package = self._validated_package(full_payload, label="全量更新包")
        package_kind = "full"
        from_version = None
        delta = self._matching_delta(payload, self.current_version)
        if delta is not None:
            try:
                package = self._validated_package(delta, label="增量更新包")
            except UpdateError:
                # A malformed optional delta must never prevent the full update.
                package = self._validated_package(
                    full_payload,
                    label="全量更新包",
                )
            else:
                package_kind = "delta"
                from_version = self.current_version

        return UpdateInfo(
            version=version,
            **package,
            notes=str(payload.get("notes") or "").strip(),
            mandatory=bool(payload.get("mandatory", False)),
            package_kind=package_kind,
            from_version=from_version,
            platform_key=self.platform_key,
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def download(
        self,
        update: UpdateInfo,
        progress_callback=None,
        speed_callback=None,
        cancelled_callback=None,
    ) -> Path:
        update_dir = get_data_dir() / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        destination = update_dir / update.installer_name
        partial = destination.with_suffix(destination.suffix + ".part")

        def ensure_not_cancelled():
            if cancelled_callback is not None and cancelled_callback():
                raise UpdateCancelled("更新下载已停止")

        ensure_not_cancelled()
        if (
            destination.is_file()
            and destination.stat().st_size == update.size
            and self._file_sha256(destination) == update.sha256
        ):
            if progress_callback is not None:
                progress_callback(update.size, update.size)
            if speed_callback is not None:
                speed_callback(0.0)
            return destination

        partial.unlink(missing_ok=True)
        if progress_callback is not None:
            progress_callback(0, update.size)
        if speed_callback is not None:
            speed_callback(0.0)
        started_at = time.monotonic()
        speed_sample_at = started_at
        speed_sample_bytes = 0
        smoothed_speed = 0.0
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
                    for block in response.iter_content(chunk_size=256 * 1024):
                        ensure_not_cancelled()
                        if not block:
                            continue
                        total += len(block)
                        if total > update.size or total > _MAX_INSTALLER_BYTES:
                            raise UpdateError(
                                "下载内容超过更新清单声明的大小"
                            )
                        digest.update(block)
                        stream.write(block)
                        now = time.monotonic()
                        elapsed = now - speed_sample_at
                        if elapsed >= 0.25:
                            current_speed = (
                                (total - speed_sample_bytes) / max(elapsed, 0.001)
                            )
                            smoothed_speed = (
                                current_speed
                                if smoothed_speed <= 0
                                else smoothed_speed * 0.65 + current_speed * 0.35
                            )
                            speed_sample_at = now
                            speed_sample_bytes = total
                            if speed_callback is not None:
                                speed_callback(smoothed_speed)
                        if progress_callback is not None:
                            progress_callback(total, update.size)
                ensure_not_cancelled()
            if total != update.size:
                raise UpdateError(
                    f"更新包大小不一致（应为 {update.size}，实际 {total}）"
                )
            if digest.hexdigest() != update.sha256:
                raise UpdateError("更新包 SHA-256 校验失败，文件已丢弃")
            os.replace(partial, destination)
            if speed_callback is not None:
                elapsed = max(time.monotonic() - started_at, 0.001)
                speed_callback(total / elapsed)
            return destination
        except UpdateCancelled:
            raise
        except requests.RequestException as exc:
            raise UpdateError(f"下载更新失败：{exc}") from exc
        finally:
            partial.unlink(missing_ok=True)
