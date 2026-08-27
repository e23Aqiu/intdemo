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
from .uos_delta import (
    UOS_DELTA_ALGORITHM,
    UOS_DELTA_FORMAT,
    UosDeltaCache,
    UosDeltaCancelled,
    UosDeltaError,
)
from .uos_file_update import (
    UOS_FILE_UPDATE_FORMAT,
    UosFileStore,
    UosFileUpdateCancelled,
    UosFileUpdateError,
)
from .uos_layers import (
    UOS_LAYER_FORMAT,
    UosLayerCancelled,
    UosLayerError,
    UosLayerStore,
)

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_PATH_PATTERN = re.compile(
    r"^/updates/files/[A-Za-z0-9][A-Za-z0-9._-]*\.(?:exe|deb|intdelta|intlayer)$",
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
    full_installer_url: str = ""
    full_installer_name: str = ""
    full_sha256: str = ""
    full_size: int = 0
    delta_format: str = ""
    delta_algorithm: str = ""
    base_sha256: str = ""
    base_size: int = 0
    target_sha256: str = ""
    target_size: int = 0
    layer_format: str = ""
    file_format: str = ""
    source_layout_sha256: str = ""
    target_layout_sha256: str = ""

    @property
    def is_delta(self) -> bool:
        return self.package_kind in {"delta", "layered", "file"}

    @property
    def is_layered(self) -> bool:
        return self.package_kind in {"layered", "file"}

    @property
    def is_file_update(self) -> bool:
        return self.package_kind == "file"

    @property
    def is_uos_delta(self) -> bool:
        return self.package_kind == "delta" and self.platform_key == UOS_UPDATE_PLATFORM

    def full_fallback(self) -> UpdateInfo:
        if not (
            self.full_installer_url
            and self.full_installer_name
            and self.full_sha256
            and self.full_size > 0
        ):
            raise UpdateError("更新清单缺少可回退的完整安装包")
        return UpdateInfo(
            version=self.version,
            installer_url=self.full_installer_url,
            installer_name=self.full_installer_name,
            sha256=self.full_sha256,
            size=self.full_size,
            notes=self.notes,
            mandatory=self.mandatory,
            package_kind="full",
            platform_key=self.platform_key,
            full_installer_url=self.full_installer_url,
            full_installer_name=self.full_installer_name,
            full_sha256=self.full_sha256,
            full_size=self.full_size,
            target_sha256=self.full_sha256,
            target_size=self.full_size,
        )


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
        self.uos_delta_cache: UosDeltaCache | None = None
        self.uos_layer_store: UosLayerStore | None = None
        self.uos_file_store: UosFileStore | None = None
        if self.platform_key == UOS_UPDATE_PLATFORM:
            self.session.headers.update(
                {
                    "X-IntDemo-Update-Capabilities": (
                        f"{UOS_FILE_UPDATE_FORMAT}, {UOS_LAYER_FORMAT}, "
                        f"{UOS_DELTA_FORMAT}"
                    )
                }
            )
            self.uos_delta_cache = UosDeltaCache(
                current_version=self.current_version
            )
            self.uos_layer_store = UosLayerStore(
                current_version=self.current_version
            )
            self.uos_file_store = UosFileStore(
                current_version=self.current_version
            )
            try:
                self.uos_delta_cache.confirm_pending()
            except (OSError, UosDeltaError):
                # Cache damage must never stop the application or full update.
                pass

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

    def _validated_package(
        self,
        payload: dict,
        *,
        label: str,
        expected_suffix: str | None = None,
    ) -> dict:
        installer_path = str(payload.get("installer_path") or "").strip()
        if not _PACKAGE_PATH_PATTERN.fullmatch(installer_path):
            raise UpdateError(f"服务器{label}路径无效")
        expected_suffix = expected_suffix or (
            ".exe" if self.platform_key == WINDOWS_UPDATE_PLATFORM else ".deb"
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

    def _validated_uos_delta(self, payload: dict, *, full: dict) -> dict:
        if str(payload.get("format") or "").strip() != UOS_DELTA_FORMAT:
            raise UpdateError("服务器 UOS 增量包格式不受支持")
        if str(payload.get("algorithm") or "").strip() != UOS_DELTA_ALGORITHM:
            raise UpdateError("服务器 UOS 增量算法不受支持")
        package = self._validated_package(
            payload,
            label="UOS 增量更新包",
            expected_suffix=".intdelta",
        )
        base_sha256 = str(payload.get("base_sha256") or "").strip().lower()
        target_sha256 = str(payload.get("target_sha256") or "").strip().lower()
        if not _HASH_PATTERN.fullmatch(base_sha256):
            raise UpdateError("服务器 UOS 增量基线校验值无效")
        if not _HASH_PATTERN.fullmatch(target_sha256):
            raise UpdateError("服务器 UOS 增量目标校验值无效")
        try:
            base_size = int(payload.get("base_size"))
            target_size = int(payload.get("target_size"))
        except (TypeError, ValueError) as exc:
            raise UpdateError("服务器 UOS 增量基线或目标大小无效") from exc
        if not 0 < base_size <= _MAX_INSTALLER_BYTES:
            raise UpdateError("服务器 UOS 增量基线大小超出允许范围")
        if not 0 < target_size <= _MAX_INSTALLER_BYTES:
            raise UpdateError("服务器 UOS 增量目标大小超出允许范围")
        if target_sha256 != full["sha256"] or target_size != full["size"]:
            raise UpdateError("服务器 UOS 增量目标与完整 DEB 不一致")
        return {
            **package,
            "delta_format": UOS_DELTA_FORMAT,
            "delta_algorithm": UOS_DELTA_ALGORITHM,
            "base_sha256": base_sha256,
            "base_size": base_size,
            "target_sha256": target_sha256,
            "target_size": target_size,
        }

    def _validated_uos_layer(self, payload: dict) -> dict:
        if str(payload.get("format") or "").strip() != UOS_LAYER_FORMAT:
            raise UpdateError("服务器 UOS 分层更新格式不受支持")
        package = self._validated_package(
            payload,
            label="UOS 分层更新包",
            expected_suffix=".intlayer",
        )
        source_layout_sha256 = str(
            payload.get("source_layout_sha256") or ""
        ).strip().casefold()
        target_layout_sha256 = str(
            payload.get("target_layout_sha256") or ""
        ).strip().casefold()
        if not _HASH_PATTERN.fullmatch(source_layout_sha256):
            raise UpdateError("服务器 UOS 分层来源布局校验值无效")
        if not _HASH_PATTERN.fullmatch(target_layout_sha256):
            raise UpdateError("服务器 UOS 分层目标布局校验值无效")
        return {
            **package,
            "layer_format": UOS_LAYER_FORMAT,
            "source_layout_sha256": source_layout_sha256,
            "target_layout_sha256": target_layout_sha256,
        }

    def _validated_uos_file(self, payload: dict) -> dict:
        if str(payload.get("format") or "").strip() != UOS_FILE_UPDATE_FORMAT:
            raise UpdateError("服务器 UOS 逐文件更新格式不受支持")
        package = self._validated_package(
            payload,
            label="UOS 逐文件更新包",
            expected_suffix=".intlayer",
        )
        source_layout_sha256 = str(
            payload.get("source_layout_sha256") or ""
        ).strip().casefold()
        target_layout_sha256 = str(
            payload.get("target_layout_sha256") or ""
        ).strip().casefold()
        if not _HASH_PATTERN.fullmatch(source_layout_sha256):
            raise UpdateError("服务器 UOS 逐文件来源布局校验值无效")
        if not _HASH_PATTERN.fullmatch(target_layout_sha256):
            raise UpdateError("服务器 UOS 逐文件目标布局校验值无效")
        return {
            **package,
            "file_format": UOS_FILE_UPDATE_FORMAT,
            "source_layout_sha256": source_layout_sha256,
            "target_layout_sha256": target_layout_sha256,
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
                "layered_updates",
                "layers",
                "file_updates",
                "files",
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

    @staticmethod
    def _matching_layer(payload: dict, current_version: str) -> dict | None:
        candidates = payload.get("layered_updates")
        if candidates is None:
            candidates = payload.get("layers")
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

    @staticmethod
    def _matching_file(payload: dict, current_version: str) -> dict | None:
        candidates = payload.get("file_updates")
        if candidates is None:
            candidates = payload.get("files")
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
        full_package = self._validated_package(full_payload, label="全量更新包")
        package = dict(full_package)
        package_kind = "full"
        from_version = None
        file_update = (
            self._matching_file(payload, self.current_version)
            if self.platform_key == UOS_UPDATE_PLATFORM
            else None
        )
        if file_update is not None:
            try:
                package = self._validated_uos_file(file_update)
                store = self.uos_file_store
                if store is None or store.source_root(
                    version=self.current_version,
                    layout_sha256=package["source_layout_sha256"],
                ) is None:
                    raise UpdateError("本机没有可用的 UOS 逐文件更新基线")
            except UpdateError:
                package = dict(full_package)
            else:
                package_kind = "file"
                from_version = self.current_version

        layer = (
            self._matching_layer(payload, self.current_version)
            if self.platform_key == UOS_UPDATE_PLATFORM and package_kind == "full"
            else None
        )
        if layer is not None:
            try:
                package = self._validated_uos_layer(layer)
                store = self.uos_layer_store
                if store is None or store.source_root(
                    version=self.current_version,
                    layout_sha256=package["source_layout_sha256"],
                ) is None:
                    raise UpdateError("本机没有可用的 UOS 分层更新基线")
            except UpdateError:
                package = dict(full_package)
            else:
                package_kind = "layered"
                from_version = self.current_version

        delta = self._matching_delta(payload, self.current_version)
        if package_kind == "full" and delta is not None:
            try:
                if self.platform_key == UOS_UPDATE_PLATFORM:
                    package = self._validated_uos_delta(
                        delta,
                        full=full_package,
                    )
                    cache = self.uos_delta_cache
                    if cache is None or cache.locate_base(
                        version=self.current_version,
                        size=package["base_size"],
                        sha256=package["base_sha256"],
                    ) is None:
                        raise UpdateError("本机没有可用的 UOS 增量基线")
                else:
                    package = self._validated_package(
                        delta,
                        label="增量更新包",
                    )
            except UpdateError:
                # A malformed optional delta must never prevent the full update.
                package = dict(full_package)
            else:
                package_kind = "delta"
                from_version = self.current_version

        package.setdefault("target_sha256", full_package["sha256"])
        package.setdefault("target_size", full_package["size"])
        return UpdateInfo(
            version=version,
            **package,
            notes=str(payload.get("notes") or "").strip(),
            mandatory=bool(payload.get("mandatory", False)),
            package_kind=package_kind,
            from_version=from_version,
            platform_key=self.platform_key,
            full_installer_url=full_package["installer_url"],
            full_installer_name=full_package["installer_name"],
            full_sha256=full_package["sha256"],
            full_size=full_package["size"],
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
        state_callback=None,
    ) -> Path:
        if update.is_file_update:
            return self._download_uos_file(
                update,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )
        if update.is_layered:
            return self._download_uos_layer(
                update,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )
        if update.is_uos_delta:
            return self._download_uos_delta(
                update,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )

        destination = self._download_destination(update)
        result = self._download_file(
            update,
            destination,
            progress_callback=progress_callback,
            speed_callback=speed_callback,
            cancelled_callback=cancelled_callback,
        )
        if update.platform_key == UOS_UPDATE_PLATFORM:
            cache = self.uos_delta_cache
            if cache is None:
                raise UpdateError("UOS 更新缓存不可用")
            try:
                cache.register_pending(
                    result,
                    version=update.version,
                    size=update.size,
                    sha256=update.sha256,
                )
            except UosDeltaError as exc:
                raise UpdateError(str(exc)) from exc
        return result

    def _download_destination(self, update: UpdateInfo) -> Path:
        if update.platform_key == UOS_UPDATE_PLATFORM:
            cache = self.uos_delta_cache
            if cache is None:
                raise UpdateError("UOS 更新缓存不可用")
            return cache.pending_path(
                name=update.installer_name,
                version=update.version,
            )
        update_dir = get_data_dir() / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        return update_dir / update.installer_name

    def _download_file(
        self,
        update: UpdateInfo,
        destination: Path,
        *,
        progress_callback=None,
        speed_callback=None,
        cancelled_callback=None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
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

    def _download_uos_delta(
        self,
        update: UpdateInfo,
        *,
        progress_callback=None,
        speed_callback=None,
        cancelled_callback=None,
        state_callback=None,
    ) -> Path:
        cache = self.uos_delta_cache
        if cache is None:
            raise UpdateError("UOS 增量更新缓存不可用")

        def fallback(reason: str) -> Path:
            if state_callback is not None:
                state_callback(
                    "fallback_full",
                    f"{reason}，已自动切换完整 UOS 更新包。",
                )
            full = update.full_fallback()
            destination = self._download_destination(full)
            result = self._download_file(
                full,
                destination,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            try:
                return cache.register_pending(
                    result,
                    version=full.version,
                    size=full.size,
                    sha256=full.sha256,
                )
            except UosDeltaError as exc:
                raise UpdateError(str(exc)) from exc

        base = cache.locate_base(
            version=str(update.from_version or self.current_version),
            size=update.base_size,
            sha256=update.base_sha256,
        )
        if base is None:
            return fallback("本机增量基线不可用")
        try:
            patch_path = cache.patch_path(update.installer_name)
            self._download_file(
                update,
                patch_path,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            # Revalidate after the network transfer in case the user cleaned
            # the cache while the patch was downloading.
            base = cache.locate_base(
                version=str(update.from_version or self.current_version),
                size=update.base_size,
                sha256=update.base_sha256,
            )
            if base is None:
                raise UosDeltaError("本机增量基线已被清理")
            return cache.reconstruct(
                base_path=base,
                patch_path=patch_path,
                target_name=update.full_installer_name,
                target_version=update.version,
                target_size=update.target_size,
                target_sha256=update.target_sha256,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )
        except (UpdateCancelled, UosDeltaCancelled):
            raise UpdateCancelled("更新下载已停止")
        except (UpdateError, UosDeltaError, OSError) as exc:
            return fallback(str(exc) or "UOS 增量更新不可用")

    def _download_uos_layer(
        self,
        update: UpdateInfo,
        *,
        progress_callback=None,
        speed_callback=None,
        cancelled_callback=None,
        state_callback=None,
    ) -> Path:
        store = self.uos_layer_store
        cache = self.uos_delta_cache
        if store is None or cache is None:
            raise UpdateError("UOS 分层更新缓存不可用")

        def fallback(reason: str) -> Path:
            if state_callback is not None:
                state_callback(
                    "fallback_full",
                    f"{reason}，已自动切换完整 UOS 更新包。",
                )
            full = update.full_fallback()
            destination = self._download_destination(full)
            result = self._download_file(
                full,
                destination,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            try:
                return cache.register_pending(
                    result,
                    version=full.version,
                    size=full.size,
                    sha256=full.sha256,
                )
            except UosDeltaError as exc:
                raise UpdateError(str(exc)) from exc

        try:
            archive_path = store.archive_path(
                update.installer_name,
                from_version=str(update.from_version or self.current_version),
                target_version=update.version,
            )
            self._download_file(
                update,
                archive_path,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            return store.stage(
                archive_path,
                from_version=str(update.from_version or self.current_version),
                target_version=update.version,
                source_layout_sha256=update.source_layout_sha256,
                target_layout_sha256=update.target_layout_sha256,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )
        except (UpdateCancelled, UosLayerCancelled):
            raise UpdateCancelled("更新下载已停止")
        except (UpdateError, UosLayerError, OSError) as exc:
            return fallback(str(exc) or "UOS 分层更新不可用")

    def _download_uos_file(
        self,
        update: UpdateInfo,
        *,
        progress_callback=None,
        speed_callback=None,
        cancelled_callback=None,
        state_callback=None,
    ) -> Path:
        store = self.uos_file_store
        cache = self.uos_delta_cache
        if store is None or cache is None:
            raise UpdateError("UOS 逐文件更新缓存不可用")

        def fallback(reason: str) -> Path:
            if state_callback is not None:
                state_callback(
                    "fallback_full",
                    f"{reason}，已自动切换完整 UOS 更新包。",
                )
            full = update.full_fallback()
            destination = self._download_destination(full)
            result = self._download_file(
                full,
                destination,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            try:
                return cache.register_pending(
                    result,
                    version=full.version,
                    size=full.size,
                    sha256=full.sha256,
                )
            except UosDeltaError as exc:
                raise UpdateError(str(exc)) from exc

        try:
            archive_path = store.archive_path(
                update.installer_name,
                from_version=str(update.from_version or self.current_version),
                target_version=update.version,
            )
            self._download_file(
                update,
                archive_path,
                progress_callback=progress_callback,
                speed_callback=speed_callback,
                cancelled_callback=cancelled_callback,
            )
            return store.stage(
                archive_path,
                from_version=str(update.from_version or self.current_version),
                target_version=update.version,
                source_layout_sha256=update.source_layout_sha256,
                target_layout_sha256=update.target_layout_sha256,
                cancelled_callback=cancelled_callback,
                state_callback=state_callback,
            )
        except (UpdateCancelled, UosFileUpdateCancelled):
            raise UpdateCancelled("更新下载已停止")
        except (UpdateError, UosFileUpdateError, OSError) as exc:
            return fallback(str(exc) or "UOS 逐文件更新不可用")
