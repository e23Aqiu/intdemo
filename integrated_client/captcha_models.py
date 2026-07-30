from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import cv2
import numpy as np
from PIL import Image, ImageOps

BUILTIN_MODEL_VERSION = "ddddocr-builtin"
DATASET_SCHEMA_VERSION = 1
MAX_DATASET_BYTES = 100 * 1024 * 1024
MAX_DATASET_FILES = 50_001
MAX_SAMPLE_IMAGE_BYTES = 1 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_MODEL_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_MODEL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MIN_NUMERIC_SAMPLES = 20
MIN_CLICK_SAMPLES = 30
MAX_TRAINING_SAMPLES = 5_000


class CaptchaTrainingError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptchaCandidate:
    captcha_type: str
    version: str
    algorithm: str
    artifact: bytes
    sample_count: int
    test_count: int
    correct_count: int
    metrics: dict

    @property
    def accuracy(self) -> float:
        return self.correct_count / self.test_count if self.test_count else 0.0


def _open_captcha_image(image_bytes: bytes, mode: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise CaptchaTrainingError("验证码图片像素数量超过限制")
            return source.convert(mode)
    except CaptchaTrainingError:
        raise
    except Exception as exc:
        raise CaptchaTrainingError(f"无法读取验证码图片：{exc}") from exc


def _feature_from_image(image: Image.Image, width=24, height=32) -> np.ndarray:
    grayscale = ImageOps.autocontrast(image.convert("L"))
    array = np.asarray(grayscale, dtype=np.uint8)
    _, binary = cv2.threshold(
        array,
        0,
        255,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )
    if float(binary.mean()) > 127:
        binary = 255 - binary
    resized = cv2.resize(binary, (width, height), interpolation=cv2.INTER_AREA)
    return resized.astype(np.float32).reshape(-1) / 255.0


def _numeric_features(image_bytes: bytes) -> list[np.ndarray]:
    image = _open_captcha_image(image_bytes, "L")
    image = ImageOps.autocontrast(image)
    image = image.resize((96, 32), Image.Resampling.LANCZOS)
    return [
        _feature_from_image(image.crop((index * 24, 0, (index + 1) * 24, 32)))
        for index in range(4)
    ]


def _click_feature(image_bytes: bytes, point: dict) -> np.ndarray:
    image = _open_captcha_image(image_bytes, "RGB")
    width, height = image.size
    center_x = float(point["x"]) * width
    center_y = float(point["y"]) * height
    radius = max(12, int(min(width, height) * 0.11))
    crop = image.crop(
        (
            max(0, int(center_x - radius)),
            max(0, int(center_y - radius)),
            min(width, int(center_x + radius)),
            min(height, int(center_y + radius)),
        )
    )
    return _feature_from_image(crop, width=32, height=32)


def _bbox_feature(image: Image.Image, bbox) -> np.ndarray:
    x1, y1, x2, y2 = (int(value) for value in bbox)
    padding = 3
    crop = image.crop(
        (
            max(0, x1 - padding),
            max(0, y1 - padding),
            min(image.width, x2 + padding),
            min(image.height, y2 + padding),
        )
    )
    return _feature_from_image(crop, width=32, height=32)


def _safe_dataset_archive(archive_bytes: bytes) -> list[dict]:
    if not archive_bytes or len(archive_bytes) > MAX_DATASET_BYTES:
        raise CaptchaTrainingError("数据集压缩包为空或大小超出限制")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise CaptchaTrainingError("数据集压缩包无效") from exc
    try:
        infos = archive.infolist()
        if len(infos) > MAX_DATASET_FILES:
            raise CaptchaTrainingError("数据集文件数量超出限制")
        total_size = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or info.flag_bits & 0x1
                or info.compress_type
                not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise CaptchaTrainingError("数据集包含不安全路径")
            total_size += int(info.file_size)
            if total_size > MAX_DATASET_BYTES:
                raise CaptchaTrainingError("数据集解压后大小超出限制")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise CaptchaTrainingError("数据集清单无效") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != DATASET_SCHEMA_VERSION
            or not isinstance(manifest.get("samples"), list)
            or len(manifest.get("samples")) > MAX_DATASET_FILES - 1
        ):
            raise CaptchaTrainingError("数据集版本或样本列表无效")
        samples = []
        image_cache = {}
        for item in manifest["samples"]:
            if not isinstance(item, dict):
                continue
            captcha_type = str(item.get("captcha_type") or "")
            image_name = str(item.get("image") or "")
            answer = item.get("answer")
            if captcha_type not in {"numeric", "click"} or not isinstance(answer, dict):
                continue
            try:
                image_info = archive.getinfo(image_name)
                if (
                    image_info.file_size <= 0
                    or image_info.file_size > MAX_SAMPLE_IMAGE_BYTES
                ):
                    continue
                if image_name not in image_cache:
                    image_cache[image_name] = archive.read(image_info)
                image = image_cache[image_name]
            except (KeyError, OSError):
                continue
            fingerprint = str(item.get("fingerprint") or "")
            if not fingerprint:
                fingerprint = hashlib.sha256(image).hexdigest()
            samples.append(
                {
                    "captcha_type": captcha_type,
                    "image": image,
                    "answer": answer,
                    "fingerprint": fingerprint,
                }
            )
        return samples
    finally:
        archive.close()


def _split_samples(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(samples, key=lambda item: item["fingerprint"])
    test = [item for index, item in enumerate(ordered) if index % 5 == 0]
    train = [item for index, item in enumerate(ordered) if index % 5 != 0]
    if not test or not train:
        raise CaptchaTrainingError("数据集无法划分训练集和测试集")
    return train, test


def _nearest_label(features, labels, feature, k=5) -> str:
    distances = np.sum((features - feature) ** 2, axis=1)
    count = min(max(1, int(k)), len(distances))
    nearest = np.argpartition(distances, count - 1)[:count]
    scores = {}
    for index in nearest:
        label = str(labels[index])
        score = 1.0 / (float(distances[index]) + 1e-6)
        scores[label] = scores.get(label, 0.0) + score
    return max(scores, key=scores.get)


class KnnCaptchaModel:
    ALGORITHM = "knn-pixels-v1"

    def __init__(
        self,
        captcha_type: str,
        version: str,
        features: np.ndarray,
        labels: np.ndarray,
        *,
        k: int = 5,
    ):
        if captcha_type not in {"numeric", "click"}:
            raise CaptchaTrainingError("模型验证码类型无效")
        features = np.asarray(features, dtype=np.float32)
        labels = np.asarray(labels).astype(str)
        expected_width = 24 * 32 if captcha_type == "numeric" else 32 * 32
        maximum_rows = 20_000 if captcha_type == "numeric" else 40_000
        if (
            features.ndim != 2
            or labels.ndim != 1
            or features.shape[1] != expected_width
            or len(features) != len(labels)
            or not len(labels)
            or len(labels) > maximum_rows
            or not np.isfinite(features).all()
        ):
            raise CaptchaTrainingError("模型特征或标签无效")
        if any(not label or len(label) > 4 for label in labels):
            raise CaptchaTrainingError("模型标签无效")
        if captcha_type == "numeric" and any(
            len(label) != 1 or label not in "0123456789"
            for label in labels
        ):
            raise CaptchaTrainingError("数字模型标签无效")
        self.captcha_type = captcha_type
        self.version = str(version)
        self.features = features
        self.labels = labels
        self.k = min(25, max(1, int(k)))

    def predict_numeric(self, image_bytes: bytes) -> str:
        if self.captcha_type != "numeric":
            return ""
        return "".join(
            _nearest_label(self.features, self.labels, feature, self.k)
            for feature in _numeric_features(image_bytes)
        )

    def predict_click_regions(
        self,
        image_bytes: bytes,
        bboxes,
    ) -> dict[str, tuple[int, int]]:
        if self.captcha_type != "click":
            return {}
        try:
            image = _open_captcha_image(image_bytes, "RGB")
        except CaptchaTrainingError:
            return {}
        positions = {}
        for bbox in bboxes:
            try:
                label = _nearest_label(
                    self.features,
                    self.labels,
                    _bbox_feature(image, bbox),
                    self.k,
                )
                x1, y1, x2, y2 = (int(value) for value in bbox)
                positions.setdefault(label, ((x1 + x2) // 2, (y1 + y2) // 2))
            except (CaptchaTrainingError, TypeError, ValueError, OverflowError):
                continue
        return positions

    def to_bytes(self) -> bytes:
        output = io.BytesIO()
        np.savez_compressed(
            output,
            schema_version=np.asarray([1], dtype=np.int16),
            captcha_type=np.asarray([self.captcha_type]),
            version=np.asarray([self.version]),
            algorithm=np.asarray([self.ALGORITHM]),
            k=np.asarray([self.k], dtype=np.int16),
            features=self.features.astype(np.float32),
            labels=self.labels.astype(str),
        )
        return output.getvalue()

    @classmethod
    def from_bytes(cls, artifact: bytes) -> KnnCaptchaModel:
        if not artifact or len(artifact) > MAX_MODEL_ARTIFACT_BYTES:
            raise CaptchaTrainingError("候选模型为空或大小超过限制")
        try:
            with zipfile.ZipFile(io.BytesIO(artifact), "r") as archive:
                infos = archive.infolist()
                if len(infos) > 16:
                    raise ValueError("too many model members")
                total_size = 0
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or info.flag_bits & 0x1
                        or info.compress_type
                        not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    ):
                        raise ValueError("unsafe model path")
                    total_size += int(info.file_size)
                    if total_size > MAX_MODEL_UNCOMPRESSED_BYTES:
                        raise ValueError("model expands beyond limit")
            with np.load(io.BytesIO(artifact), allow_pickle=False) as data:
                required = {
                    "schema_version",
                    "captcha_type",
                    "version",
                    "algorithm",
                    "k",
                    "features",
                    "labels",
                }
                if set(data.files) != required:
                    raise ValueError("unexpected model members")
                if int(data["schema_version"][0]) != 1:
                    raise ValueError("unsupported schema")
                if str(data["algorithm"][0]) != cls.ALGORITHM:
                    raise ValueError("unsupported algorithm")
                return cls(
                    str(data["captcha_type"][0]),
                    str(data["version"][0]),
                    data["features"],
                    data["labels"],
                    k=int(data["k"][0]),
                )
        except Exception as exc:
            raise CaptchaTrainingError(f"无法读取候选模型：{exc}") from exc


class CaptchaModelManager:
    def __init__(self):
        self._models = {}
        self._lock = threading.RLock()

    def install(self, captcha_type: str, version: str, artifact: bytes) -> None:
        model = KnnCaptchaModel.from_bytes(artifact)
        if model.captcha_type != captcha_type or model.version != version:
            raise CaptchaTrainingError("模型类型或版本与清单不一致")
        with self._lock:
            self._models[captcha_type] = model

    def clear(self, captcha_type: str) -> None:
        with self._lock:
            self._models.pop(captcha_type, None)

    def get(self, captcha_type: str) -> KnnCaptchaModel | None:
        with self._lock:
            return self._models.get(captcha_type)

    def version(self, captcha_type: str) -> str:
        model = self.get(captcha_type)
        return model.version if model is not None else BUILTIN_MODEL_VERSION


def _candidate_version(captcha_type: str, samples: list[dict]) -> str:
    digest = hashlib.sha256()
    for item in sorted(samples, key=lambda sample: sample["fingerprint"]):
        digest.update(item["fingerprint"].encode("ascii", errors="ignore"))
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    return f"{captcha_type}-knn-{stamp}-{digest.hexdigest()[:8]}"


def _train_numeric(samples: list[dict]) -> CaptchaCandidate:
    if len(samples) < MIN_NUMERIC_SAMPLES:
        raise CaptchaTrainingError(
            f"数字验证码至少需要 {MIN_NUMERIC_SAMPLES} 个成功样本"
        )
    train, test = _split_samples(samples)
    features = []
    labels = []
    valid_train = 0
    for item in train:
        value = str(item["answer"].get("value") or "")
        if len(value) != 4 or any(character not in "0123456789" for character in value):
            continue
        try:
            parts = _numeric_features(item["image"])
        except CaptchaTrainingError:
            continue
        features.extend(parts)
        labels.extend(value)
        valid_train += 1
    if valid_train < 10 or len(set(labels)) < 2:
        raise CaptchaTrainingError("数字验证码有效训练样本或字符种类不足")
    version = _candidate_version("numeric", samples)
    model = KnnCaptchaModel(
        "numeric",
        version,
        np.stack(features),
        np.asarray(labels),
    )
    correct = 0
    evaluated = 0
    for item in test:
        expected = str(item["answer"].get("value") or "")
        if len(expected) != 4 or any(
            character not in "0123456789" for character in expected
        ):
            continue
        try:
            predicted = model.predict_numeric(item["image"])
        except CaptchaTrainingError:
            continue
        evaluated += 1
        correct += int(predicted == expected)
    if not evaluated:
        raise CaptchaTrainingError("数字验证码测试集没有可评估样本")
    return CaptchaCandidate(
        captcha_type="numeric",
        version=version,
        algorithm=model.ALGORITHM,
        artifact=model.to_bytes(),
        sample_count=len(samples),
        test_count=evaluated,
        correct_count=correct,
        metrics={
            "evaluation": "held_out_exact_code_accuracy",
            "train_samples": valid_train,
            "test_samples": evaluated,
        },
    )


def _train_click(samples: list[dict]) -> CaptchaCandidate:
    if len(samples) < MIN_CLICK_SAMPLES:
        raise CaptchaTrainingError(
            f"文字点选验证码至少需要 {MIN_CLICK_SAMPLES} 个成功样本"
        )
    train, test = _split_samples(samples)
    features = []
    labels = []
    valid_train = 0
    for item in train:
        prompt = item["answer"].get("prompt")
        points = item["answer"].get("points")
        if not isinstance(prompt, list) or not isinstance(points, list):
            continue
        if not prompt or len(prompt) != len(points):
            continue
        trained = 0
        for character, point in zip(prompt, points):
            try:
                features.append(_click_feature(item["image"], point))
                labels.append(str(character))
                trained += 1
            except (CaptchaTrainingError, KeyError, TypeError, ValueError):
                continue
        valid_train += int(trained == len(prompt))
    if valid_train < 15 or len(set(labels)) < 2:
        raise CaptchaTrainingError("点选验证码有效训练样本或文字种类不足")
    version = _candidate_version("click", samples)
    model = KnnCaptchaModel(
        "click",
        version,
        np.stack(features),
        np.asarray(labels),
    )
    correct = 0
    evaluated = 0
    for item in test:
        prompt = item["answer"].get("prompt")
        points = item["answer"].get("points")
        if not isinstance(prompt, list) or not isinstance(points, list):
            continue
        if not prompt or len(prompt) != len(points):
            continue
        predicted = []
        try:
            for point in points:
                predicted.append(
                    _nearest_label(
                        model.features,
                        model.labels,
                        _click_feature(item["image"], point),
                        model.k,
                    )
                )
        except (CaptchaTrainingError, KeyError, TypeError, ValueError):
            continue
        evaluated += 1
        correct += int(predicted == [str(value) for value in prompt])
    if not evaluated:
        raise CaptchaTrainingError("点选验证码测试集没有可评估样本")
    return CaptchaCandidate(
        captcha_type="click",
        version=version,
        algorithm=model.ALGORITHM,
        artifact=model.to_bytes(),
        sample_count=len(samples),
        test_count=evaluated,
        correct_count=correct,
        metrics={
            "evaluation": "held_out_target_crop_sequence_accuracy",
            "train_samples": valid_train,
            "test_samples": evaluated,
            "note": "检测框仍由内置检测器提供",
        },
    )


def train_candidate(archive_bytes: bytes, captcha_type: str) -> CaptchaCandidate:
    if captcha_type not in {"numeric", "click"}:
        raise CaptchaTrainingError("验证码类型无效")
    samples = [
        item
        for item in _safe_dataset_archive(archive_bytes)
        if item["captcha_type"] == captcha_type
    ]
    available_samples = len(samples)
    if available_samples > MAX_TRAINING_SAMPLES:
        samples = sorted(
            samples,
            key=lambda item: item["fingerprint"],
        )[:MAX_TRAINING_SAMPLES]
    if captcha_type == "numeric":
        candidate = _train_numeric(samples)
    else:
        candidate = _train_click(samples)
    candidate.metrics.update(
        {
            "available_samples": available_samples,
            "selected_samples": len(samples),
        }
    )
    return candidate
