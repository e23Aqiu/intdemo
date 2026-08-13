"""Run an installed enhanced trainer and validate its protocol-v1 outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .captcha_models import (
    MAX_DATASET_BYTES,
    MAX_MODEL_ARTIFACT_BYTES,
    CaptchaCandidate,
    CaptchaTrainingError,
    TinyCnnOnnxCaptchaModel,
)
from .platform_support import system_application_launch_context
from .trainer_component import TRAINER_PROTOCOL_VERSION, TrainerComponentManager

ENHANCED_TRAINING_TIMEOUT_SECONDS = 3_600
MAX_OUTPUT_JSON_BYTES = 256 * 1024
MAX_COUNT = 10_000_000
_ALGORITHM = "tiny-cnn-onnx-v1"
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_TRAINER_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_METADATA_FIELDS = {
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
    "sample_count",
    "test_count",
    "correct_count",
    "metrics_sha256",
}
_METRICS_REQUIRED_FIELDS = {
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
_METRICS_OPTIONAL_FIELDS = {
    "class_count",
    "crop_count",
    "per_position_accuracy",
    "right_edge_augmentation",
}


class EnhancedTrainingError(CaptchaTrainingError):
    """The local enhanced trainer or its output violated protocol v1."""


def _strict_json_file(path: Path, *, label: str) -> tuple[dict, bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_OUTPUT_JSON_BYTES:
            raise ValueError("size outside limit")
        raw = path.read_bytes()
        if len(raw) != size or len(raw) > MAX_OUTPUT_JSON_BYTES:
            raise ValueError("size changed while reading")

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if not isinstance(key, str) or key in result:
                    raise ValueError("duplicate or invalid key")
                result[key] = value
            return result

        def reject_constant(value):
            raise ValueError(f"non-finite number: {value}")

        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
        if not isinstance(payload, dict):
            raise TypeError("root is not an object")
        return payload, raw
    except (
        OSError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 无效：{exc}") from exc


def _bounded_integer(
    value,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = MAX_COUNT,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 无效")
    return value


def _finite_number(value, *, label: str, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 必须是有限数字")
    result = float(value)
    if not math.isfinite(result):
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 必须是有限数字")
    if minimum is not None and result < minimum:
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 超出范围")
    if maximum is not None and result > maximum:
        raise EnhancedTrainingError(f"强化训练器输出的 {label} 超出范围")
    return result


def _validate_metrics(payload: dict, *, captcha_type: str) -> dict:
    fields = set(payload)
    numeric_optional = {
        "per_position_accuracy",
        "right_edge_augmentation",
    }
    click_optional = {
        "class_count",
        "crop_count",
    }
    allowed_optional = numeric_optional if captcha_type == "numeric" else click_optional
    if not _METRICS_REQUIRED_FIELDS <= fields or fields - (
        _METRICS_REQUIRED_FIELDS | allowed_optional
    ):
        raise EnhancedTrainingError("强化训练器 metrics.json 字段不完整或包含未知字段")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise EnhancedTrainingError("强化训练器指标格式版本不受支持")
    if (
        type(payload.get("protocol_version")) is not int
        or payload["protocol_version"] != TRAINER_PROTOCOL_VERSION
    ):
        raise EnhancedTrainingError("强化训练器指标协议版本不一致")
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, str) or not evaluation.strip() or len(evaluation) > 120:
        raise EnhancedTrainingError("强化训练器评估方法无效")
    train_samples = _bounded_integer(payload.get("train_samples"), label="train_samples", minimum=1)
    test_samples = _bounded_integer(payload.get("test_samples"), label="test_samples", minimum=1)
    correct_samples = _bounded_integer(payload.get("correct_samples"), label="correct_samples")
    if correct_samples > test_samples:
        raise EnhancedTrainingError("强化训练器正确样本数超过测试样本数")
    accuracy = _finite_number(payload.get("accuracy"), label="accuracy", minimum=0, maximum=1)
    expected_accuracy = correct_samples / test_samples
    if not math.isclose(accuracy, expected_accuracy, rel_tol=1e-9, abs_tol=1e-9):
        raise EnhancedTrainingError("强化训练器准确率与计数不一致")
    _bounded_integer(payload.get("seed"), label="seed", maximum=2_147_483_647)
    _bounded_integer(payload.get("epochs"), label="epochs", minimum=1)
    _bounded_integer(payload.get("batch_size"), label="batch_size", minimum=1)

    for field in ("class_count", "crop_count"):
        if field in payload:
            _bounded_integer(payload[field], label=field, minimum=1)
    if "class_count" in payload and payload["class_count"] < 2:
        raise EnhancedTrainingError("class_count 至少为 2")
    if "crop_count" in payload and payload["crop_count"] < train_samples:
        raise EnhancedTrainingError("crop_count 小于训练样本数")
    if "per_position_accuracy" in payload:
        values = payload["per_position_accuracy"]
        if captcha_type != "numeric" or not isinstance(values, list) or len(values) != 4:
            raise EnhancedTrainingError("per_position_accuracy 仅允许四位数字模型使用")
        for value in values:
            _finite_number(value, label="per_position_accuracy", minimum=0, maximum=1)
    if "right_edge_augmentation" in payload:
        setting = payload["right_edge_augmentation"]
        if type(setting) is not bool:
            raise EnhancedTrainingError("right_edge_augmentation 必须是布尔值")
    return {
        **payload,
        "evaluation": evaluation.strip(),
        "training_mode": "enhanced",
    }


def _validate_outputs(
    output_dir: Path,
    captcha_type: str,
    component_manager: TrainerComponentManager,
) -> CaptchaCandidate:
    try:
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise ValueError("not a regular directory")
        if getattr(output_dir, "is_junction", lambda: False)():
            raise ValueError("output directory is a junction")
        entries = list(output_dir.iterdir())
    except (OSError, ValueError) as exc:
        raise EnhancedTrainingError(f"无法读取强化训练输出目录：{exc}") from exc
    expected_names = {"candidate.onnx", "metadata.json", "metrics.json"}
    if {entry.name for entry in entries} != expected_names:
        raise EnhancedTrainingError("强化训练输出目录缺少文件或包含未声明文件")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise EnhancedTrainingError("强化训练输出必须全部是普通文件")

    paths = component_manager.training_output_paths(output_dir)
    for key, expected_name in {
        "model": "candidate.onnx",
        "metadata": "metadata.json",
        "metrics": "metrics.json",
    }.items():
        path = paths.get(key)
        if (
            not isinstance(path, Path)
            or path.name != expected_name
            or path.parent.resolve() != output_dir.resolve()
        ):
            raise EnhancedTrainingError("强化训练输出路径违反协议")
    metadata, _ = _strict_json_file(paths["metadata"], label="metadata.json")
    metrics, metrics_raw = _strict_json_file(paths["metrics"], label="metrics.json")
    if set(metadata) != _METADATA_FIELDS:
        raise EnhancedTrainingError("强化训练器 metadata.json 字段不完整或包含未知字段")
    if type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1:
        raise EnhancedTrainingError("强化训练元数据格式版本不受支持")
    if (
        type(metadata.get("protocol_version")) is not int
        or metadata["protocol_version"] != TRAINER_PROTOCOL_VERSION
    ):
        raise EnhancedTrainingError("强化训练元数据协议版本不一致")
    if metadata.get("algorithm") != _ALGORITHM:
        raise EnhancedTrainingError("强化训练模型算法标识无效")
    if metadata.get("captcha_type") != captcha_type:
        raise EnhancedTrainingError("强化训练模型验证码类型不一致")
    if metadata.get("training_mode") != "enhanced":
        raise EnhancedTrainingError("强化训练模型的训练模式无效")
    if metadata.get("artifact_file") != "candidate.onnx":
        raise EnhancedTrainingError("强化训练模型文件名无效")
    if metadata.get("metrics_file") != "metrics.json":
        raise EnhancedTrainingError("强化训练指标文件名无效")
    version = metadata.get("version")
    trainer_version = metadata.get("trainer_version")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise EnhancedTrainingError("强化训练模型版本号无效")
    if (
        not isinstance(trainer_version, str)
        or not _TRAINER_VERSION_PATTERN.fullmatch(trainer_version)
    ):
        raise EnhancedTrainingError("强化训练器版本号无效")
    status = component_manager.status(verify_files=False)
    if not status.available or trainer_version != status.version:
        raise EnhancedTrainingError("训练结果的训练器版本与本机组件不一致")

    sample_count = _bounded_integer(metadata.get("sample_count"), label="sample_count", minimum=1)
    test_count = _bounded_integer(metadata.get("test_count"), label="test_count", minimum=1)
    correct_count = _bounded_integer(metadata.get("correct_count"), label="correct_count")
    if correct_count > test_count:
        raise EnhancedTrainingError("强化训练正确样本数超过测试样本数")
    validated_metrics = _validate_metrics(metrics, captcha_type=captcha_type)
    if (
        validated_metrics["test_samples"] != test_count
        or validated_metrics["correct_samples"] != correct_count
        or validated_metrics["train_samples"] + test_count != sample_count
    ):
        raise EnhancedTrainingError("强化训练元数据与指标计数矛盾")

    model_path = paths["model"]
    try:
        if model_path.is_symlink() or not model_path.is_file():
            raise ValueError("not a regular file")
        model_size = model_path.stat().st_size
        if model_size <= 0 or model_size > MAX_MODEL_ARTIFACT_BYTES:
            raise ValueError("size outside limit")
        artifact = model_path.read_bytes()
        if len(artifact) != model_size or len(artifact) > MAX_MODEL_ARTIFACT_BYTES:
            raise ValueError("size changed while reading")
    except (OSError, ValueError) as exc:
        raise EnhancedTrainingError(f"强化训练器输出的 candidate.onnx 无效：{exc}") from exc
    artifact_size = _bounded_integer(
        metadata.get("artifact_size"),
        label="artifact_size",
        minimum=1,
        maximum=MAX_MODEL_ARTIFACT_BYTES,
    )
    if artifact_size != model_size:
        raise EnhancedTrainingError("强化训练模型大小与元数据不一致")
    artifact_digest = hashlib.sha256(artifact).hexdigest()
    if (
        not isinstance(metadata.get("artifact_sha256"), str)
        or not _HASH_PATTERN.fullmatch(metadata["artifact_sha256"])
        or metadata["artifact_sha256"] != artifact_digest
    ):
        raise EnhancedTrainingError("强化训练模型 SHA-256 校验失败")
    metrics_digest = hashlib.sha256(metrics_raw).hexdigest()
    if (
        not isinstance(metadata.get("metrics_sha256"), str)
        or not _HASH_PATTERN.fullmatch(metadata["metrics_sha256"])
        or metadata["metrics_sha256"] != metrics_digest
    ):
        raise EnhancedTrainingError("强化训练指标 SHA-256 校验失败")

    # Parsing metadata is not enough: instantiate the production inference
    # adapter to enforce ONNX metadata, input/output shapes and supported ops.
    try:
        TinyCnnOnnxCaptchaModel.from_bytes(captcha_type, version, artifact)
    except CaptchaTrainingError as exc:
        raise EnhancedTrainingError(f"强化 ONNX 模型加载验证失败：{exc}") from exc
    validated_metrics["trainer_version"] = trainer_version
    return CaptchaCandidate(
        captcha_type=captcha_type,
        version=version,
        algorithm=_ALGORITHM,
        artifact=artifact,
        sample_count=sample_count,
        test_count=test_count,
        correct_count=correct_count,
        metrics=validated_metrics,
    )


def _stderr_detail(value) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    text = " ".join(text.replace("\x00", "").split())
    return text[:1_000]


def _stderr_file_detail(stream) -> str:
    try:
        stream.flush()
        stream.seek(0)
        return _stderr_detail(stream.read(4_096))
    except (OSError, ValueError):
        return ""


def train_enhanced_candidate(
    archive_bytes: bytes,
    captcha_type: str,
    component_manager: TrainerComponentManager,
    *,
    runner=None,
    timeout_seconds: float = ENHANCED_TRAINING_TIMEOUT_SECONDS,
) -> CaptchaCandidate:
    """Train through the installed component and return a verified candidate."""
    if captcha_type not in {"numeric", "click"}:
        raise EnhancedTrainingError("验证码类型必须是 numeric 或 click")
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        raise EnhancedTrainingError("强化训练数据集必须是字节数据")
    archive_bytes = bytes(archive_bytes)
    if not archive_bytes or len(archive_bytes) > MAX_DATASET_BYTES:
        raise EnhancedTrainingError("强化训练数据集为空或大小超过限制")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise EnhancedTrainingError("强化训练超时时间无效")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 86_400:
        raise EnhancedTrainingError("强化训练超时时间无效")
    process_runner = runner or subprocess.run

    try:
        with tempfile.TemporaryDirectory(prefix="intdemo-enhanced-training-") as temporary:
            workspace = Path(temporary).resolve()
            dataset_path = workspace / "dataset.zip"
            output_dir = workspace / "output"
            output_dir.mkdir()
            dataset_path.write_bytes(archive_bytes)
            with (workspace / "trainer.stderr").open("w+b") as stderr_stream:
                run_options = {
                    "cwd": str(workspace),
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": stderr_stream,
                    "text": False,
                    "timeout": timeout,
                    "check": False,
                    "shell": False,
                }
                if os.name == "nt":
                    run_options["creationflags"] = getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )
                with component_manager.training_lock():
                    command = component_manager.command_for(
                        dataset_path,
                        captcha_type,
                        output_dir,
                    )
                    if (
                        not isinstance(command, (list, tuple))
                        or not command
                        or len(command) > 64
                        or any(
                            not isinstance(argument, str)
                            or not argument
                            or "\x00" in argument
                            or len(argument) > 32_768
                            for argument in command
                        )
                    ):
                        raise EnhancedTrainingError("强化训练器启动命令无效")
                    command = list(command)
                    environment = component_manager.training_environment()
                    run_options["env"] = environment
                    try:
                        with system_application_launch_context():
                            result = process_runner(command, **run_options)
                    except subprocess.TimeoutExpired as exc:
                        detail = _stderr_file_detail(stderr_stream) or _stderr_detail(
                            exc.stderr
                        )
                        raise EnhancedTrainingError(
                            "强化训练超时" + (f"：{detail}" if detail else "")
                        ) from exc
                    if int(getattr(result, "returncode", -1)) != 0:
                        detail = _stderr_file_detail(stderr_stream) or _stderr_detail(
                            getattr(result, "stderr", b"")
                        )
                        raise EnhancedTrainingError(
                            "强化训练器执行失败" + (f"：{detail}" if detail else "")
                        )
                    # stdout is intentionally ignored; only authenticated filesystem
                    # outputs participate in candidate construction.
                    return _validate_outputs(output_dir, captcha_type, component_manager)
    except EnhancedTrainingError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise EnhancedTrainingError(f"无法启动强化训练器：{exc}") from exc


__all__ = [
    "ENHANCED_TRAINING_TIMEOUT_SECONDS",
    "EnhancedTrainingError",
    "train_enhanced_candidate",
]
