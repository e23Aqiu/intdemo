from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ..config import APP_VERSION, get_data_dir
from .uos_layers import (
    UOS_LAYER_LAYOUT,
    UosLayerError,
    UosLayerStore,
    file_sha256,
    read_layout,
    validate_layout,
    validate_package_root,
)

UOS_FILE_UPDATE_FORMAT = "uos-file-update-v2"
UOS_FILE_LAYOUT_FORMAT = "uos-file-layout-v2"
UOS_FILE_LAYOUT = "file-layout.json"
UOS_FILE_MANIFEST = "manifest.json"

_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_NAME_PATTERN = re.compile(
    r"^IntDemo-UOS-arm64-Files-(?P<from>\d+\.\d+\.\d+)-to-"
    r"(?P<target>\d+\.\d+\.\d+)\.intlayer$",
    re.IGNORECASE,
)
_MANAGED_ROOTS = ("app", "browser")
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_DISK_RESERVE_BYTES = 128 * 1024 * 1024


class UosFileUpdateError(RuntimeError):
    pass


class UosFileUpdateCancelled(UosFileUpdateError):
    pass


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _layout_digest(payload: dict) -> str:
    normalized = dict(payload)
    normalized.pop("layout_sha256", None)
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def _validated_version(value: object, label: str) -> str:
    version = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise UosFileUpdateError(f"{label}版本无效")
    return version


def _validated_managed_path(value: object, label: str) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in text
        or str(path) != text
        or len(path.parts) < 2
        or path.parts[0] not in _MANAGED_ROOTS
    ):
        raise UosFileUpdateError(f"{label}路径不安全")
    return path


def _validated_link_target(
    value: object,
    parent: PurePosixPath,
    managed_root: str,
) -> str:
    text = str(value or "")
    target = PurePosixPath(text)
    if not text or target.is_absolute() or "\\" in text:
        raise UosFileUpdateError("UOS 逐文件布局包含不安全的符号链接")
    resolved: list[str] = list(parent.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if len(resolved) <= 1:
                raise UosFileUpdateError("UOS 逐文件符号链接超出管理目录")
            resolved.pop()
        else:
            resolved.append(part)
    if not resolved or resolved[0] != managed_root:
        raise UosFileUpdateError("UOS 逐文件符号链接超出管理目录")
    return text


def _scan_managed_tree(
    package_root: Path,
    *,
    expected_entries: dict[str, dict] | None = None,
) -> tuple[list[dict], int]:
    entries: list[dict] = []
    total_size = 0
    for root_name in _MANAGED_ROOTS:
        root = package_root / root_name
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UosFileUpdateError(f"UOS 管理目录不存在：{root_name}") from exc
        if not root.is_dir():
            raise UosFileUpdateError(f"UOS 管理路径不是目录：{root_name}")
        for directory, dir_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in list(dir_names):
                candidate = directory_path / name
                if not candidate.is_symlink():
                    continue
                dir_names.remove(name)
                relative = PurePosixPath(
                    root_name,
                    candidate.relative_to(root).as_posix(),
                )
                target = os.readlink(candidate)
                _validated_link_target(target, relative.parent, root_name)
                entries.append(
                    {"path": str(relative), "type": "symlink", "target": target}
                )
            for name in file_names:
                candidate = directory_path / name
                relative = PurePosixPath(
                    root_name,
                    candidate.relative_to(root).as_posix(),
                )
                if candidate.is_symlink():
                    expected = (expected_entries or {}).get(str(relative))
                    if isinstance(expected, dict) and expected.get("type") == "file":
                        try:
                            resolved = candidate.resolve(strict=True)
                        except (OSError, RuntimeError) as exc:
                            raise UosFileUpdateError(
                                f"UOS 复用文件链接无效：{relative}"
                            ) from exc
                        if not resolved.is_file():
                            raise UosFileUpdateError(
                                f"UOS 复用文件链接目标无效：{relative}"
                            )
                        file_stat = resolved.stat()
                        total_size += file_stat.st_size
                        entries.append(
                            {
                                "path": str(relative),
                                "type": "file",
                                "mode": stat.S_IMODE(file_stat.st_mode),
                                "size": file_stat.st_size,
                                "sha256": file_sha256(resolved),
                            }
                        )
                        continue
                    target = os.readlink(candidate)
                    _validated_link_target(target, relative.parent, root_name)
                    entries.append(
                        {
                            "path": str(relative),
                            "type": "symlink",
                            "target": target,
                        }
                    )
                    continue
                if not candidate.is_file():
                    raise UosFileUpdateError(
                        f"UOS 管理目录包含不支持的文件：{candidate}"
                    )
                file_stat = candidate.stat()
                total_size += file_stat.st_size
                entries.append(
                    {
                        "path": str(relative),
                        "type": "file",
                        "mode": stat.S_IMODE(file_stat.st_mode),
                        "size": file_stat.st_size,
                        "sha256": file_sha256(candidate),
                    }
                )
    entries.sort(key=lambda item: (str(item["path"]), str(item["type"])))
    return entries, total_size


def build_file_layout(package_root: str | Path, version: str) -> dict:
    root = Path(package_root).expanduser().resolve()
    version = _validated_version(version, "目标")
    try:
        layer_layout = read_layout(root, version=version)
        validate_package_root(root, layer_layout)
    except UosLayerError as exc:
        raise UosFileUpdateError(f"UOS 兼容布局无效：{exc}") from exc
    entries, total_size = _scan_managed_tree(root)
    layout = {
        "schema_version": 2,
        "format": UOS_FILE_LAYOUT_FORMAT,
        "platform": "linux-aarch64",
        "version": version,
        "bootstrap_sha256": layer_layout["bootstrap_sha256"],
        "layer_layout_sha256": layer_layout["layout_sha256"],
        "file_count": len(entries),
        "total_size": total_size,
        "entries": entries,
    }
    layout["layout_sha256"] = _layout_digest(layout)
    return layout


def validate_file_layout(payload: object, *, version: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise UosFileUpdateError("UOS 逐文件布局根节点必须是对象")
    if payload.get("schema_version") != 2:
        raise UosFileUpdateError("UOS 逐文件布局版本不受支持")
    if payload.get("format") != UOS_FILE_LAYOUT_FORMAT:
        raise UosFileUpdateError("UOS 逐文件布局格式不受支持")
    if payload.get("platform") != "linux-aarch64":
        raise UosFileUpdateError("UOS 逐文件布局平台无效")
    actual_version = _validated_version(payload.get("version"), "布局")
    if version is not None and actual_version != version:
        raise UosFileUpdateError("UOS 逐文件布局版本不匹配")
    for key, label in (
        ("bootstrap_sha256", "引导文件"),
        ("layer_layout_sha256", "兼容布局"),
    ):
        if not _HASH_PATTERN.fullmatch(str(payload.get(key) or "").casefold()):
            raise UosFileUpdateError(f"UOS 逐文件布局的{label}指纹无效")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise UosFileUpdateError("UOS 逐文件布局缺少文件清单")
    try:
        file_count = int(payload.get("file_count"))
        total_size = int(payload.get("total_size"))
    except (TypeError, ValueError) as exc:
        raise UosFileUpdateError("UOS 逐文件布局汇总字段无效") from exc
    if file_count != len(entries) or not 1 <= file_count <= _MAX_ARCHIVE_MEMBERS:
        raise UosFileUpdateError("UOS 逐文件布局文件数无效")
    if not 0 <= total_size <= _MAX_EXPANDED_BYTES:
        raise UosFileUpdateError("UOS 逐文件布局大小超出限制")
    seen: set[str] = set()
    regular_size = 0
    normalized_order: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise UosFileUpdateError("UOS 逐文件布局条目无效")
        path = _validated_managed_path(entry.get("path"), "UOS 逐文件")
        path_text = str(path)
        if path_text in seen:
            raise UosFileUpdateError("UOS 逐文件布局包含重复路径")
        seen.add(path_text)
        kind = str(entry.get("type") or "")
        normalized_order.append((path_text, kind))
        for parent in path.parents:
            parent_text = str(parent)
            if parent_text in seen:
                raise UosFileUpdateError("UOS 逐文件布局存在父路径类型冲突")
        if kind == "file":
            digest = str(entry.get("sha256") or "").casefold()
            try:
                size = int(entry.get("size"))
                mode = int(entry.get("mode"))
            except (TypeError, ValueError) as exc:
                raise UosFileUpdateError("UOS 逐文件布局文件字段无效") from exc
            if not _HASH_PATTERN.fullmatch(digest):
                raise UosFileUpdateError("UOS 逐文件布局文件校验值无效")
            if size < 0 or size > _MAX_EXPANDED_BYTES:
                raise UosFileUpdateError("UOS 逐文件布局文件大小无效")
            if mode < 0 or mode > 0o7777:
                raise UosFileUpdateError("UOS 逐文件布局文件权限无效")
            regular_size += size
            if regular_size > _MAX_EXPANDED_BYTES:
                raise UosFileUpdateError("UOS 逐文件布局总大小超出限制")
        elif kind == "symlink":
            _validated_link_target(entry.get("target"), path.parent, path.parts[0])
        else:
            raise UosFileUpdateError("UOS 逐文件布局条目类型不受支持")
    expected_order = sorted(normalized_order)
    if normalized_order != expected_order:
        raise UosFileUpdateError("UOS 逐文件布局没有按路径排序")
    if regular_size != total_size:
        raise UosFileUpdateError("UOS 逐文件布局汇总大小不一致")
    digest = str(payload.get("layout_sha256") or "").casefold()
    if not _HASH_PATTERN.fullmatch(digest) or digest != _layout_digest(payload):
        raise UosFileUpdateError("UOS 逐文件布局自身校验失败")
    return payload


def read_file_layout(package_root: str | Path, *, version: str | None = None) -> dict:
    path = Path(package_root).expanduser().resolve() / UOS_FILE_LAYOUT
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UosFileUpdateError(f"无法读取 UOS 逐文件布局：{path}") from exc
    return validate_file_layout(payload, version=version)


def write_file_layout(package_root: str | Path, version: str) -> Path:
    root = Path(package_root).expanduser().resolve()
    payload = build_file_layout(root, version)
    target = root / UOS_FILE_LAYOUT
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def validate_file_package_root(package_root: str | Path, layout: dict) -> None:
    root = Path(package_root).expanduser().resolve()
    validated = validate_file_layout(layout)
    try:
        layer_layout = read_layout(root, version=validated["version"])
    except UosLayerError as exc:
        raise UosFileUpdateError(f"UOS 兼容布局校验失败：{exc}") from exc
    if layer_layout["layout_sha256"] != validated["layer_layout_sha256"]:
        raise UosFileUpdateError("UOS 兼容布局与逐文件布局不一致")
    entries, total_size = _scan_managed_tree(
        root,
        expected_entries=_entry_map(validated),
    )
    if entries != validated["entries"] or total_size != validated["total_size"]:
        raise UosFileUpdateError("UOS 安装内容与逐文件布局不一致")
    entry_map = _entry_map(validated)
    app_entry = entry_map.get("app/intdemo-client")
    if not isinstance(app_entry, dict) or app_entry.get("type") != "file":
        raise UosFileUpdateError("UOS 逐文件布局缺少业务程序入口")
    logical_layers: dict[str, dict] = {
        "app": {
            "kind": "file",
            "path": "app/intdemo-client",
            "size": int(app_entry["size"]),
            "mode": int(app_entry["mode"]),
            "sha256": str(app_entry["sha256"]),
        }
    }
    for name, prefix, layer_path in (
        ("runtime", "app/_internal/", "app/_internal"),
        ("browser", "browser/", "browser"),
    ):
        layer_entries = []
        layer_size = 0
        for entry in validated["entries"]:
            path_text = str(entry["path"])
            if not path_text.startswith(prefix):
                continue
            normalized = {**entry, "path": path_text[len(prefix) :]}
            layer_entries.append(normalized)
            if entry["type"] == "file":
                layer_size += int(entry["size"])
        layer_entries.sort(key=lambda item: (str(item["path"]), str(item["type"])))
        logical_layers[name] = {
            "kind": "tree",
            "path": layer_path,
            "size": layer_size,
            "file_count": len(layer_entries),
            "sha256": hashlib.sha256(_canonical_bytes(layer_entries)).hexdigest(),
        }
    if logical_layers != layer_layout["layers"]:
        raise UosFileUpdateError("UOS 逐文件内容与兼容布局描述不一致")


def _zip_file(
    archive: zipfile.ZipFile,
    source: Path,
    archive_name: str,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (mode & 0o7777)) << 16
    with source.open("rb") as input_stream, archive.open(info, "w") as output:
        shutil.copyfileobj(input_stream, output, length=1024 * 1024)


def _entry_map(layout: dict) -> dict[str, dict]:
    return {str(entry["path"]): entry for entry in layout["entries"]}


def _diff_layouts(source_layout: dict, target_layout: dict) -> tuple[list[str], list[str]]:
    source_entries = _entry_map(source_layout)
    target_entries = _entry_map(target_layout)
    changed = sorted(
        path
        for path, descriptor in target_entries.items()
        if source_entries.get(path) != descriptor
    )
    deleted = sorted(path for path in source_entries if path not in target_entries)
    return changed, deleted


def create_file_archive(
    source_root: str | Path,
    target_root: str | Path,
    output: str | Path,
    *,
    from_version: str,
    target_version: str,
) -> dict:
    source_root = Path(source_root).expanduser().resolve()
    target_root = Path(target_root).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    from_version = _validated_version(from_version, "来源")
    target_version = _validated_version(target_version, "目标")
    source_layout = read_file_layout(source_root, version=from_version)
    target_layout = read_file_layout(target_root, version=target_version)
    validate_file_package_root(source_root, source_layout)
    validate_file_package_root(target_root, target_layout)
    if source_layout["bootstrap_sha256"] != target_layout["bootstrap_sha256"]:
        raise UosFileUpdateError("UOS 引导文件发生变化，必须发布完整 DEB")
    changed_paths, deleted_paths = _diff_layouts(source_layout, target_layout)
    if not changed_paths and not deleted_paths:
        raise UosFileUpdateError("来源版本与目标版本的管理文件均未发生变化")
    try:
        target_layer_layout = read_layout(target_root, version=target_version)
    except UosLayerError as exc:
        raise UosFileUpdateError(f"无法读取目标兼容布局：{exc}") from exc
    manifest = {
        "schema_version": 2,
        "format": UOS_FILE_UPDATE_FORMAT,
        "platform": "linux-aarch64",
        "from_version": from_version,
        "target_version": target_version,
        "source_layout_sha256": source_layout["layout_sha256"],
        "target_layout": target_layout,
        "target_layer_layout": target_layer_layout,
        "changed_paths": changed_paths,
        "deleted_paths": deleted_paths,
        "created_at": _utc_now(),
    }
    target_entries = _entry_map(target_layout)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            partial,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for path_text in changed_paths:
                entry = target_entries[path_text]
                if entry["type"] != "file":
                    continue
                relative = _validated_managed_path(path_text, "UOS 逐文件包")
                source = target_root.joinpath(*relative.parts)
                _zip_file(
                    archive,
                    source,
                    f"payload/{relative.as_posix()}",
                    int(entry["mode"]),
                )
            archive.writestr(
                UOS_FILE_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return manifest


def validate_file_manifest(
    payload: object,
    *,
    from_version: str | None = None,
    target_version: str | None = None,
    source_layout_sha256: str | None = None,
    target_layout_sha256: str | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise UosFileUpdateError("UOS 逐文件包清单根节点必须是对象")
    if (
        payload.get("schema_version") != 2
        or payload.get("format") != UOS_FILE_UPDATE_FORMAT
    ):
        raise UosFileUpdateError("UOS 逐文件包格式不受支持")
    if payload.get("platform") != "linux-aarch64":
        raise UosFileUpdateError("UOS 逐文件包平台无效")
    actual_from = _validated_version(payload.get("from_version"), "来源")
    actual_target = _validated_version(payload.get("target_version"), "目标")
    if from_version is not None and actual_from != from_version:
        raise UosFileUpdateError("UOS 逐文件包来源版本不匹配")
    if target_version is not None and actual_target != target_version:
        raise UosFileUpdateError("UOS 逐文件包目标版本不匹配")
    source_digest = str(payload.get("source_layout_sha256") or "").casefold()
    if not _HASH_PATTERN.fullmatch(source_digest):
        raise UosFileUpdateError("UOS 逐文件包来源布局校验值无效")
    if source_layout_sha256 is not None and source_digest != source_layout_sha256:
        raise UosFileUpdateError("UOS 逐文件包来源布局不匹配")
    target_layout = validate_file_layout(
        payload.get("target_layout"),
        version=actual_target,
    )
    if (
        target_layout_sha256 is not None
        and target_layout["layout_sha256"] != target_layout_sha256
    ):
        raise UosFileUpdateError("UOS 逐文件包目标布局不匹配")
    try:
        target_layer_layout = validate_layout(
            payload.get("target_layer_layout"),
            version=actual_target,
        )
    except UosLayerError as exc:
        raise UosFileUpdateError(f"UOS 逐文件包兼容布局无效：{exc}") from exc
    if target_layer_layout["layout_sha256"] != target_layout["layer_layout_sha256"]:
        raise UosFileUpdateError("UOS 逐文件包的两个目标布局不一致")
    if target_layer_layout["bootstrap_sha256"] != target_layout["bootstrap_sha256"]:
        raise UosFileUpdateError("UOS 逐文件包的引导文件指纹不一致")
    for key, label in (
        ("changed_paths", "变更"),
        ("deleted_paths", "删除"),
    ):
        values = payload.get(key)
        if not isinstance(values, list):
            raise UosFileUpdateError(f"UOS 逐文件包{label}清单无效")
        normalized = [
            str(_validated_managed_path(item, f"UOS 逐文件包{label}"))
            for item in values
        ]
        if normalized != sorted(set(normalized)):
            raise UosFileUpdateError(f"UOS 逐文件包{label}清单无效")
    if not payload["changed_paths"] and not payload["deleted_paths"]:
        raise UosFileUpdateError("UOS 逐文件包没有任何变化")
    if set(payload["changed_paths"]) & set(payload["deleted_paths"]):
        raise UosFileUpdateError("UOS 逐文件包变更与删除清单冲突")
    target_paths = set(_entry_map(target_layout))
    if any(path not in target_paths for path in payload["changed_paths"]):
        raise UosFileUpdateError("UOS 逐文件包变更清单包含非目标路径")
    if any(path in target_paths for path in payload["deleted_paths"]):
        raise UosFileUpdateError("UOS 逐文件包删除清单仍存在于目标布局")
    return payload


def read_file_manifest(archive_path: str | Path, **expected) -> dict:
    path = Path(archive_path).expanduser().resolve()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise UosFileUpdateError("UOS 逐文件包文件数量超出限制")
            manifests = [info for info in infos if info.filename == UOS_FILE_MANIFEST]
            if len(manifests) != 1 or manifests[0].is_dir():
                raise UosFileUpdateError("UOS 逐文件包缺少唯一清单")
            if manifests[0].file_size > _MAX_MANIFEST_BYTES:
                raise UosFileUpdateError("UOS 逐文件包清单过大")
            payload = json.loads(archive.read(manifests[0]).decode("utf-8"))
    except UosFileUpdateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise UosFileUpdateError("无法读取 UOS 逐文件包") from exc
    return validate_file_manifest(payload, **expected)


class UosFileStore:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        current_version: str | None = None,
        package_root: str | Path | None = None,
        active_root: str | Path | None = None,
    ):
        self.data_dir = Path(data_dir or get_data_dir()).expanduser().resolve()
        self.current_version = _validated_version(
            current_version or APP_VERSION,
            "当前",
        )
        self._layer_store = UosLayerStore(
            self.data_dir,
            current_version=self.current_version,
            package_root=package_root,
            active_root=active_root,
        )
        self.package_root = self._layer_store.package_root
        self.active_root = self._layer_store.active_root
        self.root = self._layer_store.root
        self.versions_dir = self._layer_store.versions_dir
        self.downloads_dir = self._layer_store.downloads_dir
        self.pending_version_path = self._layer_store.pending_version_path
        self.pending_attempted_path = self._layer_store.pending_attempted_path
        self.pending_pid_path = self._layer_store.pending_pid_path
        self.current_version_path = self._layer_store.current_version_path

    def _ensure_directories(self) -> None:
        self._layer_store._ensure_directories()

    def archive_path(self, name: str, *, from_version: str, target_version: str) -> Path:
        self._ensure_directories()
        match = _ARCHIVE_NAME_PATTERN.fullmatch(str(name or "").strip())
        if (
            not match
            or Path(name).name != name
            or match.group("from") != from_version
            or match.group("target") != target_version
        ):
            raise UosFileUpdateError("UOS 逐文件包文件名无效")
        return self.downloads_dir / name

    def source_root(self, *, version: str, layout_sha256: str) -> Path | None:
        version = _validated_version(version, "来源")
        digest = str(layout_sha256 or "").casefold()
        if not _HASH_PATTERN.fullmatch(digest):
            return None
        candidates: list[Path] = []
        if self.active_root is not None:
            candidates.append(self.active_root)
        candidates.append(self.package_root)
        if self._layer_store._read_version(self.current_version_path) == version:
            candidates.append(self.versions_dir / version)
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                layout = read_file_layout(resolved, version=version)
                if layout["layout_sha256"] != digest:
                    continue
            except UosFileUpdateError:
                continue
            return resolved
        return None

    @staticmethod
    def _validated_members(
        archive: zipfile.ZipFile,
        manifest: dict,
    ) -> dict[str, zipfile.ZipInfo]:
        target_entries = _entry_map(manifest["target_layout"])
        expected_files = {
            f"payload/{path}"
            for path in manifest["changed_paths"]
            if target_entries[path]["type"] == "file"
        }
        members: dict[str, zipfile.ZipInfo] = {}
        expanded = 0
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or str(path) != name.rstrip("/")
                or name in members
            ):
                raise UosFileUpdateError("UOS 逐文件包包含不安全或重复路径")
            if name != UOS_FILE_MANIFEST:
                if name not in expected_files:
                    raise UosFileUpdateError("UOS 逐文件包包含未声明内容")
                unix_type = (info.external_attr >> 16) & 0o170000
                if unix_type not in {0, stat.S_IFREG} or info.is_dir():
                    raise UosFileUpdateError("UOS 逐文件包成员类型不受支持")
                target_path = name[len("payload/") :]
                if info.file_size != int(target_entries[target_path]["size"]):
                    raise UosFileUpdateError("UOS 逐文件包成员大小与目标布局不一致")
                expanded += info.file_size
                if expanded > _MAX_EXPANDED_BYTES:
                    raise UosFileUpdateError("UOS 逐文件包解压大小超出限制")
            members[name] = info
        if expected_files != (set(members) - {UOS_FILE_MANIFEST}):
            raise UosFileUpdateError("UOS 逐文件包缺少已声明的文件")
        return members

    def _reuse_regular_file(self, source: Path, target: Path) -> None:
        resolved = source.resolve(strict=True)
        try:
            os.link(resolved, target)
            return
        except OSError:
            pass
        try:
            resolved.relative_to(self.package_root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            shutil.copy2(resolved, target)
        else:
            # The installed DEB seed is immutable for the lifetime of an
            # in-app version. A later DEB upgrade supersedes older user roots,
            # so a read-only seed link is safe and avoids a second runtime copy.
            try:
                os.symlink(resolved, target)
            except (OSError, NotImplementedError):
                shutil.copy2(resolved, target)

    def stage(
        self,
        archive_path: str | Path,
        *,
        from_version: str,
        target_version: str,
        source_layout_sha256: str,
        target_layout_sha256: str,
        cancelled_callback=None,
        state_callback=None,
    ) -> Path:
        archive_path = Path(archive_path).expanduser().resolve()
        manifest = read_file_manifest(
            archive_path,
            from_version=from_version,
            target_version=target_version,
            source_layout_sha256=source_layout_sha256,
            target_layout_sha256=target_layout_sha256,
        )
        source_root = self.source_root(
            version=from_version,
            layout_sha256=source_layout_sha256,
        )
        if source_root is None:
            raise UosFileUpdateError("本机没有可用的 UOS 逐文件更新基线")
        source_layout = read_file_layout(source_root, version=from_version)
        validate_file_package_root(source_root, source_layout)
        target_layout = manifest["target_layout"]
        expected_changed, expected_deleted = _diff_layouts(
            source_layout,
            target_layout,
        )
        if manifest["changed_paths"] != expected_changed:
            raise UosFileUpdateError("UOS 逐文件包变更清单与来源基线不一致")
        if manifest["deleted_paths"] != expected_deleted:
            raise UosFileUpdateError("UOS 逐文件包删除清单与来源基线不一致")
        self._ensure_directories()
        changed_bytes = sum(
            int(entry.get("size") or 0)
            for entry in _entry_map(target_layout).values()
            if entry["path"] in set(expected_changed) and entry["type"] == "file"
        )
        if shutil.disk_usage(self.root).free < changed_bytes + _DISK_RESERVE_BYTES:
            raise UosFileUpdateError("磁盘可用空间不足，无法准备 UOS 逐文件更新")

        def ensure_not_cancelled() -> None:
            if cancelled_callback is not None and cancelled_callback():
                raise UosFileUpdateCancelled("UOS 逐文件更新已停止")

        final_root = self.versions_dir / target_version
        temporary = self.versions_dir / f".{target_version}.{os.getpid()}.tmp"
        if temporary.exists() or temporary.is_symlink():
            self._layer_store._remove_version_path(temporary)
        source_entries = _entry_map(source_layout)
        changed_set = set(expected_changed)
        try:
            ensure_not_cancelled()
            if state_callback is not None:
                state_callback("preparing_files", "正在逐文件组装 UOS 更新…")
            temporary.mkdir(parents=True, mode=0o700)
            with zipfile.ZipFile(archive_path) as archive:
                members = self._validated_members(archive, manifest)
                for index, entry in enumerate(target_layout["entries"]):
                    if index % 64 == 0:
                        ensure_not_cancelled()
                    relative = _validated_managed_path(entry["path"], "UOS 目标文件")
                    destination = temporary.joinpath(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if entry["type"] == "symlink":
                        os.symlink(entry["target"], destination)
                        continue
                    if entry["path"] in changed_set:
                        info = members[f"payload/{entry['path']}"]
                        with archive.open(info) as input_stream, destination.open(
                            "wb"
                        ) as output:
                            shutil.copyfileobj(input_stream, output, 1024 * 1024)
                        destination.chmod(int(entry["mode"]))
                        continue
                    if source_entries.get(entry["path"]) != entry:
                        raise UosFileUpdateError("UOS 来源文件描述与目标复用项不一致")
                    source = source_root.joinpath(*relative.parts)
                    self._reuse_regular_file(source, destination)
            ensure_not_cancelled()
            (temporary / UOS_LAYER_LAYOUT).write_text(
                json.dumps(
                    manifest["target_layer_layout"],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (temporary / UOS_FILE_LAYOUT).write_text(
                json.dumps(target_layout, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if state_callback is not None:
                state_callback("verifying_files", "正在校验 UOS 逐文件更新…")
            validate_file_package_root(temporary, target_layout)
            if final_root.exists() or final_root.is_symlink():
                try:
                    existing = read_file_layout(final_root, version=target_version)
                    validate_file_package_root(final_root, existing)
                except UosFileUpdateError:
                    self._layer_store._remove_version_path(final_root)
                else:
                    if existing["layout_sha256"] != target_layout_sha256:
                        raise UosFileUpdateError("本机已有不同内容的同版本 UOS 更新")
                    self._layer_store._remove_version_path(temporary)
            if temporary.exists():
                os.replace(temporary, final_root)
            self._layer_store._write_text_atomic(
                self.pending_version_path,
                target_version,
            )
            self.pending_attempted_path.unlink(missing_ok=True)
            self.pending_pid_path.unlink(missing_ok=True)
            return archive_path
        finally:
            if temporary.exists() or temporary.is_symlink():
                try:
                    self._layer_store._remove_version_path(temporary)
                except OSError:
                    pass


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IntDemo UOS 逐文件更新工具")
    commands = parser.add_subparsers(dest="command", required=True)
    layout = commands.add_parser("write-layout")
    layout.add_argument("--package-root", required=True)
    layout.add_argument("--version", required=True)
    build = commands.add_parser("build-archive")
    build.add_argument("--source-root", required=True)
    build.add_argument("--target-root", required=True)
    build.add_argument("--from-version", required=True)
    build.add_argument("--target-version", required=True)
    build.add_argument("--output", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--source-root", required=True)
    replay.add_argument("--archive", required=True)
    replay.add_argument("--from-version", required=True)
    replay.add_argument("--target-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "write-layout":
            print(write_file_layout(args.package_root, args.version))
        elif args.command == "build-archive":
            manifest = create_file_archive(
                args.source_root,
                args.target_root,
                args.output,
                from_version=args.from_version,
                target_version=args.target_version,
            )
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        elif args.command == "replay":
            manifest = read_file_manifest(
                args.archive,
                from_version=args.from_version,
                target_version=args.target_version,
            )
            with tempfile.TemporaryDirectory(prefix="intdemo-file-replay-") as data_dir:
                store = UosFileStore(
                    data_dir,
                    current_version=args.from_version,
                    package_root=args.source_root,
                    active_root=args.source_root,
                )
                store.stage(
                    args.archive,
                    from_version=args.from_version,
                    target_version=args.target_version,
                    source_layout_sha256=manifest["source_layout_sha256"],
                    target_layout_sha256=manifest["target_layout"]["layout_sha256"],
                )
                validate_file_package_root(
                    store.versions_dir / args.target_version,
                    manifest["target_layout"],
                )
            print("UOS 逐文件更新回放验证通过")
        else:  # pragma: no cover - argparse guards this branch
            parser.error(f"未知命令：{args.command}")
    except UosFileUpdateError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
