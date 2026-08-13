"""Secure, offline management for the optional enhanced trainer component."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .config import APP_VERSION, get_data_dir
from .platform_support import (
    UOS_UPDATE_PLATFORM,
    WINDOWS_UPDATE_PLATFORM,
    system_application_environment,
    system_application_launch_context,
    update_platform_key,
)

TRAINER_COMPONENT_ID = "intdemo-enhanced-trainer"
TRAINER_COMPONENT_SCHEMA_VERSION = 1
TRAINER_PROTOCOL_VERSION = 1
TRAINER_PACKAGE_SUFFIX = ".inttrainer"
TRAINER_OUTPUT_MODEL_NAME = "candidate.onnx"
TRAINER_OUTPUT_METADATA_NAME = "metadata.json"
TRAINER_OUTPUT_METRICS_NAME = "metrics.json"
SUPPORTED_TRAINER_PLATFORMS = {
    WINDOWS_UPDATE_PLATFORM,
    UOS_UPDATE_PLATFORM,
}

# These limits apply to uncompressed content.  They are intentionally above
# the expected PyTorch CPU component size while still bounding ZIP bombs.
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_COMPRESSION_RATIO = 250
SELF_TEST_TIMEOUT_SECONDS = 120
LOCK_METADATA_SCHEMA_VERSION = 1
REMOVAL_MARKER_SCHEMA_VERSION = 1

STATUS_NOT_INSTALLED = "not_installed"
STATUS_AVAILABLE = "available"
STATUS_DAMAGED = "damaged"
STATUS_INCOMPATIBLE = "incompatible"

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){2,3}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_INSTALLING_DIRECTORY_PATTERN = re.compile(r"^\.installing-[A-Za-z0-9_-]{6,64}$")
_REMOVAL_DIRECTORY_PATTERN = re.compile(r"^\.trainer-removing-([0-9a-f]{32})$")
_REMOVAL_MARKER_PATTERN = re.compile(
    r"^\.trainer-removing-([0-9a-f]{32})\.json$"
)
_VERSION_DIRECTORY_PATTERN = re.compile(
    r"^\d+(?:\.\d+){2,3}-(?:windows-x86_64|linux-aarch64)-"
    r"[0-9a-f]{12}-[0-9a-f]{8}$"
)
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class TrainerComponentError(RuntimeError):
    """Base error for component validation and lifecycle operations."""


class TrainerPackageError(TrainerComponentError):
    """The selected archive is malformed or violates package policy."""


class TrainerCompatibilityError(TrainerPackageError):
    """The package is not compatible with this client/platform."""


class TrainerSelfTestError(TrainerComponentError):
    """The component executable failed its mandatory self-test."""


class TrainerBusyError(TrainerComponentError):
    """A component lifecycle operation was attempted during training."""


@dataclass(frozen=True)
class TrainerComponentStatus:
    code: str
    message: str
    version: str = ""
    platform: str = ""
    component_path: Path | None = None
    numeric_method: str = ""
    click_method: str = ""
    last_self_test_at: str = ""
    installed_size: int = 0
    maintenance_required: bool = False
    cleanup_available: bool = False
    maintenance_message: str = ""

    @property
    def installed(self) -> bool:
        return self.code != STATUS_NOT_INSTALLED

    @property
    def available(self) -> bool:
        return self.code == STATUS_AVAILABLE

    @property
    def detail(self) -> str:
        """UI-friendly alias for the human-readable status message."""

        return self.message


@dataclass(frozen=True)
class TrainerInstallResult:
    status: TrainerComponentStatus
    replaced_version: str = ""


@dataclass(frozen=True)
class _LifecycleResiduals:
    descriptions: tuple[str, ...] = ()
    internal_size: int = 0
    external_size: int = 0
    cleanup_available: bool = False

    @property
    def present(self) -> bool:
        return bool(self.descriptions)


def _version_key(value: str) -> tuple[int, int, int, int]:
    normalized = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(normalized):
        raise TrainerPackageError(f"强化组件版本号无效：{normalized or '-'}")
    parts = [int(part) for part in normalized.split(".")]
    if any(part > 999_999 for part in parts):
        raise TrainerPackageError("强化组件版本号数值超出允许范围")
    return tuple((parts + [0, 0, 0, 0])[:4])


def _strict_json_loads(value: bytes, *, label: str) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite number: {value}")

    try:
        payload = json.loads(
            value.decode("utf-8-sig"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise TrainerPackageError(f"{label}不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise TrainerPackageError(f"{label}必须是 JSON 对象")
    return payload


def _normalized_archive_path(value: str, *, directory: bool = False) -> str:
    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("/"):
        raise TrainerPackageError("强化组件包含空路径、绝对路径或 NUL 字符")
    if len(raw) > 512 or any(ord(character) < 32 for character in raw):
        raise TrainerPackageError("强化组件路径过长或包含控制字符")
    if directory and raw.endswith("/"):
        raw = raw[:-1]
    raw_parts = raw.split("/")
    if any(part in {"", ".."} for part in raw_parts):
        raise TrainerPackageError(f"强化组件包含不安全路径：{value}")
    parts = [part for part in raw_parts if part != "."]
    if not parts:
        raise TrainerPackageError(f"强化组件包含无效路径：{value}")
    for part in parts:
        if ":" in part or part.endswith((" ", ".")):
            raise TrainerPackageError(f"强化组件包含 Windows 不安全路径：{value}")
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise TrainerPackageError(f"强化组件包含系统保留路径：{value}")
    normalized = str(PurePosixPath(*parts))
    if normalized in {"", "."}:
        raise TrainerPackageError(f"强化组件包含无效路径：{value}")
    return normalized


def _path_identity(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _regular_zip_member(info: zipfile.ZipInfo) -> tuple[bool, bool]:
    """Return ``(regular_file, directory)`` for a non-special ZIP member."""

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    unix_kind = stat.S_IFMT(unix_mode)
    is_directory = info.is_dir() or bool(info.external_attr & 0x10)
    if is_directory:
        if unix_kind not in {0, stat.S_IFDIR}:
            return False, False
        return False, True
    if unix_kind not in {0, stat.S_IFREG}:
        return False, False
    return True, False


class TrainerComponentManager:
    """Install and manage one platform-specific enhanced trainer component.

    Protocol v1 has two subcommands::

        intdemo-trainer self-test --protocol-version 1
        intdemo-trainer train --protocol-version 1 --dataset DATASET \
            --captcha-type {numeric,click} --output DIRECTORY

    A successful training invocation exits with code zero and writes
    ``candidate.onnx``, ``metadata.json`` and ``metrics.json`` in DIRECTORY.
    Runtime progress may be emitted as UTF-8 JSON Lines on stdout; launchers
    must treat unknown event fields as forward-compatible.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str | None = None,
        platform_key: str | None = None,
        current_version: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        self.data_dir = Path(data_dir) if data_dir is not None else get_data_dir()
        self.component_root = self.data_dir / "components" / "trainer"
        self.versions_root = self.component_root / "versions"
        self.active_path = self.component_root / "active.json"
        self.preference_path = self.component_root / "preference.json"
        # The lock must remain outside component_root: uninstall renames that
        # directory, which cannot safely carry an open lock file on Windows.
        self.training_lock_path = self.component_root.parent / ".trainer-operation.lock"
        self.platform_key = str(platform_key or update_platform_key()).casefold()
        self.current_version = str(current_version or APP_VERSION).strip()
        self.runner = runner or subprocess.run

    def _ensure_supported_runtime(self) -> None:
        if self.platform_key not in SUPPORTED_TRAINER_PLATFORMS:
            raise TrainerCompatibilityError(
                "当前系统不支持强化训练组件，仅支持 Windows x64 和 Linux ARM64"
            )
        _version_key(self.current_version)

    def _validate_manifest(self, manifest: dict) -> dict:
        required = {
            "schema_version",
            "component_id",
            "version",
            "platform",
            "protocol_version",
            "min_client_version",
            "entrypoint",
            "capabilities",
            "files",
        }
        allowed = required | {"max_client_version"}
        if set(manifest) != required and not (
            set(manifest) == allowed and "max_client_version" in manifest
        ):
            missing = sorted(required - set(manifest))
            unknown = sorted(set(manifest) - allowed)
            detail = []
            if missing:
                detail.append(f"缺少 {', '.join(missing)}")
            if unknown:
                detail.append(f"未知 {', '.join(unknown)}")
            raise TrainerPackageError(
                "强化组件清单字段无效" + (f"（{'；'.join(detail)}）" if detail else "")
            )
        if type(manifest.get("schema_version")) is not int or (
            manifest["schema_version"] != TRAINER_COMPONENT_SCHEMA_VERSION
        ):
            raise TrainerPackageError("强化组件清单版本不受支持")
        if manifest.get("component_id") != TRAINER_COMPONENT_ID:
            raise TrainerPackageError("所选文件不是 IntDemo 强化训练组件")
        version = str(manifest.get("version") or "").strip()
        _version_key(version)
        platform_name = str(manifest.get("platform") or "").strip().casefold()
        if platform_name not in SUPPORTED_TRAINER_PLATFORMS:
            raise TrainerPackageError("强化组件声明了不受支持的平台")
        if type(manifest.get("protocol_version")) is not int or (
            manifest["protocol_version"] != TRAINER_PROTOCOL_VERSION
        ):
            raise TrainerCompatibilityError("强化组件命令行协议版本不兼容")
        min_client_version = str(manifest.get("min_client_version") or "").strip()
        _version_key(min_client_version)
        max_client_version = manifest.get("max_client_version")
        if max_client_version is not None:
            max_client_version = str(max_client_version).strip()
            _version_key(max_client_version)
            if _version_key(max_client_version) < _version_key(min_client_version):
                raise TrainerPackageError("强化组件客户端版本范围无效")

        entrypoint = _normalized_archive_path(str(manifest.get("entrypoint") or ""))
        expected_entrypoint = (
            "bin/intdemo-trainer.exe"
            if platform_name == WINDOWS_UPDATE_PLATFORM
            else "bin/intdemo-trainer"
        )
        if entrypoint != expected_entrypoint:
            raise TrainerPackageError(
                f"强化组件入口程序必须是 {expected_entrypoint}"
            )

        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {
            "numeric",
            "click",
        }:
            raise TrainerPackageError("强化组件 capabilities 字段无效")
        normalized_capabilities = {}
        for captcha_type in ("numeric", "click"):
            method = str(capabilities.get(captcha_type) or "").strip().casefold()
            if not _CAPABILITY_PATTERN.fullmatch(method):
                raise TrainerPackageError(
                    f"强化组件 {captcha_type} 训练方法编号无效"
                )
            normalized_capabilities[captcha_type] = method

        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_ARCHIVE_FILES:
            raise TrainerPackageError("强化组件文件清单为空或文件数量超限")
        normalized_files = []
        identities = set()
        total_size = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise TrainerPackageError("强化组件文件条目字段无效")
            path = _normalized_archive_path(str(item.get("path") or ""))
            if _path_identity(path) == "manifest.json":
                raise TrainerPackageError("manifest.json 不能出现在文件哈希清单中")
            identity = _path_identity(path)
            if identity in identities:
                raise TrainerPackageError("强化组件清单包含重复的规范化路径")
            identities.add(identity)
            size = item.get("size")
            if type(size) is not int or size < 0 or size > MAX_MEMBER_BYTES:
                raise TrainerPackageError(f"强化组件文件大小无效：{path}")
            digest = str(item.get("sha256") or "").strip().casefold()
            if not _HASH_PATTERN.fullmatch(digest):
                raise TrainerPackageError(f"强化组件文件哈希无效：{path}")
            total_size += size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise TrainerPackageError("强化组件解压后总大小超出允许范围")
            normalized_files.append({"path": path, "size": size, "sha256": digest})
        if _path_identity(entrypoint) not in identities:
            raise TrainerPackageError("强化组件入口程序未列入文件清单")

        normalized = dict(manifest)
        normalized.update(
            {
                "version": version,
                "platform": platform_name,
                "min_client_version": min_client_version,
                "max_client_version": max_client_version,
                "entrypoint": entrypoint,
                "capabilities": normalized_capabilities,
                "files": normalized_files,
            }
        )
        return normalized

    def _check_compatibility(self, manifest: Mapping[str, object]) -> None:
        self._ensure_supported_runtime()
        if manifest["platform"] != self.platform_key:
            raise TrainerCompatibilityError(
                f"组件平台为 {manifest['platform']}，本机平台为 {self.platform_key}"
            )
        current = _version_key(self.current_version)
        if current < _version_key(str(manifest["min_client_version"])):
            raise TrainerCompatibilityError(
                f"强化组件要求客户端不低于 {manifest['min_client_version']}"
            )
        maximum = manifest.get("max_client_version")
        if maximum and current > _version_key(str(maximum)):
            raise TrainerCompatibilityError(
                f"强化组件仅兼容客户端 {maximum} 及以下版本"
            )

    def _read_archive(self, package_path: Path) -> tuple[zipfile.ZipFile, dict, bytes]:
        if package_path.suffix.casefold() != TRAINER_PACKAGE_SUFFIX:
            raise TrainerPackageError("请选择 .inttrainer 强化组件包")
        try:
            package_size = package_path.stat().st_size
        except OSError as exc:
            raise TrainerPackageError(f"无法读取强化组件包：{exc}") from exc
        if not 0 < package_size <= MAX_PACKAGE_BYTES:
            raise TrainerPackageError("强化组件包大小超出允许范围")
        try:
            archive = zipfile.ZipFile(package_path, "r")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise TrainerPackageError("强化组件包不是有效 ZIP 文件") from exc

        try:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES + 512:
                raise TrainerPackageError("强化组件包文件数量超出允许范围")
            members: dict[str, zipfile.ZipInfo] = {}
            identities: set[str] = set()
            uncompressed_size = 0
            for info in infos:
                regular, directory = _regular_zip_member(info)
                if not regular and not directory:
                    raise TrainerPackageError(
                        f"强化组件包含符号链接或特殊文件：{info.filename}"
                    )
                path = _normalized_archive_path(info.filename, directory=directory)
                identity = _path_identity(path)
                if identity in identities:
                    raise TrainerPackageError("强化组件包含重复的规范化路径")
                identities.add(identity)
                if info.flag_bits & 0x1:
                    raise TrainerPackageError("强化组件不允许使用 ZIP 密码加密")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise TrainerPackageError("强化组件使用了不支持的压缩算法")
                if directory:
                    if info.file_size != 0:
                        raise TrainerPackageError("强化组件目录条目的大小必须为零")
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise TrainerPackageError(f"强化组件文件过大：{path}")
                uncompressed_size += info.file_size
                if uncompressed_size > MAX_UNCOMPRESSED_BYTES + MAX_MANIFEST_BYTES:
                    raise TrainerPackageError("强化组件解压后总大小超出允许范围")
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size
                    > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO
                ):
                    raise TrainerPackageError("强化组件压缩比异常，已拒绝解压")
                members[identity] = info
            manifest_info = members.get("manifest.json")
            if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise TrainerPackageError("强化组件缺少有效的 manifest.json")
            try:
                manifest_bytes = archive.read(manifest_info)
            except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
                raise TrainerPackageError("无法读取强化组件清单") from exc
            raw_manifest = _strict_json_loads(
                manifest_bytes,
                label="强化组件清单",
            )
            manifest = self._validate_manifest(raw_manifest)
            self._check_compatibility(manifest)

            declared = {
                _path_identity(item["path"]): item
                for item in manifest["files"]
            }
            actual = {key for key in members if key != "manifest.json"}
            if actual != set(declared):
                missing = sorted(set(declared) - actual)
                extra = sorted(actual - set(declared))
                detail = []
                if missing:
                    detail.append(f"缺少 {', '.join(missing[:3])}")
                if extra:
                    detail.append(f"未登记 {', '.join(extra[:3])}")
                raise TrainerPackageError(
                    "强化组件内容与文件清单不一致"
                    + (f"（{'；'.join(detail)}）" if detail else "")
                )
            for identity, item in declared.items():
                if members[identity].file_size != item["size"]:
                    raise TrainerPackageError(
                        f"强化组件文件大小与清单不一致：{item['path']}"
                    )
            return archive, manifest, manifest_bytes
        except Exception:
            archive.close()
            raise

    @staticmethod
    def _safe_output_path(root: Path, relative_path: str) -> Path:
        root_resolved = root.resolve()
        destination = root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            destination.resolve().relative_to(root_resolved)
        except (OSError, ValueError) as exc:
            raise TrainerPackageError(
                f"强化组件解压路径越界：{relative_path}"
            ) from exc
        return destination

    def _extract_verified(
        self,
        archive: zipfile.ZipFile,
        manifest: Mapping[str, object],
        manifest_bytes: bytes,
        destination: Path,
    ) -> None:
        info_by_identity = {
            _path_identity(
                _normalized_archive_path(info.filename, directory=info.is_dir())
            ): info
            for info in archive.infolist()
            if not info.is_dir()
        }
        for item in manifest["files"]:
            info = info_by_identity[_path_identity(item["path"])]
            output_path = self._safe_output_path(destination, item["path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            total = 0
            try:
                with archive.open(info, "r") as source, output_path.open(
                    "xb"
                ) as target:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > item["size"] or total > MAX_MEMBER_BYTES:
                            raise TrainerPackageError(
                                f"强化组件文件解压大小超限：{item['path']}"
                            )
                        digest.update(block)
                        target.write(block)
            except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
                raise TrainerPackageError(
                    f"强化组件文件解压失败：{item['path']}"
                ) from exc
            if total != item["size"]:
                raise TrainerPackageError(
                    f"强化组件文件解压大小不一致：{item['path']}"
                )
            if digest.hexdigest() != item["sha256"]:
                raise TrainerPackageError(
                    f"强化组件文件 SHA-256 校验失败：{item['path']}"
                )
        (destination / "manifest.json").write_bytes(manifest_bytes)
        entrypoint = self._safe_output_path(destination, str(manifest["entrypoint"]))
        if self.platform_key == UOS_UPDATE_PLATFORM:
            entrypoint.chmod(entrypoint.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _run_self_test(self, directory: Path, manifest: Mapping[str, object]) -> None:
        entrypoint = self._safe_output_path(directory, str(manifest["entrypoint"]))
        command = [
            str(entrypoint),
            "self-test",
            "--protocol-version",
            str(TRAINER_PROTOCOL_VERSION),
        ]
        environment = system_application_environment()
        environment.update(
            {
                "INTDEMO_TRAINER_COMPONENT_DIR": str(directory),
                "INTDEMO_TRAINER_PROTOCOL_VERSION": str(TRAINER_PROTOCOL_VERSION),
            }
        )
        try:
            with system_application_launch_context():
                result = self.runner(
                    command,
                    cwd=str(directory),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=SELF_TEST_TIMEOUT_SECONDS,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TrainerSelfTestError(f"强化组件自检无法启动：{exc}") from exc
        if int(getattr(result, "returncode", -1)) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()[:500]
            raise TrainerSelfTestError(
                "强化组件自检失败" + (f"：{detail}" if detail else "")
            )
        try:
            payload = json.loads(str(getattr(result, "stdout", "") or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TrainerSelfTestError("强化组件自检返回了无效 JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TrainerSelfTestError("强化组件自检未返回成功状态")
        if payload.get("protocol_version") != TRAINER_PROTOCOL_VERSION:
            raise TrainerSelfTestError("强化组件自检协议版本不一致")
        if str(payload.get("component_version") or "") != manifest["version"]:
            raise TrainerSelfTestError("强化组件自检版本与清单不一致")

    def _active_payload(self) -> dict:
        if os.path.lexists(self.component_root) and self._is_link_like(
            self.component_root
        ):
            raise TrainerPackageError("强化组件安装目录是链接，已拒绝读取")
        if os.path.lexists(self.active_path) and self._is_link_like(self.active_path):
            raise TrainerPackageError("强化组件激活状态是链接，已拒绝读取")
        try:
            raw = self.active_path.read_bytes()
        except FileNotFoundError as exc:
            raise TrainerPackageError("强化组件尚未安装") from exc
        except OSError as exc:
            raise TrainerPackageError(f"无法读取强化组件激活状态：{exc}") from exc
        payload = _strict_json_loads(raw, label="强化组件激活状态")
        required = {
            "schema_version",
            "version",
            "platform",
            "directory",
            "manifest_sha256",
            "installed_at",
            "last_self_test_at",
        }
        if set(payload) != required or payload.get("schema_version") != 1:
            raise TrainerPackageError("强化组件激活状态格式无效")
        directory = _normalized_archive_path(str(payload.get("directory") or ""))
        parts = PurePosixPath(directory).parts
        if len(parts) != 2 or parts[0] != "versions":
            raise TrainerPackageError("强化组件激活目录无效")
        digest = str(payload.get("manifest_sha256") or "").casefold()
        if not _HASH_PATTERN.fullmatch(digest):
            raise TrainerPackageError("强化组件激活清单哈希无效")
        payload["directory"] = directory
        payload["manifest_sha256"] = digest
        return payload

    def _load_installed(self, *, verify_files: bool = True) -> tuple[dict, dict, Path]:
        active = self._active_payload()
        if not self.versions_root.is_dir() or self._is_link_like(self.versions_root):
            raise TrainerPackageError("强化组件版本根目录丢失或类型无效")
        directory = self._safe_output_path(self.component_root, active["directory"])
        if not directory.is_dir() or self._is_link_like(directory):
            raise TrainerPackageError("强化组件版本目录丢失或类型无效")
        manifest_path = directory / "manifest.json"
        try:
            if not manifest_path.is_file() or self._is_link_like(manifest_path):
                raise TrainerPackageError("强化组件清单丢失或类型无效")
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise TrainerPackageError("强化组件清单丢失或不可读") from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != active["manifest_sha256"]:
            raise TrainerPackageError("强化组件清单与激活记录不一致")
        raw_manifest = _strict_json_loads(
            manifest_bytes,
            label="已安装强化组件清单",
        )
        manifest = self._validate_manifest(raw_manifest)
        self._check_compatibility(manifest)
        if active["version"] != manifest["version"] or (
            active["platform"] != manifest["platform"]
        ):
            raise TrainerPackageError("强化组件清单与激活版本不一致")
        for item in manifest["files"]:
            path = self._safe_output_path(directory, item["path"])
            try:
                if not path.is_file() or self._is_link_like(path):
                    raise TrainerPackageError(
                        f"强化组件文件丢失或类型无效：{item['path']}"
                    )
                if path.stat().st_size != item["size"]:
                    raise TrainerPackageError(
                        f"强化组件文件大小异常：{item['path']}"
                    )
                if verify_files:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    if digest.hexdigest() != item["sha256"]:
                        raise TrainerPackageError(
                            f"强化组件文件校验失败：{item['path']}"
                        )
            except OSError as exc:
                raise TrainerPackageError(
                    f"无法检查强化组件文件：{item['path']}"
                ) from exc
        return active, manifest, directory

    def _get_status_read_only(
        self,
        *,
        verify_files: bool = True,
    ) -> TrainerComponentStatus:
        if os.path.lexists(self.component_root) and self._is_link_like(
            self.component_root
        ):
            return TrainerComponentStatus(
                code=STATUS_DAMAGED,
                message="强化组件安装目录是链接，已拒绝读取",
            )
        if not os.path.lexists(self.active_path):
            return TrainerComponentStatus(
                code=STATUS_NOT_INSTALLED,
                message="本机未安装强化训练组件",
            )
        try:
            active, manifest, directory = self._load_installed(
                verify_files=verify_files
            )
        except TrainerCompatibilityError as exc:
            version, platform_name = self._active_status_hints()
            return TrainerComponentStatus(
                code=STATUS_INCOMPATIBLE,
                message=str(exc),
                version=version,
                platform=platform_name,
            )
        except TrainerComponentError as exc:
            version, platform_name = self._active_status_hints()
            return TrainerComponentStatus(
                code=STATUS_DAMAGED,
                message=str(exc),
                version=version,
                platform=platform_name,
            )
        capabilities = manifest["capabilities"]
        return TrainerComponentStatus(
            code=STATUS_AVAILABLE,
            message="强化训练组件可用",
            version=manifest["version"],
            platform=manifest["platform"],
            component_path=directory,
            numeric_method=capabilities["numeric"],
            click_method=capabilities["click"],
            last_self_test_at=str(active.get("last_self_test_at") or ""),
            installed_size=self._component_storage_size(),
        )

    @staticmethod
    def _status_with_residuals(
        status: TrainerComponentStatus,
        residuals: _LifecycleResiduals,
        *,
        prefix: str = "",
    ) -> TrainerComponentStatus:
        descriptions = list(residuals.descriptions)
        if prefix:
            descriptions.insert(0, prefix)
        if not descriptions:
            return status
        return replace(
            status,
            installed_size=(
                max(status.installed_size, residuals.internal_size)
                + residuals.external_size
            ),
            maintenance_required=True,
            cleanup_available=residuals.cleanup_available,
            maintenance_message="；".join(descriptions),
        )

    def get_status(self, *, verify_files: bool = True) -> TrainerComponentStatus:
        """Return status after a best-effort, exclusively locked recovery pass."""

        try:
            with self._operation_lock(
                operation="status-maintenance",
                busy_message="强化训练或组件维护正在运行",
            ):
                _released, residuals = self._maintain_lifecycle_while_locked()
                status = self._get_status_read_only(verify_files=verify_files)
                return self._status_with_residuals(status, residuals)
        except TrainerBusyError:
            status = self._get_status_read_only(verify_files=verify_files)
            residuals = self._scan_lifecycle_residuals()
            if not residuals.present:
                return status
            return self._status_with_residuals(
                status,
                residuals,
                prefix="组件正在使用，检测到的残留本次未清理",
            )
        except OSError as exc:
            status = self._get_status_read_only(verify_files=verify_files)
            residuals = self._scan_lifecycle_residuals()
            if not residuals.present:
                return status
            return self._status_with_residuals(
                status,
                residuals,
                prefix=f"无法取得组件维护锁：{exc}",
            )

    def _component_storage_size(self) -> int:
        """Return logical bytes used by all locally retained component files."""

        return self._directory_storage_size(self.component_root)

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        try:
            if path.is_symlink() or bool(
                getattr(path, "is_junction", lambda: False)()
            ):
                return True
            attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            return bool(reparse_flag and attributes & reparse_flag)
        except OSError:
            return True

    @classmethod
    def _directory_storage_size(cls, root: Path) -> int:
        if not root.is_dir() or cls._is_link_like(root):
            return 0
        total = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    path = Path(entry.path)
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False) and not cls._is_link_like(
                        path
                    ):
                        pending.append(path)
                except OSError:
                    continue
        return total

    def _maintenance_active_name(self) -> str | None:
        if not os.path.lexists(self.active_path):
            return ""
        if self._is_link_like(self.active_path):
            return None
        try:
            return PurePosixPath(self._active_payload()["directory"]).name
        except TrainerComponentError:
            return None

    @staticmethod
    def _valid_removal_marker(path: Path, token: str) -> bool:
        try:
            payload = _strict_json_loads(
                path.read_bytes(),
                label="强化组件卸载事务标记",
            )
        except (OSError, TrainerComponentError):
            return False
        return payload == {
            "schema_version": REMOVAL_MARKER_SCHEMA_VERSION,
            "token": token,
            "component": "trainer",
            "directory": f".trainer-removing-{token}",
        }

    def _scan_lifecycle_residuals(self) -> _LifecycleResiduals:
        descriptions: list[str] = []
        internal_size = self._component_storage_size()
        external_size = 0
        installing_count = 0
        inactive_count = 0
        uncertain_versions = 0
        cleanup_available = False
        active_name = self._maintenance_active_name()

        try:
            if self.component_root.is_dir() and not self._is_link_like(
                self.component_root
            ):
                for path in self.component_root.iterdir():
                    if (
                        _INSTALLING_DIRECTORY_PATTERN.fullmatch(path.name)
                        and path.is_dir()
                        and not self._is_link_like(path)
                    ):
                        installing_count += 1
                        cleanup_available = True
                if self.versions_root.is_dir() and not self._is_link_like(
                    self.versions_root
                ):
                    for path in self.versions_root.iterdir():
                        if (
                            path.name == active_name
                            or not path.is_dir()
                            or self._is_link_like(path)
                            or not _VERSION_DIRECTORY_PATTERN.fullmatch(path.name)
                        ):
                            continue
                        if active_name is None:
                            uncertain_versions += 1
                        else:
                            inactive_count += 1
                            cleanup_available = True
        except OSError as exc:
            descriptions.append(f"无法完整检查组件目录：{exc}")

        if installing_count:
            descriptions.append(f"有 {installing_count} 个安装临时目录待清理")
        if inactive_count:
            descriptions.append(f"有 {inactive_count} 个非活动组件版本待清理")
        if uncertain_versions:
            descriptions.append(
                f"激活记录无效，{uncertain_versions} 个版本目录未自动清理"
            )

        components_root = self.component_root.parent
        marked_tokens: set[str] = set()
        removal_directories: dict[str, Path] = {}
        try:
            if components_root.is_dir() and not self._is_link_like(components_root):
                for path in components_root.iterdir():
                    marker_match = _REMOVAL_MARKER_PATTERN.fullmatch(path.name)
                    if marker_match and path.is_file() and not path.is_symlink():
                        token = marker_match.group(1)
                        if self._valid_removal_marker(path, token):
                            marked_tokens.add(token)
                        continue
                    directory_match = _REMOVAL_DIRECTORY_PATTERN.fullmatch(path.name)
                    if directory_match:
                        token = directory_match.group(1)
                        if path.is_dir() and not self._is_link_like(path):
                            removal_directories[token] = path
        except OSError as exc:
            descriptions.append(f"无法完整检查卸载残留：{exc}")

        confirmed = marked_tokens & removal_directories.keys()
        external_size = sum(
            self._directory_storage_size(removal_directories[token])
            for token in confirmed
        )
        if confirmed:
            descriptions.append(f"有 {len(confirmed)} 个卸载临时目录待清理")
            cleanup_available = True
        return _LifecycleResiduals(
            descriptions=tuple(descriptions),
            internal_size=internal_size,
            external_size=external_size,
            cleanup_available=cleanup_available,
        )

    @classmethod
    def _remove_owned_directory(
        cls,
        path: Path,
        *,
        parent: Path,
        pattern: re.Pattern[str],
    ) -> int:
        if path.parent != parent or not pattern.fullmatch(path.name):
            raise TrainerComponentError("强化组件维护路径校验失败")
        if not path.is_dir() or cls._is_link_like(path):
            raise TrainerComponentError("强化组件维护目录类型无效")
        parent_resolved = parent.resolve()
        resolved = path.resolve()
        if resolved.parent != parent_resolved:
            raise TrainerComponentError("强化组件维护路径越界")
        released = cls._directory_storage_size(path)
        shutil.rmtree(path)
        return released

    def _maintain_lifecycle_while_locked(
        self,
    ) -> tuple[int, _LifecycleResiduals]:
        """Recover interrupted lifecycle work; caller must own the kernel lock."""

        released = 0
        errors: list[str] = []
        components_root = self.component_root.parent

        try:
            if components_root.is_dir() and not self._is_link_like(components_root):
                markers: list[tuple[Path, str]] = []
                for path in components_root.iterdir():
                    match = _REMOVAL_MARKER_PATTERN.fullmatch(path.name)
                    if (
                        match
                        and path.is_file()
                        and not path.is_symlink()
                        and self._valid_removal_marker(path, match.group(1))
                    ):
                        markers.append((path, match.group(1)))
                for marker, token in markers:
                    removal = components_root / f".trainer-removing-{token}"
                    try:
                        if removal.exists():
                            released += self._remove_owned_directory(
                                removal,
                                parent=components_root,
                                pattern=_REMOVAL_DIRECTORY_PATTERN,
                            )
                        marker.unlink(missing_ok=True)
                    except (OSError, TrainerComponentError) as exc:
                        errors.append(f"清理卸载临时目录失败：{exc}")
        except OSError as exc:
            errors.append(f"检查卸载临时目录失败：{exc}")

        if self.component_root.is_dir() and not self._is_link_like(
            self.component_root
        ):
            try:
                installing = list(self.component_root.iterdir())
            except OSError as exc:
                installing = []
                errors.append(f"检查安装临时目录失败：{exc}")
            for path in installing:
                if not _INSTALLING_DIRECTORY_PATTERN.fullmatch(path.name):
                    continue
                try:
                    released += self._remove_owned_directory(
                        path,
                        parent=self.component_root,
                        pattern=_INSTALLING_DIRECTORY_PATTERN,
                    )
                except (OSError, TrainerComponentError) as exc:
                    errors.append(f"清理安装临时目录失败：{exc}")

            active_name = self._maintenance_active_name()
            if active_name is not None and self.versions_root.is_dir():
                try:
                    versions = list(self.versions_root.iterdir())
                except OSError as exc:
                    versions = []
                    errors.append(f"检查非活动组件版本失败：{exc}")
                for path in versions:
                    if (
                        path.name == active_name
                        or not _VERSION_DIRECTORY_PATTERN.fullmatch(path.name)
                    ):
                        continue
                    try:
                        released += self._remove_owned_directory(
                            path,
                            parent=self.versions_root,
                            pattern=_VERSION_DIRECTORY_PATTERN,
                        )
                    except (OSError, TrainerComponentError) as exc:
                        errors.append(f"清理非活动组件版本失败：{exc}")

        residuals = self._scan_lifecycle_residuals()
        if errors:
            residuals = replace(
                residuals,
                descriptions=tuple(errors) + residuals.descriptions,
            )
        return released, residuals

    def _active_status_hints(self) -> tuple[str, str]:
        """Read non-authoritative display hints from active.json after errors."""

        if os.path.lexists(self.component_root) and self._is_link_like(
            self.component_root
        ):
            return "", ""
        if os.path.lexists(self.active_path) and self._is_link_like(self.active_path):
            return "", ""
        try:
            payload = json.loads(self.active_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return "", ""
            version = str(payload.get("version") or "").strip()
            platform_name = str(payload.get("platform") or "").strip().casefold()
            return version[:64], platform_name[:64]
        except (OSError, UnicodeError, ValueError, TypeError):
            return "", ""

    def status(self, *, verify_files: bool = True) -> TrainerComponentStatus:
        """Return component state; short alias intended for UI callers."""

        return self.get_status(verify_files=verify_files)

    def preferred_mode(self) -> str:
        """Return ``standard`` or ``enhanced`` for the next training job.

        A stale enhanced preference is never reported when the component is
        unavailable, so a damaged or removed component safely falls back.
        """

        try:
            payload = _strict_json_loads(
                self.preference_path.read_bytes(),
                label="训练模式偏好",
            )
            if set(payload) != {"schema_version", "mode"}:
                return "standard"
            mode = str(payload.get("mode") or "").strip().casefold()
        except (OSError, TrainerComponentError):
            return "standard"
        if mode == "enhanced" and self.get_status(verify_files=False).available:
            return "enhanced"
        return "standard"

    def set_preferred_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().casefold()
        if normalized not in {"standard", "enhanced"}:
            raise ValueError("训练模式必须是 standard 或 enhanced")
        if normalized == "enhanced" and not self.get_status(
            verify_files=True
        ).available:
            raise TrainerComponentError("强化组件不可用，无法切换到强化训练模式")
        self._atomic_write_json(
            self.preference_path,
            {"schema_version": 1, "mode": normalized},
        )
        return normalized

    @contextmanager
    def _operation_lock(
        self,
        *,
        operation: str,
        busy_message: str,
    ) -> Iterator[None]:
        """Hold the cross-process lock shared by training and lifecycle work.

        The kernel lock, rather than lock-file existence or PID probing, is
        authoritative.  It is released automatically after an abnormal
        process exit, so stale PID metadata can be replaced without ever
        taking a lock away from a live owner.
        """

        lock_path = self.training_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    } and getattr(exc, "winerror", None) not in {32, 33}:
                        raise
                    raise TrainerBusyError(busy_message) from exc
            else:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    raise TrainerBusyError(busy_message) from exc
            acquired = True
            payload = json.dumps(
                {
                    "schema_version": LOCK_METADATA_SCHEMA_VERSION,
                    "pid": os.getpid(),
                    "operation": operation,
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("无法写入强化组件操作锁")
                remaining = remaining[written:]
            os.ftruncate(descriptor, len(payload))
            os.fsync(descriptor)
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def install(self, package_path: Path | str) -> TrainerInstallResult:
        self._ensure_supported_runtime()
        with self._operation_lock(
            operation="install",
            busy_message="强化训练或组件维护正在运行，无法安装或更新组件",
        ):
            return self._install_while_locked(package_path)

    def _install_while_locked(
        self,
        package_path: Path | str,
    ) -> TrainerInstallResult:
        _released, maintenance = self._maintain_lifecycle_while_locked()
        if maintenance.present:
            raise TrainerComponentError(
                "强化组件残留无法安全清理：" + maintenance.descriptions[0]
            )
        if os.path.lexists(self.component_root) and self._is_link_like(
            self.component_root
        ):
            raise TrainerComponentError("强化组件安装目录是链接，已拒绝安装")
        if os.path.lexists(self.component_root) and not self.component_root.is_dir():
            raise TrainerComponentError("强化组件安装路径不是目录，已拒绝安装")
        if os.path.lexists(self.versions_root) and (
            self._is_link_like(self.versions_root)
            or not self.versions_root.is_dir()
        ):
            raise TrainerComponentError("强化组件版本根目录类型无效，已拒绝安装")
        if os.path.lexists(self.active_path) and self._is_link_like(self.active_path):
            raise TrainerComponentError("强化组件激活状态是链接，已拒绝安装")
        previous = self._get_status_read_only(verify_files=False)
        try:
            previous_active_bytes = self.active_path.read_bytes()
        except FileNotFoundError:
            previous_active_bytes = None
        except OSError as exc:
            raise TrainerComponentError(
                f"无法备份当前强化组件激活状态：{exc}"
            ) from exc
        package = Path(package_path).expanduser().resolve()
        self.versions_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".installing-", dir=str(self.component_root))
        )
        active_switched = False
        try:
            archive, manifest, manifest_bytes = self._read_archive(package)
            try:
                self._extract_verified(archive, manifest, manifest_bytes, temporary)
            finally:
                archive.close()
            self._run_self_test(temporary, manifest)

            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            directory_name = (
                f"{manifest['version']}-{manifest['platform']}-"
                f"{manifest_digest[:12]}-{uuid.uuid4().hex[:8]}"
            )
            destination = self.versions_root / directory_name
            os.replace(temporary, destination)
            active_payload = {
                "schema_version": 1,
                "version": manifest["version"],
                "platform": manifest["platform"],
                "directory": f"versions/{directory_name}",
                "manifest_sha256": manifest_digest,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "last_self_test_at": datetime.now(timezone.utc).isoformat(),
            }
            self._atomic_write_json(self.active_path, active_payload)
            active_switched = True
            status = self._get_status_read_only(verify_files=True)
            if not status.available:
                raise TrainerComponentError(status.message)
            _released, maintenance = self._maintain_lifecycle_while_locked()
            status = self._status_with_residuals(
                replace(
                    status,
                    installed_size=self._component_storage_size(),
                ),
                maintenance,
            )
            return TrainerInstallResult(
                status=status,
                replaced_version=previous.version if previous.installed else "",
            )
        except Exception:
            if active_switched:
                try:
                    if previous_active_bytes is None:
                        self.active_path.unlink(missing_ok=True)
                    else:
                        previous_payload = _strict_json_loads(
                            previous_active_bytes,
                            label="原强化组件激活状态",
                        )
                        self._atomic_write_json(self.active_path, previous_payload)
                except Exception as rollback_error:  # noqa: BLE001 - rollback boundary
                    raise TrainerComponentError(
                        f"强化组件安装失败，且恢复原版本失败：{rollback_error}"
                    )
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            # A post-move failure may leave a canonical orphan version. The
            # next locked status/install/uninstall pass removes it after
            # protecting the directory named by a valid active.json.

    def self_test(self) -> TrainerComponentStatus:
        with self._operation_lock(
            operation="self-test",
            busy_message="强化训练或组件维护正在运行，无法执行组件自检",
        ):
            return self._self_test_while_locked()

    def _self_test_while_locked(self) -> TrainerComponentStatus:
        _released, maintenance = self._maintain_lifecycle_while_locked()
        active, manifest, directory = self._load_installed(verify_files=True)
        self._run_self_test(directory, manifest)
        active["last_self_test_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write_json(self.active_path, active)
        status = self._get_status_read_only(verify_files=False)
        return self._status_with_residuals(status, maintenance)

    def build_training_command(
        self,
        *,
        dataset_path: Path | str,
        captcha_type: str,
        output_dir: Path | str,
    ) -> list[str]:
        captcha = str(captcha_type or "").strip().casefold()
        if captcha not in {"numeric", "click"}:
            raise ValueError("captcha_type 必须是 numeric 或 click")
        _active, manifest, directory = self._load_installed(verify_files=True)
        entrypoint = self._safe_output_path(directory, manifest["entrypoint"])
        output = Path(output_dir).expanduser().resolve()
        component_root = self.component_root.resolve()
        try:
            output.relative_to(component_root)
        except ValueError:
            pass
        else:
            raise ValueError("强化训练输出目录不能位于强化组件安装目录内")
        return [
            str(entrypoint),
            "train",
            "--protocol-version",
            str(TRAINER_PROTOCOL_VERSION),
            "--dataset",
            str(Path(dataset_path).expanduser().resolve()),
            "--captcha-type",
            captcha,
            "--output",
            str(output),
        ]

    def training_environment(self) -> dict[str, str]:
        """Return a child-process environment safe for either supported OS."""

        _active, _manifest, directory = self._load_installed(verify_files=False)
        environment = system_application_environment()
        environment.pop("INTDEMO_TRAINER_SIGNING_PRIVATE_KEY", None)
        environment.update(
            {
                "INTDEMO_TRAINER_COMPONENT_DIR": str(directory),
                "INTDEMO_TRAINER_PROTOCOL_VERSION": str(TRAINER_PROTOCOL_VERSION),
            }
        )
        return environment

    def training_output_paths(
        self,
        output_dir: Path | str,
    ) -> dict[str, Path]:
        """Return the three protocol-v1 result paths expected from a trainer."""

        root = Path(output_dir).expanduser().resolve()
        return {
            "model": root / TRAINER_OUTPUT_MODEL_NAME,
            "metadata": root / TRAINER_OUTPUT_METADATA_NAME,
            "metrics": root / TRAINER_OUTPUT_METRICS_NAME,
        }

    def command_for(
        self,
        dataset_path: Path | str,
        captcha_type: str,
        output_dir: Path | str,
    ) -> list[str]:
        """Compatibility alias used by orchestration/UI code."""

        return self.build_training_command(
            dataset_path=dataset_path,
            captcha_type=captcha_type,
            output_dir=output_dir,
        )

    def active_executable(self) -> Path:
        _active, manifest, directory = self._load_installed(verify_files=True)
        return self._safe_output_path(directory, manifest["entrypoint"])

    @contextmanager
    def training_lock(self) -> Iterator[None]:
        with self._operation_lock(
            operation="training",
            busy_message="强化训练或组件维护正在运行",
        ):
            yield

    def uninstall(self) -> int:
        """Remove only the local trainer component and return released bytes."""

        with self._operation_lock(
            operation="uninstall",
            busy_message="强化训练或组件维护正在运行，请稍后再卸载组件",
        ):
            return self._uninstall_while_locked()

    def _uninstall_while_locked(self) -> int:
        released, maintenance = self._maintain_lifecycle_while_locked()
        if os.path.lexists(self.component_root) and (
            self._is_link_like(self.component_root)
            or not self.component_root.is_dir()
        ):
            raise TrainerComponentError("强化组件安装目录类型无效，已拒绝卸载")
        if not self.component_root.exists():
            if maintenance.present:
                raise TrainerComponentError(
                    "强化组件残留无法安全清理：" + maintenance.descriptions[0]
                )
            return released
        expected_parent = (self.data_dir / "components").resolve()
        root = self.component_root.resolve()
        if root.parent != expected_parent or root.name != "trainer":
            raise TrainerComponentError("强化组件卸载路径校验失败")
        released += self._directory_storage_size(root)
        token = uuid.uuid4().hex
        removal_path = expected_parent / f".trainer-removing-{token}"
        marker_path = expected_parent / f".trainer-removing-{token}.json"
        self._atomic_write_json(
            marker_path,
            {
                "schema_version": REMOVAL_MARKER_SCHEMA_VERSION,
                "token": token,
                "component": "trainer",
                "directory": removal_path.name,
            },
        )
        try:
            os.replace(root, removal_path)
            shutil.rmtree(removal_path)
        except Exception:
            # Once the root has been renamed, deletion may already be partial.
            # Keep the marker and tombstone for a later locked retry instead
            # of restoring a potentially damaged component as active.
            raise
        marker_path.unlink(missing_ok=True)
        # preference.json normally moves with component_root, but writing the
        # fallback outside the removed tree is intentionally unnecessary:
        # preferred_mode() defaults to standard when the file is absent.
        return released


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_DAMAGED",
    "STATUS_INCOMPATIBLE",
    "STATUS_NOT_INSTALLED",
    "TRAINER_COMPONENT_ID",
    "TRAINER_COMPONENT_SCHEMA_VERSION",
    "TRAINER_OUTPUT_METADATA_NAME",
    "TRAINER_OUTPUT_METRICS_NAME",
    "TRAINER_OUTPUT_MODEL_NAME",
    "TRAINER_PACKAGE_SUFFIX",
    "TRAINER_PROTOCOL_VERSION",
    "TrainerBusyError",
    "TrainerCompatibilityError",
    "TrainerComponentError",
    "TrainerComponentManager",
    "TrainerComponentStatus",
    "TrainerInstallResult",
    "TrainerPackageError",
    "TrainerSelfTestError",
]
