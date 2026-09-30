"""Portable model bundles shared by the trainer and the client runtime.

The server validates the stable envelope.  Training components create bundles,
while every supported desktop client contains the runtime that validates and
executes the model family inside the bundle.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

MODEL_BUNDLE_SCHEMA_VERSION = 1
ENGINE_API_VERSION = 1
ENGINE_ID = "intdemo-captcha-engine"
# This is intentionally independent from the trainer component version.  A
# trainer-only update must keep emitting models for this runtime version unless
# it introduces a genuinely new inference contract.
ENGINE_RUNTIME_VERSION = "2.0.0"
MAX_MODEL_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_MODEL_BUNDLE_FILES = 64
MAX_MODEL_BUNDLE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MODEL_BUNDLE_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MODEL_MANIFEST_BYTES = 64 * 1024

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$")
_FAMILY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class ModelBundleError(ValueError):
    """The stable model envelope is malformed or incompatible."""


@dataclass(frozen=True)
class ModelBundle:
    captcha_type: str
    version: str
    model_family: str
    min_component_version: str
    files: Mapping[str, bytes]
    manifest: Mapping[str, object]


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _safe_name(value: str) -> str:
    name = str(value or "").replace("\\", "/")
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "//" in name
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
        or name.endswith("/")
        or any(
            part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            for part in path.parts
        )
    ):
        raise ModelBundleError("模型包包含不安全路径")
    normalized = str(path)
    if normalized == "manifest.json":
        raise ModelBundleError("模型文件不能占用 manifest.json")
    return normalized


def _regular_member(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return not mode or stat.S_ISREG(mode)


def _strict_json(payload: bytes) -> object:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in result:
                raise ValueError("duplicate or invalid key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite number: {value}")

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def build_model_bundle(
    *,
    captcha_type: str,
    version: str,
    model_family: str,
    min_component_version: str,
    files: Mapping[str, bytes],
) -> bytes:
    """Create a deterministic, data-only engine model bundle."""

    captcha = str(captcha_type or "").strip().casefold()
    version = str(version or "").strip()
    family = str(model_family or "").strip().casefold()
    minimum = str(min_component_version or "").strip()
    if captcha not in {"numeric", "click"}:
        raise ModelBundleError("模型验证码类型无效")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ModelBundleError("模型版本号无效")
    if not _FAMILY_PATTERN.fullmatch(family):
        raise ModelBundleError("模型系列编号无效")
    if not _VERSION_PATTERN.fullmatch(minimum):
        raise ModelBundleError("最低推理运行时版本号无效")
    if not isinstance(files, Mapping) or not files or len(files) > MAX_MODEL_BUNDLE_FILES:
        raise ModelBundleError("模型包文件列表无效")

    normalized: dict[str, bytes] = {}
    descriptors = []
    for raw_name, raw_payload in sorted(files.items(), key=lambda item: str(item[0])):
        name = _safe_name(str(raw_name))
        payload = bytes(raw_payload)
        if not payload or len(payload) > MAX_MODEL_BUNDLE_MEMBER_BYTES:
            raise ModelBundleError(f"模型文件大小无效：{name}")
        identity = name.casefold()
        if identity in {item.casefold() for item in normalized}:
            raise ModelBundleError("模型包包含重复路径")
        normalized[name] = payload
        descriptors.append(
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    manifest = {
        "schema_version": MODEL_BUNDLE_SCHEMA_VERSION,
        "engine_id": ENGINE_ID,
        "engine_api_version": ENGINE_API_VERSION,
        "captcha_type": captcha,
        "version": version,
        "model_family": family,
        "min_component_version": minimum,
        "files": descriptors,
    }
    manifest_bytes = _canonical_json(manifest)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest_info = zipfile.ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(manifest_info, manifest_bytes)
        for name, payload in normalized.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)
    result = stream.getvalue()
    if not 0 < len(result) <= MAX_MODEL_BUNDLE_BYTES:
        raise ModelBundleError("模型包大小超出限制")
    return result


def load_model_bundle(
    payload: bytes,
    *,
    expected_captcha_type: str | None = None,
    expected_version: str | None = None,
) -> ModelBundle:
    """Validate and unpack a stable engine model bundle in memory."""

    data = bytes(payload or b"")
    if not 0 < len(data) <= MAX_MODEL_BUNDLE_BYTES:
        raise ModelBundleError("模型包大小无效")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ModelBundleError("模型包不是有效 ZIP 文件") from exc
    try:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_MODEL_BUNDLE_FILES + 1:
            raise ModelBundleError("模型包文件数量无效")
        members: dict[str, zipfile.ZipInfo] = {}
        expanded_size = 0
        for info in infos:
            if (
                info.is_dir()
                or not _regular_member(info)
                or info.flag_bits & 0x1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size < 0
                or info.file_size > MAX_MODEL_BUNDLE_MEMBER_BYTES
            ):
                raise ModelBundleError("模型包包含不支持的文件")
            name = str(PurePosixPath(info.filename.replace("\\", "/")))
            if name == "manifest.json":
                pass
            else:
                name = _safe_name(name)
            identity = name.casefold()
            if identity in members:
                raise ModelBundleError("模型包包含重复路径")
            members[identity] = info
            expanded_size += info.file_size
            if expanded_size > MAX_MODEL_BUNDLE_EXPANDED_BYTES:
                raise ModelBundleError("模型包解压后大小超出限制")
        manifest_info = members.get("manifest.json")
        if manifest_info is None or manifest_info.file_size > MAX_MODEL_MANIFEST_BYTES:
            raise ModelBundleError("模型包缺少有效清单")
        try:
            manifest = _strict_json(archive.read(manifest_info))
        except (
            UnicodeError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise ModelBundleError("模型包清单无效") from exc
        required = {
            "schema_version",
            "engine_id",
            "engine_api_version",
            "captcha_type",
            "version",
            "model_family",
            "min_component_version",
            "files",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise ModelBundleError("模型包清单字段无效")
        captcha = str(manifest.get("captcha_type") or "").strip().casefold()
        version = str(manifest.get("version") or "").strip()
        family = str(manifest.get("model_family") or "").strip().casefold()
        minimum = str(manifest.get("min_component_version") or "").strip()
        if (
            type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != MODEL_BUNDLE_SCHEMA_VERSION
            or manifest.get("engine_id") != ENGINE_ID
            or type(manifest.get("engine_api_version")) is not int
            or manifest.get("engine_api_version") != ENGINE_API_VERSION
            or captcha not in {"numeric", "click"}
            or not _VERSION_PATTERN.fullmatch(version)
            or not _FAMILY_PATTERN.fullmatch(family)
            or not _VERSION_PATTERN.fullmatch(minimum)
        ):
            raise ModelBundleError("模型包清单契约不兼容")
        if expected_captcha_type and captcha != str(expected_captcha_type).casefold():
            raise ModelBundleError("模型包验证码类型不一致")
        if expected_version and version != str(expected_version):
            raise ModelBundleError("模型包版本不一致")
        descriptors = manifest.get("files")
        if not isinstance(descriptors, list) or not descriptors:
            raise ModelBundleError("模型包文件清单无效")
        extracted: dict[str, bytes] = {}
        declared = set()
        for item in descriptors:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise ModelBundleError("模型包文件条目无效")
            name = _safe_name(str(item.get("path") or ""))
            identity = name.casefold()
            if identity in declared or identity not in members:
                raise ModelBundleError("模型包文件清单不一致")
            declared.add(identity)
            size = item.get("size")
            digest = str(item.get("sha256") or "").casefold()
            if type(size) is not int or not 0 < size <= MAX_MODEL_BUNDLE_MEMBER_BYTES:
                raise ModelBundleError("模型包文件大小声明无效")
            if not _HASH_PATTERN.fullmatch(digest):
                raise ModelBundleError("模型包文件哈希声明无效")
            content = archive.read(members[identity])
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise ModelBundleError("模型包文件完整性校验失败")
            extracted[name] = content
        if set(members) != {"manifest.json", *declared}:
            raise ModelBundleError("模型包包含未声明文件")
        if archive.testzip() is not None:
            raise ModelBundleError("模型包压缩数据损坏")
        return ModelBundle(captcha, version, family, minimum, extracted, manifest)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ModelBundleError):
            raise
        raise ModelBundleError("模型包读取失败") from exc
    finally:
        archive.close()


__all__ = [
    "ENGINE_API_VERSION",
    "ENGINE_ID",
    "ENGINE_RUNTIME_VERSION",
    "MAX_MODEL_BUNDLE_BYTES",
    "MAX_MODEL_BUNDLE_EXPANDED_BYTES",
    "MODEL_BUNDLE_SCHEMA_VERSION",
    "ModelBundle",
    "ModelBundleError",
    "build_model_bundle",
    "load_model_bundle",
]
