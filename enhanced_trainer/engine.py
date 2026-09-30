"""Shared CAPTCHA inference runtime for desktop clients and trainer self-tests."""

from __future__ import annotations

import base64
import io
import json
import math
import re
import sys
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .click_engine import (
    CLICK_FEATURE_COUNT,
    CLICK_FEATURE_SCHEMA,
    ClickFeatureExtractor,
    resolve_click_features,
)
from .model_bundle import (
    ENGINE_API_VERSION,
    ENGINE_RUNTIME_VERSION,
    MAX_MODEL_BUNDLE_BYTES,
    ModelBundleError,
    load_model_bundle,
)
from .protocol import COMPONENT_VERSION

MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){2,3}$")


class EngineError(RuntimeError):
    pass


def _version_key(value: str) -> tuple[int, int, int, int]:
    if not _VERSION_PATTERN.fullmatch(str(value or "")):
        raise EngineError("模型要求的推理运行时版本号无效")
    values = [int(item) for item in str(value).split(".")]
    return tuple((values + [0, 0, 0, 0])[:4])


def _strict_json(payload: bytes, *, label: str) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in result:
                raise ValueError("duplicate or invalid key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite number: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EngineError(f"{label}不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise EngineError(f"{label}必须是 JSON 对象")
    return value


def _decode(value, *, label: str, maximum: int) -> bytes:
    if not isinstance(value, str):
        raise EngineError(f"{label}数据无效")
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise EngineError(f"{label}不是有效 Base64") from exc
    if not 0 < len(payload) <= maximum:
        raise EngineError(f"{label}大小无效")
    return payload


def _finite_config_number(config: dict, name: str) -> float:
    value = config.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineError(f"点选模型配置 {name} 无效")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 100:
        raise EngineError(f"点选模型配置 {name} 无效")
    return result


@dataclass
class _LoadedModel:
    captcha_type: str
    version: str
    family: str
    config: dict
    session: object


class CaptchaEngine:
    def __init__(self):
        self.models: dict[str, _LoadedModel] = {}
        self._click_extractor = None

    @staticmethod
    def _session(model_bytes: bytes):
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            return ort.InferenceSession(
                model_bytes,
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise EngineError(f"无法加载 ONNX 模型：{exc}") from exc

    def load_model(
        self,
        artifact: bytes,
        *,
        expected_type: str | None = None,
        expected_version: str | None = None,
    ) -> dict:
        try:
            bundle = load_model_bundle(
                artifact,
                expected_captcha_type=expected_type,
                expected_version=expected_version,
            )
        except ModelBundleError as exc:
            raise EngineError(str(exc)) from exc
        if _version_key(bundle.min_component_version) > _version_key(
            ENGINE_RUNTIME_VERSION
        ):
            raise EngineError(
                f"模型需要客户端推理运行时 {bundle.min_component_version} 或更高版本"
            )
        if set(bundle.files) != {"model.onnx", "config.json"}:
            raise EngineError("当前组件不支持该模型包文件布局")
        config = _strict_json(bundle.files["config.json"], label="模型配置")
        common = {
            "schema_version",
            "engine_api_version",
            "captcha_type",
            "version",
            "model_family",
        }
        click_fields = {
            "feature_schema",
            "feature_count",
            "ranker_weight",
            "min_probability",
            "min_row_margin",
            "min_assignment_margin",
        }
        expected_fields = common | (click_fields if bundle.captcha_type == "click" else set())
        if set(config) != expected_fields:
            raise EngineError("模型配置字段无效")
        if (
            type(config.get("schema_version")) is not int
            or config.get("schema_version") != 1
            or type(config.get("engine_api_version")) is not int
            or config.get("engine_api_version") != ENGINE_API_VERSION
            or config.get("captcha_type") != bundle.captcha_type
            or config.get("version") != bundle.version
            or config.get("model_family") != bundle.model_family
        ):
            raise EngineError("模型配置与模型包清单不一致")
        session = self._session(bundle.files["model.onnx"])
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise EngineError("模型输入输出数量无效")
        if bundle.captcha_type == "numeric":
            if (
                bundle.model_family != "numeric-tiny-cnn-v1"
                or list(inputs[0].shape) != [1, 1, 48, 112]
                or list(outputs[0].shape) != [1, 4, 10]
            ):
                raise EngineError("数字模型运行契约无效")
        else:
            if (
                bundle.model_family != "click-prompt-ranker-v1"
                or config.get("feature_schema") != CLICK_FEATURE_SCHEMA
                or config.get("feature_count") != CLICK_FEATURE_COUNT
                or list(inputs[0].shape)[1:] != [CLICK_FEATURE_COUNT]
                or list(outputs[0].shape)[1:] != [1]
            ):
                raise EngineError("点选排序模型运行契约无效")
            for name in (
                "ranker_weight",
                "min_probability",
                "min_row_margin",
                "min_assignment_margin",
            ):
                value = _finite_config_number(config, name)
                if name != "min_assignment_margin" and value > 1:
                    raise EngineError(f"点选模型配置 {name} 无效")
        self.models[bundle.captcha_type] = _LoadedModel(
            bundle.captcha_type,
            bundle.version,
            bundle.model_family,
            config,
            session,
        )
        return {
            "captcha_type": bundle.captcha_type,
            "version": bundle.version,
            "model_family": bundle.model_family,
        }

    def unload_model(self, captcha_type: str) -> None:
        self.models.pop(str(captcha_type), None)

    def predict_numeric(self, image_bytes: bytes) -> dict:
        loaded = self.models.get("numeric")
        if loaded is None:
            raise EngineError("数字验证码模型尚未加载")
        try:
            with Image.open(io.BytesIO(image_bytes)) as opened:
                image = opened.convert("L").resize((112, 48), Image.Resampling.LANCZOS)
            tensor = np.asarray(image, dtype=np.float32)[None, None, :, :] / 255.0
            input_name = loaded.session.get_inputs()[0].name
            output_name = loaded.session.get_outputs()[0].name
            logits = np.asarray(
                loaded.session.run([output_name], {input_name: tensor})[0],
                dtype=np.float32,
            )
        except Exception as exc:
            raise EngineError(f"数字验证码推理失败：{exc}") from exc
        if logits.shape != (1, 4, 10) or not np.isfinite(logits).all():
            raise EngineError("数字验证码模型输出无效")
        shifted = logits[0] - np.max(logits[0], axis=1, keepdims=True)
        probabilities = np.exp(np.clip(shifted, -40, 40))
        probabilities /= np.sum(probabilities, axis=1, keepdims=True)
        indexes = np.argmax(logits[0], axis=1)
        confidence = float(
            min(
                probabilities[position, value]
                for position, value in enumerate(indexes)
            )
        )
        return {
            "status": "ok",
            "value": "".join(str(int(value)) for value in indexes),
            "confidence": round(confidence, 6),
            "version": loaded.version,
        }

    def predict_click(self, image_bytes: bytes, targets) -> dict:
        loaded = self.models.get("click")
        if loaded is None:
            raise EngineError("点选验证码模型尚未加载")
        target_list = [str(value or "").strip() for value in (targets or [])]
        if not 1 <= len(target_list) <= 8 or any(not value for value in target_list):
            raise EngineError("点选验证码目标文字无效")
        if self._click_extractor is None:
            self._click_extractor = ClickFeatureExtractor()
        prepared = self._click_extractor.extract(image_bytes, target_list)
        if not prepared.candidates:
            return {
                "status": "abstain",
                "points": [],
                "confidence": 0.0,
                "reason": "未检测到候选文字区域",
                "version": loaded.version,
            }
        flattened = prepared.features.reshape(-1, CLICK_FEATURE_COUNT)
        input_name = loaded.session.get_inputs()[0].name
        output_name = loaded.session.get_outputs()[0].name
        try:
            model_scores = np.asarray(
                loaded.session.run([output_name], {input_name: flattened})[0],
                dtype=np.float32,
            ).reshape(len(target_list), len(prepared.candidates))
        except Exception as exc:
            raise EngineError(f"点选验证码排序失败：{exc}") from exc
        ranker_weight = _finite_config_number(loaded.config, "ranker_weight")
        scores = (
            prepared.base_scores * (1.0 - ranker_weight)
            + model_scores * ranker_weight
        )
        resolution = resolve_click_features(
            prepared,
            scores,
            min_probability=_finite_config_number(loaded.config, "min_probability"),
            min_row_margin=_finite_config_number(loaded.config, "min_row_margin"),
            min_assignment_margin=_finite_config_number(
                loaded.config,
                "min_assignment_margin",
            ),
        )
        return {
            "status": resolution.status,
            "points": list(resolution.points),
            "confidence": round(resolution.confidence, 6),
            "reason": resolution.reason,
            "version": loaded.version,
        }


def _response(request_id, *, result=None, error: str = "") -> dict:
    if error:
        return {"request_id": request_id, "ok": False, "error": error[:1_000]}
    return {"request_id": request_id, "ok": True, "result": result}


def serve(input_stream=None, output_stream=None) -> int:
    """Serve bounded JSON-line requests over local stdin/stdout."""

    source = input_stream or sys.stdin.buffer
    destination = output_stream or sys.stdout.buffer
    engine = CaptchaEngine()
    while True:
        raw = source.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            return 2
        request_id = None
        should_stop = False
        try:
            request = _strict_json(raw, label="组件请求")
            if not isinstance(request, dict):
                raise EngineError("组件请求必须是 JSON 对象")
            candidate_request_id = request.get("request_id")
            if (
                not isinstance(candidate_request_id, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", candidate_request_id)
                is None
            ):
                raise EngineError("组件请求编号无效")
            request_id = candidate_request_id
            command = str(request.get("command") or "")
            if command == "hello":
                result = {
                    "engine_api_version": ENGINE_API_VERSION,
                    "component_version": COMPONENT_VERSION,
                    "capabilities": [
                        "train.numeric",
                        "infer.numeric",
                        "train.click",
                        "infer.click",
                    ],
                }
            elif command == "load_model":
                artifact = _decode(
                    request.get("artifact_base64"),
                    label="模型包",
                    maximum=MAX_MODEL_BUNDLE_BYTES,
                )
                result = engine.load_model(
                    artifact,
                    expected_type=str(request.get("captcha_type") or ""),
                    expected_version=str(request.get("version") or ""),
                )
            elif command == "unload_model":
                engine.unload_model(str(request.get("captcha_type") or ""))
                result = {"unloaded": True}
            elif command == "predict_numeric":
                result = engine.predict_numeric(
                    _decode(
                        request.get("image_base64"),
                        label="验证码图片",
                        maximum=MAX_IMAGE_BYTES,
                    )
                )
            elif command == "predict_click":
                result = engine.predict_click(
                    _decode(
                        request.get("image_base64"),
                        label="验证码图片",
                        maximum=MAX_IMAGE_BYTES,
                    ),
                    request.get("targets"),
                )
            elif command == "shutdown":
                result = {"stopped": True}
                should_stop = True
            else:
                raise EngineError("不支持的组件命令")
            payload = _response(request_id, result=result)
        except Exception as exc:  # noqa: BLE001 - process protocol boundary
            payload = _response(request_id, error=" ".join(str(exc).split()))
        destination.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        destination.flush()
        if should_stop:
            return 0


__all__ = ["CaptchaEngine", "EngineError", "serve"]
