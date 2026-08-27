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

UOS_LAYER_FORMAT = "uos-layered-v1"
UOS_LAYER_LAYOUT_FORMAT = "uos-layer-layout-v1"
UOS_LAYER_MANIFEST = "manifest.json"
UOS_LAYER_LAYOUT = "layer-layout.json"

_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_NAME_PATTERN = re.compile(
    r"^IntDemo-UOS-arm64-Layers-(?P<from>\d+\.\d+\.\d+)-to-"
    r"(?P<target>\d+\.\d+\.\d+)\.intlayer$",
    re.IGNORECASE,
)
_LAYER_PATHS = {
    "app": PurePosixPath("app/intdemo-client"),
    "runtime": PurePosixPath("app/_internal"),
    "browser": PurePosixPath("browser"),
}
_BOOTSTRAP_FILES = (
    PurePosixPath("intdemo-client"),
    PurePosixPath("client-online.json"),
)
_BOOTSTRAP_CERTS = PurePosixPath("certs")
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_DISK_RESERVE_BYTES = 128 * 1024 * 1024


class UosLayerError(RuntimeError):
    pass


class UosLayerCancelled(UosLayerError):
    pass


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        raise UosLayerError(f"{label}版本无效")
    return version


def _validated_relative_path(value: object, label: str) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in text
        or str(path) != text
    ):
        raise UosLayerError(f"{label}路径不安全")
    return path


def _validated_link_target(value: str, parent: PurePosixPath) -> str:
    target = PurePosixPath(str(value or ""))
    if not str(value or "") or target.is_absolute() or "\\" in str(value):
        raise UosLayerError("分层包包含不安全的符号链接")
    resolved: list[str] = list(parent.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise UosLayerError("分层包符号链接超出所属层")
            resolved.pop()
        else:
            resolved.append(part)
    return str(value)


def _scan_tree(root: Path) -> tuple[list[dict], int]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise UosLayerError(f"分层目录不存在：{root}")
    entries: list[dict] = []
    total_size = 0
    for directory, dir_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in list(dir_names):
            candidate = directory_path / name
            if not candidate.is_symlink():
                continue
            dir_names.remove(name)
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            target = os.readlink(candidate)
            _validated_link_target(target, relative.parent)
            entries.append(
                {"path": str(relative), "type": "symlink", "target": target}
            )
        for name in file_names:
            candidate = directory_path / name
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            if candidate.is_symlink():
                target = os.readlink(candidate)
                _validated_link_target(target, relative.parent)
                entries.append(
                    {
                        "path": str(relative),
                        "type": "symlink",
                        "target": target,
                    }
                )
                continue
            if not candidate.is_file():
                raise UosLayerError(f"分层目录包含不支持的文件：{candidate}")
            size = candidate.stat().st_size
            total_size += size
            entries.append(
                {
                    "path": str(relative),
                    "type": "file",
                    "mode": stat.S_IMODE(candidate.stat().st_mode),
                    "size": size,
                    "sha256": file_sha256(candidate),
                }
            )
    entries.sort(key=lambda item: (str(item["path"]), str(item["type"])))
    return entries, total_size


def _layer_descriptor(package_root: Path, name: str) -> dict:
    relative = _LAYER_PATHS[name]
    candidate = package_root.joinpath(*relative.parts)
    if name == "app":
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UosLayerError("业务程序层不存在") from exc
        if not resolved.is_file():
            raise UosLayerError("业务程序层不是文件")
        file_stat = resolved.stat()
        return {
            "kind": "file",
            "path": str(relative),
            "size": file_stat.st_size,
            "mode": stat.S_IMODE(file_stat.st_mode),
            "sha256": file_sha256(resolved),
        }
    entries, total_size = _scan_tree(candidate)
    return {
        "kind": "tree",
        "path": str(relative),
        "size": total_size,
        "file_count": len(entries),
        "sha256": hashlib.sha256(_canonical_bytes(entries)).hexdigest(),
    }


def _bootstrap_digest(package_root: Path) -> str:
    """Hash files that a user-layer deliberately cannot replace.

    The launcher, online endpoint configuration and CA roots continue to run
    from the installed DEB.  A layered release is therefore safe only while
    these files are byte-for-byte compatible with its source baseline.
    """

    entries: list[dict] = []
    for relative in _BOOTSTRAP_FILES:
        candidate = package_root.joinpath(*relative.parts)
        try:
            file_stat = candidate.stat()
        except OSError as exc:
            raise UosLayerError(f"UOS 引导文件不存在：{relative}") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise UosLayerError(f"UOS 引导文件类型无效：{relative}")
        entries.append(
            {
                "path": str(relative),
                "type": "file",
                "mode": stat.S_IMODE(file_stat.st_mode),
                "size": file_stat.st_size,
                "sha256": file_sha256(candidate),
            }
        )
    cert_root = package_root.joinpath(*_BOOTSTRAP_CERTS.parts)
    cert_entries, _cert_size = _scan_tree(cert_root)
    for entry in cert_entries:
        entries.append(
            {
                **entry,
                "path": f"{_BOOTSTRAP_CERTS}/{entry['path']}",
            }
        )
    entries.sort(key=lambda item: (str(item["path"]), str(item["type"])))
    return hashlib.sha256(_canonical_bytes(entries)).hexdigest()


def build_layout(package_root: str | Path, version: str) -> dict:
    root = Path(package_root).expanduser().resolve()
    version = _validated_version(version, "目标")
    layout = {
        "schema_version": 1,
        "format": UOS_LAYER_LAYOUT_FORMAT,
        "platform": "linux-aarch64",
        "version": version,
        "bootstrap_sha256": _bootstrap_digest(root),
        "layers": {
            name: _layer_descriptor(root, name) for name in _LAYER_PATHS
        },
    }
    layout["layout_sha256"] = _layout_digest(layout)
    return layout


def validate_layout(payload: object, *, version: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise UosLayerError("UOS 分层布局根节点必须是对象")
    if payload.get("schema_version") != 1:
        raise UosLayerError("UOS 分层布局版本不受支持")
    if payload.get("format") != UOS_LAYER_LAYOUT_FORMAT:
        raise UosLayerError("UOS 分层布局格式不受支持")
    if payload.get("platform") != "linux-aarch64":
        raise UosLayerError("UOS 分层布局平台无效")
    actual_version = _validated_version(payload.get("version"), "布局")
    if version is not None and actual_version != version:
        raise UosLayerError("UOS 分层布局版本不匹配")
    bootstrap_digest = str(payload.get("bootstrap_sha256") or "").casefold()
    if not _HASH_PATTERN.fullmatch(bootstrap_digest):
        raise UosLayerError("UOS 分层布局的引导文件指纹无效")
    layers = payload.get("layers")
    if not isinstance(layers, dict) or set(layers) != set(_LAYER_PATHS):
        raise UosLayerError("UOS 分层布局缺少必要层")
    for name, expected_path in _LAYER_PATHS.items():
        descriptor = layers.get(name)
        if not isinstance(descriptor, dict):
            raise UosLayerError(f"UOS {name} 层描述无效")
        expected_kind = "file" if name == "app" else "tree"
        if descriptor.get("kind") != expected_kind:
            raise UosLayerError(f"UOS {name} 层类型无效")
        path = _validated_relative_path(descriptor.get("path"), f"UOS {name} 层")
        if path != expected_path:
            raise UosLayerError(f"UOS {name} 层路径无效")
        digest = str(descriptor.get("sha256") or "").casefold()
        if not _HASH_PATTERN.fullmatch(digest):
            raise UosLayerError(f"UOS {name} 层校验值无效")
        try:
            size = int(descriptor.get("size"))
        except (TypeError, ValueError) as exc:
            raise UosLayerError(f"UOS {name} 层大小无效") from exc
        if size < 0 or size > _MAX_EXPANDED_BYTES:
            raise UosLayerError(f"UOS {name} 层大小超出限制")
        if expected_kind == "tree":
            try:
                file_count = int(descriptor.get("file_count"))
            except (TypeError, ValueError) as exc:
                raise UosLayerError(f"UOS {name} 层文件数无效") from exc
            if file_count < 1 or file_count > _MAX_ARCHIVE_MEMBERS:
                raise UosLayerError(f"UOS {name} 层文件数超出限制")
        else:
            try:
                mode = int(descriptor.get("mode"))
            except (TypeError, ValueError) as exc:
                raise UosLayerError("UOS 业务程序权限无效") from exc
            if mode < 0 or mode > 0o7777:
                raise UosLayerError("UOS 业务程序权限无效")
    digest = str(payload.get("layout_sha256") or "").casefold()
    if not _HASH_PATTERN.fullmatch(digest) or digest != _layout_digest(payload):
        raise UosLayerError("UOS 分层布局自身校验失败")
    return payload


def read_layout(package_root: str | Path, *, version: str | None = None) -> dict:
    path = Path(package_root).expanduser().resolve() / UOS_LAYER_LAYOUT
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UosLayerError(f"无法读取 UOS 分层布局：{path}") from exc
    return validate_layout(payload, version=version)


def write_layout(package_root: str | Path, version: str) -> Path:
    root = Path(package_root).expanduser().resolve()
    payload = build_layout(root, version)
    target = root / UOS_LAYER_LAYOUT
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


def validate_package_root(package_root: str | Path, layout: dict) -> None:
    root = Path(package_root).expanduser().resolve()
    validated = validate_layout(layout)
    for name in _LAYER_PATHS:
        if _layer_descriptor(root, name) != validated["layers"][name]:
            raise UosLayerError(f"UOS {name} 层内容与布局不一致")


def validate_bootstrap(package_root: str | Path, layout: dict) -> None:
    root = Path(package_root).expanduser().resolve()
    validated = validate_layout(layout)
    if _bootstrap_digest(root) != validated["bootstrap_sha256"]:
        raise UosLayerError("UOS 引导文件与分层布局不一致")


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


def _tree_entries_for_archive(root: Path) -> tuple[list[dict], list[dict]]:
    entries, _size = _scan_tree(root)
    files = [entry for entry in entries if entry["type"] == "file"]
    links = [entry for entry in entries if entry["type"] == "symlink"]
    return files, links


def create_layer_archive(
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
    source_layout = read_layout(source_root, version=from_version)
    target_layout = read_layout(target_root, version=target_version)
    validate_package_root(source_root, source_layout)
    validate_package_root(target_root, target_layout)
    if source_layout["bootstrap_sha256"] != target_layout["bootstrap_sha256"]:
        raise UosLayerError("UOS 引导文件发生变化，必须发布完整 DEB")
    changed_layers = [
        name
        for name in _LAYER_PATHS
        if source_layout["layers"][name] != target_layout["layers"][name]
    ]
    if not changed_layers:
        raise UosLayerError("来源版本与目标版本的三个层均未发生变化")

    links: dict[str, list[dict]] = {}
    manifest = {
        "schema_version": 1,
        "format": UOS_LAYER_FORMAT,
        "platform": "linux-aarch64",
        "from_version": from_version,
        "target_version": target_version,
        "source_layout_sha256": source_layout["layout_sha256"],
        "target_layout": target_layout,
        "changed_layers": changed_layers,
        "links": links,
        "created_at": _utc_now(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for name in changed_layers:
                layer_path = target_root.joinpath(*_LAYER_PATHS[name].parts)
                if name == "app":
                    resolved = layer_path.resolve(strict=True)
                    descriptor = target_layout["layers"][name]
                    _zip_file(
                        archive,
                        resolved,
                        "payload/app/intdemo-client",
                        int(descriptor["mode"]),
                    )
                    continue
                files, layer_links = _tree_entries_for_archive(layer_path)
                links[name] = [
                    {"path": item["path"], "target": item["target"]}
                    for item in layer_links
                ]
                for entry in files:
                    relative = _validated_relative_path(
                        entry["path"], f"UOS {name} 层文件"
                    )
                    source = layer_path.joinpath(*relative.parts)
                    _zip_file(
                        archive,
                        source,
                        f"payload/{name}/{relative.as_posix()}",
                        int(entry["mode"]),
                    )
            archive.writestr(
                UOS_LAYER_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return manifest


def validate_layer_manifest(
    payload: object,
    *,
    from_version: str | None = None,
    target_version: str | None = None,
    source_layout_sha256: str | None = None,
    target_layout_sha256: str | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise UosLayerError("UOS 分层包清单根节点必须是对象")
    if payload.get("schema_version") != 1 or payload.get("format") != UOS_LAYER_FORMAT:
        raise UosLayerError("UOS 分层包格式不受支持")
    if payload.get("platform") != "linux-aarch64":
        raise UosLayerError("UOS 分层包平台无效")
    actual_from = _validated_version(payload.get("from_version"), "来源")
    actual_target = _validated_version(payload.get("target_version"), "目标")
    if from_version is not None and actual_from != from_version:
        raise UosLayerError("UOS 分层包来源版本不匹配")
    if target_version is not None and actual_target != target_version:
        raise UosLayerError("UOS 分层包目标版本不匹配")
    source_hash = str(payload.get("source_layout_sha256") or "").casefold()
    if not _HASH_PATTERN.fullmatch(source_hash):
        raise UosLayerError("UOS 分层包来源布局校验值无效")
    if source_layout_sha256 is not None and source_hash != source_layout_sha256:
        raise UosLayerError("UOS 分层包来源布局不匹配")
    target_layout = validate_layout(payload.get("target_layout"), version=actual_target)
    if (
        target_layout_sha256 is not None
        and target_layout["layout_sha256"] != target_layout_sha256
    ):
        raise UosLayerError("UOS 分层包目标布局不匹配")
    changed = payload.get("changed_layers")
    if (
        not isinstance(changed, list)
        or not changed
        or len(set(changed)) != len(changed)
        or any(name not in _LAYER_PATHS for name in changed)
    ):
        raise UosLayerError("UOS 分层包变更层列表无效")
    links = payload.get("links")
    if not isinstance(links, dict) or any(name not in changed for name in links):
        raise UosLayerError("UOS 分层包符号链接描述无效")
    for name, items in links.items():
        if not isinstance(items, list) or name == "app":
            raise UosLayerError("UOS 分层包符号链接描述无效")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise UosLayerError("UOS 分层包符号链接描述无效")
            relative = _validated_relative_path(
                item.get("path"), f"UOS {name} 层符号链接"
            )
            if str(relative) in seen:
                raise UosLayerError("UOS 分层包包含重复符号链接")
            seen.add(str(relative))
            _validated_link_target(str(item.get("target") or ""), relative.parent)
    return payload


def read_layer_manifest(
    archive_path: str | Path,
    **expected,
) -> dict:
    path = Path(archive_path).expanduser().resolve()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise UosLayerError("UOS 分层包文件数量超出限制")
            manifests = [info for info in infos if info.filename == UOS_LAYER_MANIFEST]
            if len(manifests) != 1 or manifests[0].is_dir():
                raise UosLayerError("UOS 分层包缺少唯一清单")
            if manifests[0].file_size > 2 * 1024 * 1024:
                raise UosLayerError("UOS 分层包清单过大")
            payload = json.loads(archive.read(manifests[0]).decode("utf-8"))
    except UosLayerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise UosLayerError("无法读取 UOS 分层包") from exc
    return validate_layer_manifest(payload, **expected)


class UosLayerStore:
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
            current_version or APP_VERSION, "当前"
        )
        configured_package = str(
            package_root or os.environ.get("INTDEMO_PACKAGE_ROOT", "")
        ).strip()
        if configured_package:
            self.package_root = Path(configured_package).expanduser().resolve()
        else:
            executable = Path(sys.executable).resolve()
            self.package_root = executable.parent.parent
        configured_active = str(
            active_root or os.environ.get("INTDEMO_LAYER_ROOT", "")
        ).strip()
        self.active_root = (
            Path(configured_active).expanduser().resolve()
            if configured_active
            else None
        )
        self.root = self.data_dir / "uos-layers"
        self.versions_dir = self.root / "versions"
        self.downloads_dir = self.root / "downloads"
        self.pending_version_path = self.root / "pending-version"
        self.pending_attempted_path = self.root / "pending-attempted"
        self.pending_pid_path = self.root / "pending-pid"
        self.current_version_path = self.root / "current-version"

    def _ensure_directories(self) -> None:
        for directory in (self.root, self.versions_dir, self.downloads_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    @staticmethod
    def _write_text_atomic(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(value + "\n", encoding="ascii")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_version(path: Path) -> str:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return ""
        return value if _VERSION_PATTERN.fullmatch(value) else ""

    def archive_path(self, name: str, *, from_version: str, target_version: str) -> Path:
        self._ensure_directories()
        match = _ARCHIVE_NAME_PATTERN.fullmatch(str(name or "").strip())
        if (
            not match
            or Path(name).name != name
            or match.group("from") != from_version
            or match.group("target") != target_version
        ):
            raise UosLayerError("UOS 分层包文件名无效")
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
        candidate_version = self._read_version(self.current_version_path)
        if candidate_version == version:
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
                layout = read_layout(resolved, version=version)
            except UosLayerError:
                continue
            if layout["layout_sha256"] == digest:
                return resolved
        return None

    @staticmethod
    def _validated_members(
        archive: zipfile.ZipFile, changed_layers: set[str]
    ) -> dict[str, zipfile.ZipInfo]:
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
            ):
                raise UosLayerError("UOS 分层包包含不安全路径")
            if name in members:
                raise UosLayerError("UOS 分层包包含重复路径")
            if name != UOS_LAYER_MANIFEST:
                if len(path.parts) < 3 or path.parts[0] != "payload":
                    raise UosLayerError("UOS 分层包包含未声明内容")
                if path.parts[1] not in changed_layers:
                    raise UosLayerError("UOS 分层包包含未声明层")
                unix_type = (info.external_attr >> 16) & 0o170000
                if unix_type not in {0, stat.S_IFREG} or info.is_dir():
                    raise UosLayerError("UOS 分层包成员类型不受支持")
                expanded += info.file_size
                if expanded > _MAX_EXPANDED_BYTES:
                    raise UosLayerError("UOS 分层包解压大小超出限制")
            members[name] = info
        return members

    @staticmethod
    def _reuse_layer(source: Path, target: Path) -> None:
        try:
            os.symlink(source, target, target_is_directory=source.is_dir())
            return
        except (OSError, NotImplementedError):
            # Tests and uncommon filesystems may not permit user symlinks.
            # Copying preserves correctness; supported UOS filesystems take
            # the symlink path and therefore retain the intended disk saving.
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)

    @staticmethod
    def _remove_version_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

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
        manifest = read_layer_manifest(
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
            raise UosLayerError("本机 UOS 分层更新基线不可用")
        source_layout = read_layout(source_root, version=from_version)
        validate_package_root(source_root, source_layout)
        target_layout = manifest["target_layout"]
        changed_layers = set(manifest["changed_layers"])
        self._ensure_directories()
        expanded_size = sum(
            int(target_layout["layers"][name]["size"])
            for name in changed_layers
        )
        if shutil.disk_usage(self.root).free < expanded_size + _DISK_RESERVE_BYTES:
            raise UosLayerError("磁盘可用空间不足，无法准备 UOS 分层更新")

        def ensure_not_cancelled() -> None:
            if cancelled_callback is not None and cancelled_callback():
                raise UosLayerCancelled("UOS 分层更新已停止")

        final_root = self.versions_dir / target_version
        temporary = self.versions_dir / f".{target_version}.{os.getpid()}.tmp"
        if temporary.exists() or temporary.is_symlink():
            self._remove_version_path(temporary)
        try:
            ensure_not_cancelled()
            if state_callback is not None:
                state_callback("preparing_layers", "正在组装 UOS 分层更新…")
            temporary.mkdir(parents=True, mode=0o700)
            with zipfile.ZipFile(archive_path) as archive:
                members = self._validated_members(archive, changed_layers)
                for name, relative in _LAYER_PATHS.items():
                    target = temporary.joinpath(*relative.parts)
                    source = source_root.joinpath(*relative.parts).resolve(strict=True)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if name not in changed_layers:
                        self._reuse_layer(source, target)
                        continue
                    if name == "app":
                        info = members.get("payload/app/intdemo-client")
                        if info is None:
                            raise UosLayerError("UOS 分层包缺少业务程序层")
                        with archive.open(info) as input_stream, target.open("wb") as output:
                            shutil.copyfileobj(input_stream, output, 1024 * 1024)
                        target.chmod(int(target_layout["layers"][name]["mode"]))
                        continue
                    target.mkdir(parents=True, exist_ok=True)
                    prefix = f"payload/{name}/"
                    for member_name, info in members.items():
                        if not member_name.startswith(prefix):
                            continue
                        member_relative = _validated_relative_path(
                            member_name[len(prefix) :], f"UOS {name} 层文件"
                        )
                        destination = target.joinpath(*member_relative.parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as input_stream, destination.open("wb") as output:
                            shutil.copyfileobj(input_stream, output, 1024 * 1024)
                        mode = (info.external_attr >> 16) & 0o7777
                        destination.chmod(mode or 0o644)
                    for link in manifest.get("links", {}).get(name, []):
                        relative_link = _validated_relative_path(
                            link["path"], f"UOS {name} 层符号链接"
                        )
                        destination = target.joinpath(*relative_link.parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(link["target"], destination)
            ensure_not_cancelled()
            layout_path = temporary / UOS_LAYER_LAYOUT
            layout_path.write_text(
                json.dumps(target_layout, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if state_callback is not None:
                state_callback("verifying_layers", "正在校验 UOS 分层更新…")
            validate_package_root(temporary, target_layout)
            if final_root.exists():
                try:
                    existing = read_layout(final_root, version=target_version)
                    validate_package_root(final_root, existing)
                except UosLayerError:
                    self._remove_version_path(final_root)
                else:
                    if existing["layout_sha256"] != target_layout_sha256:
                        raise UosLayerError("本机已有不同内容的同版本分层更新")
                    self._remove_version_path(temporary)
            if temporary.exists():
                os.replace(temporary, final_root)
            self._write_text_atomic(self.pending_version_path, target_version)
            self.pending_attempted_path.unlink(missing_ok=True)
            self.pending_pid_path.unlink(missing_ok=True)
            return archive_path
        finally:
            if temporary.exists() or temporary.is_symlink():
                try:
                    self._remove_version_path(temporary)
                except OSError:
                    pass

    def confirm_pending(self, version: str | None = None) -> bool:
        version = _validated_version(version or self.current_version, "当前")
        if self._read_version(self.pending_version_path) != version:
            return False
        if self._read_version(self.pending_attempted_path) != version:
            return False
        target_root = self.versions_dir / version
        layout = read_layout(target_root, version=version)
        file_layout_path = target_root / "file-layout.json"
        if file_layout_path.is_file():
            from .uos_file_update import (
                UosFileUpdateError,
                read_file_layout,
                validate_file_package_root,
            )

            try:
                file_layout = read_file_layout(target_root, version=version)
                validate_file_package_root(target_root, file_layout)
            except UosFileUpdateError as exc:
                raise UosLayerError(f"UOS 逐文件版本校验失败：{exc}") from exc
        else:
            validate_package_root(target_root, layout)
        self._write_text_atomic(self.current_version_path, version)
        self.pending_version_path.unlink(missing_ok=True)
        self.pending_attempted_path.unlink(missing_ok=True)
        self.pending_pid_path.unlink(missing_ok=True)
        retained = {version}
        try:
            for candidate in self.versions_dir.iterdir():
                if candidate.name in retained:
                    continue
                if _VERSION_PATTERN.fullmatch(candidate.name):
                    self._remove_version_path(candidate)
            for candidate in self.downloads_dir.iterdir():
                if candidate.is_file() and candidate.name.casefold().endswith(
                    (".intlayer", ".intlayer.part")
                ):
                    candidate.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best effort and must not roll back an already
            # validated, successfully started version.
            pass
        return True


def confirm_running_layer(version: str | None = None) -> bool:
    pending = str(os.environ.get("INTDEMO_LAYER_PENDING_VERSION", "")).strip()
    expected = str(version or APP_VERSION).strip()
    if not pending or pending != expected:
        return False
    try:
        return UosLayerStore(current_version=expected).confirm_pending(expected)
    except (OSError, UosLayerError):
        return False


def _build_cli_parser():
    parser = argparse.ArgumentParser(description="IntDemo UOS 分层更新工具")
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
            path = write_layout(args.package_root, args.version)
            print(path)
        elif args.command == "build-archive":
            manifest = create_layer_archive(
                args.source_root,
                args.target_root,
                args.output,
                from_version=args.from_version,
                target_version=args.target_version,
            )
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        elif args.command == "replay":
            manifest = read_layer_manifest(
                args.archive,
                from_version=args.from_version,
                target_version=args.target_version,
            )
            with tempfile.TemporaryDirectory(prefix="intdemo-layer-replay-") as data_dir:
                store = UosLayerStore(
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
                replay_root = store.versions_dir / args.target_version
                validate_package_root(replay_root, manifest["target_layout"])
            print("UOS 分层更新回放验证通过")
        else:  # pragma: no cover - argparse guards this branch
            parser.error(f"未知命令：{args.command}")
    except UosLayerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
