"""Stable protocol and artifact contracts shared by trainer and packager."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

PROTOCOL_VERSION = 1
OUTPUT_SCHEMA_VERSION = 1
COMPONENT_VERSION = "1.0.0"
ALGORITHM = "tiny-cnn-onnx-v1"
TRAINING_MODE = "enhanced"

MODEL_FILE_NAME = "candidate.onnx"
METADATA_FILE_NAME = "metadata.json"
METRICS_FILE_NAME = "metrics.json"

NUMERIC_INPUT_SHAPE = (1, 1, 48, 112)
NUMERIC_OUTPUT_SHAPE = (1, 4, 10)
CLICK_INPUT_SHAPE = ("N", 3, 64, 64)
MAX_MODEL_BYTES = 20 * 1024 * 1024

_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

OUTPUT_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "algorithm",
        "captcha_type",
        "version",
        "training_mode",
        "trainer_version",
        "artifact_file",
        "artifact_sha256",
        "artifact_size",
        "metrics_file",
        "metrics_sha256",
        "sample_count",
        "test_count",
        "correct_count",
    }
)
METRICS_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "evaluation",
        "train_samples",
        "test_samples",
        "correct_samples",
        "accuracy",
        "seed",
        "epochs",
        "batch_size",
    }
)
METRICS_OPTIONAL_FIELDS = frozenset(
    {
        "class_count",
        "crop_count",
        "per_position_accuracy",
        "right_edge_augmentation",
    }
)


class TrainerProtocolError(RuntimeError):
    """A protocol request or generated output is invalid."""


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(payload))


def model_metadata(
    *,
    captcha_type: str,
    version: str,
    labels: list[str] | None = None,
) -> dict[str, str]:
    """Return ONNX custom metadata consumed by TinyCnnOnnxCaptchaModel."""

    if captcha_type not in {"numeric", "click"}:
        raise TrainerProtocolError("验证码类型必须是 numeric 或 click")
    if not _VERSION_PATTERN.fullmatch(str(version or "")):
        raise TrainerProtocolError("模型版本号格式无效")
    metadata = {
        "algorithm": ALGORITHM,
        "captcha_type": captcha_type,
        "version": version,
        "training_mode": TRAINING_MODE,
        "trainer_version": COMPONENT_VERSION,
    }
    if captcha_type == "click":
        if (
            not isinstance(labels, list)
            or len(labels) < 2
            or len(labels) > 1024
            or len(set(labels)) != len(labels)
            or any(
                not isinstance(label, str) or not label or len(label) > 4
                for label in labels
            )
        ):
            raise TrainerProtocolError("点选模型标签列表无效")
        metadata["labels_json"] = json.dumps(
            labels,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return metadata


def output_metadata(
    *,
    captcha_type: str,
    version: str,
    artifact_sha256: str,
    artifact_size: int,
    metrics_sha256: str,
    sample_count: int,
    test_count: int,
    correct_count: int,
) -> dict[str, object]:
    if captcha_type not in {"numeric", "click"}:
        raise TrainerProtocolError("验证码类型无效")
    if not _VERSION_PATTERN.fullmatch(str(version or "")):
        raise TrainerProtocolError("模型版本号格式无效")
    if not _HASH_PATTERN.fullmatch(artifact_sha256):
        raise TrainerProtocolError("模型 SHA-256 无效")
    if not _HASH_PATTERN.fullmatch(metrics_sha256):
        raise TrainerProtocolError("指标 SHA-256 无效")
    if not 0 < artifact_size <= MAX_MODEL_BYTES:
        raise TrainerProtocolError("模型大小无效")
    if (
        sample_count < 1
        or test_count < 1
        or correct_count < 0
        or correct_count > test_count
    ):
        raise TrainerProtocolError("训练计数字段无效")
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "algorithm": ALGORITHM,
        "captcha_type": captcha_type,
        "version": version,
        "training_mode": TRAINING_MODE,
        "trainer_version": COMPONENT_VERSION,
        "artifact_file": MODEL_FILE_NAME,
        "artifact_sha256": artifact_sha256,
        "artifact_size": artifact_size,
        "metrics_file": METRICS_FILE_NAME,
        "metrics_sha256": metrics_sha256,
        "sample_count": sample_count,
        "test_count": test_count,
        "correct_count": correct_count,
    }


def validate_generated_outputs(output_dir: Path | str) -> dict[str, object]:
    """Strictly validate the trainer's three protocol-v1 output files."""

    root = Path(output_dir).resolve()
    model_path = root / MODEL_FILE_NAME
    metadata_path = root / METADATA_FILE_NAME
    metrics_path = root / METRICS_FILE_NAME
    try:
        model_size = model_path.stat().st_size
        metadata_bytes = metadata_path.read_bytes()
        metrics_bytes = metrics_path.read_bytes()
    except OSError as exc:
        raise TrainerProtocolError(f"强化训练输出文件不完整：{exc}") from exc
    if not 0 < model_size <= MAX_MODEL_BYTES:
        raise TrainerProtocolError("强化 ONNX 模型大小无效")
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        metrics = json.loads(metrics_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TrainerProtocolError("强化训练元数据不是有效 JSON") from exc
    if not isinstance(metadata, dict) or not isinstance(metrics, dict):
        raise TrainerProtocolError("强化训练元数据必须是 JSON 对象")
    if set(metadata) != OUTPUT_METADATA_FIELDS:
        raise TrainerProtocolError("强化训练 metadata.json 字段集合无效")
    if (
        metadata.get("schema_version") != OUTPUT_SCHEMA_VERSION
        or metadata.get("protocol_version") != PROTOCOL_VERSION
        or metadata.get("algorithm") != ALGORITHM
        or metadata.get("training_mode") != TRAINING_MODE
        or metadata.get("trainer_version") != COMPONENT_VERSION
        or metadata.get("artifact_file") != MODEL_FILE_NAME
        or metadata.get("metrics_file") != METRICS_FILE_NAME
        or metadata.get("captcha_type") not in {"numeric", "click"}
        or not _VERSION_PATTERN.fullmatch(str(metadata.get("version") or ""))
    ):
        raise TrainerProtocolError("强化训练 metadata.json 契约不兼容")
    if sha256_file(model_path) != metadata.get("artifact_sha256"):
        raise TrainerProtocolError("强化 ONNX 模型 SHA-256 不一致")
    if model_size != metadata.get("artifact_size"):
        raise TrainerProtocolError("强化 ONNX 模型大小不一致")
    if sha256_bytes(metrics_bytes) != metadata.get("metrics_sha256"):
        raise TrainerProtocolError("强化训练 metrics.json SHA-256 不一致")
    if (
        type(metadata.get("sample_count")) is not int
        or type(metadata.get("test_count")) is not int
        or type(metadata.get("correct_count")) is not int
        or metadata["sample_count"] < 1
        or metadata["test_count"] < 1
        or not 0 <= metadata["correct_count"] <= metadata["test_count"]
    ):
        raise TrainerProtocolError("强化训练计数字段无效")
    fields = set(metrics)
    if not METRICS_REQUIRED_FIELDS <= fields or fields - (
        METRICS_REQUIRED_FIELDS | METRICS_OPTIONAL_FIELDS
    ):
        raise TrainerProtocolError("强化训练 metrics.json 字段集合无效")
    if (
        metrics.get("schema_version") != OUTPUT_SCHEMA_VERSION
        or metrics.get("protocol_version") != PROTOCOL_VERSION
        or metrics.get("test_samples") != metadata["test_count"]
        or metrics.get("correct_samples") != metadata["correct_count"]
        or metrics.get("train_samples") + metadata["test_count"]
        != metadata["sample_count"]
    ):
        raise TrainerProtocolError("强化训练指标与元数据不一致")
    accuracy = metrics.get("accuracy")
    if (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, (int, float))
        or not math.isfinite(float(accuracy))
        or not 0 <= accuracy <= 1
    ):
        raise TrainerProtocolError("强化训练准确率无效")
    expected_accuracy = metadata["correct_count"] / metadata["test_count"]
    if abs(float(accuracy) - expected_accuracy) > 1e-9:
        raise TrainerProtocolError("强化训练准确率与计数不一致")
    for field in ("train_samples", "test_samples", "correct_samples", "seed"):
        value = metrics.get(field)
        if type(value) is not int or value < 0 or value > 10_000_000:
            raise TrainerProtocolError(f"强化训练指标 {field} 无效")
    for field in ("epochs", "batch_size"):
        value = metrics.get(field)
        if type(value) is not int or not 1 <= value <= 10_000_000:
            raise TrainerProtocolError(f"强化训练指标 {field} 无效")
    captcha_type = metadata["captcha_type"]
    if captcha_type == "numeric":
        if fields & {"class_count", "crop_count"}:
            raise TrainerProtocolError("数字模型包含点选专用训练指标")
        positions = metrics.get("per_position_accuracy")
        if positions is not None and (
            not isinstance(positions, list)
            or len(positions) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= value <= 1
                for value in positions
            )
        ):
            raise TrainerProtocolError("数字模型逐位置准确率无效")
        if "right_edge_augmentation" in metrics and not isinstance(
            metrics["right_edge_augmentation"],
            bool,
        ):
            raise TrainerProtocolError("数字模型右侧裁切增强字段无效")
    else:
        if fields & {"per_position_accuracy", "right_edge_augmentation"}:
            raise TrainerProtocolError("点选模型包含数字专用训练指标")
        for field in ("class_count", "crop_count"):
            if field in metrics and (
                type(metrics[field]) is not int or metrics[field] < 1
            ):
                raise TrainerProtocolError(f"点选训练指标 {field} 无效")
    return {"metadata": metadata, "metrics": metrics, "model_path": model_path}
