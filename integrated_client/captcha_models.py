from __future__ import annotations

import hashlib
import io
import json
import math
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageOps

from .captcha_tensors import cnn_click_tensor

BUILTIN_MODEL_VERSION = "ddddocr-builtin"
DATASET_SCHEMA_VERSION = 1
MAX_DATASET_BYTES = 100 * 1024 * 1024
MAX_DATASET_FILES = 50_003
MAX_SAMPLE_IMAGE_BYTES = 1 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_MODEL_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_MODEL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MIN_NUMERIC_SAMPLES = 20
MIN_CLICK_SAMPLES = 30
MAX_TRAINING_SAMPLES = 5_000


_ARCHIVE_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


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


@dataclass(frozen=True)
class CaptchaDatasetInspection:
    captcha_type: str
    declared_samples: int
    valid_samples: tuple[dict, ...]
    missing_images: int = 0
    invalid_answers: int = 0
    invalid_images: int = 0
    duplicate_samples: int = 0

    @property
    def valid_count(self) -> int:
        return len(self.valid_samples)

    def rejection_summary(self) -> str:
        reasons = []
        for count, label in (
            (self.missing_images, "图片缺失或路径无效"),
            (self.invalid_answers, "答案格式无效"),
            (self.invalid_images, "图片无法读取"),
            (self.duplicate_samples, "内容重复"),
        ):
            if count:
                reasons.append(f"{label} {count} 条")
        return "、".join(reasons) or "无"


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


HOG_SVM_ALGORITHM = "hog-linear-svm-v1"
ENHANCED_ONNX_ALGORITHM = "tiny-cnn-onnx-v1"
_HOG_WINDOW = (64, 64)
_HOG_BLOCK = (16, 16)
_HOG_STRIDE = (8, 8)
_HOG_CELL = (8, 8)
_HOG_BINS = 9
_HOG_FEATURE_WIDTH = 1_764
_MAX_SVM_CLASSES = 1_024


def _hog_descriptor() -> cv2.HOGDescriptor:
    return cv2.HOGDescriptor(
        _HOG_WINDOW,
        _HOG_BLOCK,
        _HOG_STRIDE,
        _HOG_CELL,
        _HOG_BINS,
    )


def _hog_feature_from_image(
    image: Image.Image,
    *,
    preserve_color: bool = False,
) -> np.ndarray:
    """Return an aspect-preserving HOG descriptor with finite float32 values."""
    if preserve_color:
        source = np.asarray(ImageOps.autocontrast(image.convert("RGB")), dtype=np.uint8)
        background = (0, 0, 0)
    else:
        grayscale = ImageOps.autocontrast(image.convert("L"))
        array = np.asarray(grayscale, dtype=np.uint8)
        _, source = cv2.threshold(
            array,
            0,
            255,
            cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
        )
        if float(source.mean()) > 127:
            source = 255 - source
        background = 0
    source_height, source_width = source.shape[:2]
    if source_height <= 0 or source_width <= 0:
        raise CaptchaTrainingError("验证码图像尺寸无效")
    scale = min(_HOG_WINDOW[0] / source_width, _HOG_WINDOW[1] / source_height)
    target_width = max(1, min(_HOG_WINDOW[0], int(round(source_width * scale))))
    target_height = max(1, min(_HOG_WINDOW[1], int(round(source_height * scale))))
    resized = cv2.resize(
        source,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
    )
    canvas_shape = (_HOG_WINDOW[1], _HOG_WINDOW[0], 3) if preserve_color else (
        _HOG_WINDOW[1],
        _HOG_WINDOW[0],
    )
    canvas = np.full(canvas_shape, background, dtype=np.uint8)
    left = (_HOG_WINDOW[0] - target_width) // 2
    top = (_HOG_WINDOW[1] - target_height) // 2
    canvas[top : top + target_height, left : left + target_width] = resized
    feature = _hog_descriptor().compute(canvas).reshape(-1).astype(np.float32)
    if feature.shape != (_HOG_FEATURE_WIDTH,) or not np.isfinite(feature).all():
        raise CaptchaTrainingError("无法生成有效的 HOG 特征")
    return feature


def _numeric_hog_feature(image_bytes: bytes) -> np.ndarray:
    # Standard numeric training deliberately sees the complete CAPTCHA image.
    # It shares one descriptor between all four position classifiers and never
    # assumes that glyphs occupy four equal-width cells.
    return _hog_feature_from_image(_open_captcha_image(image_bytes, "L"))


def _click_crop(image: Image.Image, point: dict) -> Image.Image:
    width, height = image.size
    center_x = float(point["x"]) * width
    center_y = float(point["y"]) * height
    radius = max(12, int(min(width, height) * 0.11))
    return image.crop(
        (
            max(0, int(center_x - radius)),
            max(0, int(center_y - radius)),
            min(width, int(center_x + radius)),
            min(height, int(center_y + radius)),
        )
    )


def _click_hog_feature(image_bytes: bytes, point: dict) -> np.ndarray:
    image = _open_captcha_image(image_bytes, "RGB")
    return _hog_feature_from_image(_click_crop(image, point), preserve_color=True)


def _bbox_hog_feature(image: Image.Image, bbox) -> np.ndarray:
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
    return _hog_feature_from_image(crop, preserve_color=True)


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
        members = {}
        for info in infos:
            try:
                member_name = _normalize_archive_member(info.filename)
            except ValueError:
                member_name = ""
            if (
                not member_name
                or info.flag_bits & 0x1
                or info.compress_type
                not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise CaptchaTrainingError("数据集包含不安全路径")
            if member_name.endswith("/"):
                # Explicit directory entries are emitted by the v1.0.6
                # exporter; they do not participate in manifest lookups.
                continue
            if member_name in members:
                # ZIP allows duplicate names, but accepting one arbitrarily
                # would make manifest references platform-dependent.
                raise CaptchaTrainingError("数据集包含重复文件路径")
            members[member_name] = info
            total_size += int(info.file_size)
            if total_size > MAX_DATASET_BYTES:
                raise CaptchaTrainingError("数据集解压后大小超出限制")
        try:
            manifest_info = members.get("manifest.json")
            if manifest_info is None:
                raise KeyError("manifest.json")
            manifest = json.loads(
                archive.read(manifest_info).decode("utf-8-sig")
            )
        except (
            KeyError,
            UnicodeError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
        ) as exc:
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
                image_name = _normalize_archive_member(image_name)
                image_info = members.get(image_name)
                if image_info is None:
                    raise KeyError(image_name)
                if (
                    image_info.file_size <= 0
                    or image_info.file_size > MAX_SAMPLE_IMAGE_BYTES
                ):
                    continue
                if image_name not in image_cache:
                    image_cache[image_name] = archive.read(image_info)
                image = image_cache[image_name]
            except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
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


def inspect_training_dataset(
    archive_bytes: bytes,
    captcha_type: str,
) -> CaptchaDatasetInspection:
    """Apply the common standard/enhanced validity rules before training."""
    if captcha_type not in {"numeric", "click"}:
        raise CaptchaTrainingError("验证码类型无效")
    parsed = _safe_dataset_archive(archive_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            manifest_info = next(
                (
                    info
                    for info in archive.infolist()
                    if _normalize_archive_member(info.filename) == "manifest.json"
                ),
                None,
            )
            if manifest_info is None:
                raise ValueError("manifest missing")
            manifest = json.loads(archive.read(manifest_info).decode("utf-8-sig"))
    except (
        OSError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        raise CaptchaTrainingError("数据集清单无效") from exc

    manifest_samples = manifest.get("samples", [])
    exported = sum(
        1
        for item in manifest_samples
        if isinstance(item, dict) and item.get("captcha_type") == captcha_type
    )
    source_count = manifest.get("source_sample_count")
    if (
        manifest.get("captcha_type") == captcha_type
        and type(source_count) is int
        and source_count >= exported
    ):
        declared = source_count
    else:
        declared = exported
    selected = [item for item in parsed if item["captcha_type"] == captcha_type]
    missing_images = max(0, declared - len(selected))
    invalid_answers = 0
    invalid_images = 0
    duplicate_samples = 0
    fingerprints = set()
    valid_samples = []
    for item in selected:
        answer = item["answer"]
        if captcha_type == "numeric":
            value = str(answer.get("value") or "").strip()
            if re.fullmatch(r"[0-9]{4}", value) is None:
                invalid_answers += 1
                continue
            normalized_answer = {"value": value}
        else:
            prompt = answer.get("prompt")
            points = answer.get("points")
            if (
                not isinstance(prompt, list)
                or not isinstance(points, list)
                or not 1 <= len(prompt) <= 8
                or len(prompt) != len(points)
            ):
                invalid_answers += 1
                continue
            normalized_prompt = []
            normalized_points = []
            valid_answer = True
            for label, point in zip(prompt, points):
                label = str(label or "").strip()
                if (
                    not label
                    or len(label) > 4
                    or not isinstance(point, dict)
                    or set(point) != {"x", "y"}
                ):
                    valid_answer = False
                    break
                try:
                    x = float(point["x"])
                    y = float(point["y"])
                except (TypeError, ValueError):
                    valid_answer = False
                    break
                if not math.isfinite(x) or not math.isfinite(y) or not (
                    0 <= x <= 1 and 0 <= y <= 1
                ):
                    valid_answer = False
                    break
                normalized_prompt.append(label)
                normalized_points.append({"x": x, "y": y})
            if not valid_answer:
                invalid_answers += 1
                continue
            normalized_answer = {
                "prompt": normalized_prompt,
                "points": normalized_points,
            }
        try:
            with Image.open(io.BytesIO(item["image"])) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("image pixel count exceeded")
                image.verify()
        except (OSError, SyntaxError, ValueError):
            invalid_images += 1
            continue
        fingerprint = str(item.get("fingerprint") or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            digest = hashlib.sha256()
            digest.update(captcha_type.encode("ascii"))
            digest.update(b"\0")
            digest.update(item["image"])
            digest.update(b"\0")
            digest.update(
                json.dumps(
                    normalized_answer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            fingerprint = digest.hexdigest()
        if fingerprint in fingerprints:
            duplicate_samples += 1
            continue
        fingerprints.add(fingerprint)
        valid_samples.append(
            {
                **item,
                "answer": normalized_answer,
                "fingerprint": fingerprint,
            }
        )
    return CaptchaDatasetInspection(
        captcha_type=captcha_type,
        declared_samples=declared,
        valid_samples=tuple(valid_samples),
        missing_images=missing_images,
        invalid_answers=invalid_answers,
        invalid_images=invalid_images,
        duplicate_samples=duplicate_samples,
    )


def _normalize_archive_member(name: str) -> str:
    """Return a canonical, safe ZIP member name.

    ZIP producers on Windows occasionally emit backslashes or a leading
    ``./``.  Normalizing those forms lets old exports and new exports share
    the same reader while retaining traversal and absolute-path protection.
    """
    text = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/"):
        raise ValueError("unsafe archive path")
    if _ARCHIVE_DRIVE_PREFIX.match(text):
        raise ValueError("unsafe archive path")
    directory = text.endswith("/")
    parts = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("unsafe archive path")
        parts.append(part)
    if not parts:
        raise ValueError("empty archive path")
    normalized = "/".join(parts)
    return normalized + ("/" if directory else "")


def _split_samples(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(samples, key=lambda item: item["fingerprint"])
    test = [item for index, item in enumerate(ordered) if index % 5 == 0]
    train = [item for index, item in enumerate(ordered) if index % 5 != 0]
    if not test or not train:
        raise CaptchaTrainingError("数据集无法划分训练集和测试集")
    return train, test


def _split_numeric_samples(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build a deterministic holdout without removing a position's last digit."""
    ordered = sorted(samples, key=lambda item: item["fingerprint"])
    counts = [
        {
            digit: sum(
                item["answer"]["value"][position] == digit
                for item in ordered
            )
            for digit in "0123456789"
        }
        for position in range(4)
    ]
    target = max(1, len(ordered) // 5)
    test = []
    train = []
    for item in ordered:
        value = item["answer"]["value"]
        can_hold_out = len(test) < target and all(
            counts[position][digit] > 1
            for position, digit in enumerate(value)
        )
        if can_hold_out:
            test.append(item)
            for position, digit in enumerate(value):
                counts[position][digit] -= 1
        else:
            train.append(item)
    if not test or not train:
        return _split_samples(ordered)
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
    """Read-only compatibility model for artifacts created before v1.1.0."""

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
                members = set()
                for info in infos:
                    if (
                        info.flag_bits & 0x1
                        or info.compress_type
                        not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    ):
                        raise ValueError("unsafe model path")
                    try:
                        member_name = _normalize_archive_member(info.filename)
                    except ValueError as exc:
                        raise ValueError("unsafe model path") from exc
                    if member_name.endswith("/") or member_name in members:
                        raise ValueError("duplicate or empty model path")
                    members.add(member_name)
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


def _train_binary_linear_svm(features: np.ndarray, positive: np.ndarray):
    features = np.ascontiguousarray(features, dtype=np.float32)
    positive = np.asarray(positive, dtype=bool)
    positive_count = int(np.count_nonzero(positive))
    negative_count = int(len(positive) - positive_count)
    if not positive_count or not negative_count:
        raise CaptchaTrainingError("线性 SVM 每个分类器都需要正负样本")
    responses = np.where(positive, 1, -1).astype(np.int32)
    svm = cv2.ml.SVM_create()
    svm.setType(cv2.ml.SVM_C_SVC)
    svm.setKernel(cv2.ml.SVM_LINEAR)
    svm.setC(1.0)
    svm.setTermCriteria(
        (cv2.TERM_CRITERIA_MAX_ITER | cv2.TERM_CRITERIA_EPS, 2_000, 1e-6)
    )
    # cv::ml orders the responses as -1, +1. Balance one-vs-rest training so
    # uncommon glyphs are not overwhelmed by negative samples.
    svm.setClassWeights(
        np.asarray(
            [1.0, min(100.0, negative_count / positive_count)],
            dtype=np.float32,
        )
    )
    if not svm.train(features, cv2.ml.ROW_SAMPLE, responses):
        raise CaptchaTrainingError("OpenCV 无法训练线性 SVM")
    support_vectors = np.asarray(svm.getSupportVectors(), dtype=np.float64)
    rho, alpha, support_indexes = svm.getDecisionFunction(0)
    alpha = np.asarray(alpha, dtype=np.float64).reshape(1, -1)
    support_indexes = np.asarray(support_indexes, dtype=np.int64).reshape(-1)
    if (
        support_vectors.ndim != 2
        or support_vectors.shape[1] != features.shape[1]
        or alpha.shape[1] != len(support_indexes)
        or np.any(support_indexes < 0)
        or np.any(support_indexes >= len(support_vectors))
    ):
        raise CaptchaTrainingError("OpenCV 返回了无效的线性 SVM 参数")
    # RAW_OUTPUT is positive for the -1 response. Negating it produces a
    # positive-class score that can be compared across the OVR classifiers.
    raw_weight = alpha @ support_vectors[support_indexes]
    weight = -raw_weight.reshape(-1)
    bias = float(rho)
    norm = float(np.linalg.norm(weight))
    if not np.isfinite(norm) or norm <= 1e-12 or not np.isfinite(bias):
        raise CaptchaTrainingError(
            "线性 SVM 无法区分当前样本；请增加图像差异更大的验证码样本后重试"
        )
    return (weight / norm).astype(np.float32), np.float32(bias / norm)


def _train_ovr_linear_svm(
    features: np.ndarray,
    labels,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    features = np.asarray(features, dtype=np.float32)
    labels = np.asarray(labels).astype(str)
    if (
        features.ndim != 2
        or features.shape[1] != _HOG_FEATURE_WIDTH
        or labels.ndim != 1
        or len(features) != len(labels)
        or not len(labels)
        or not np.isfinite(features).all()
    ):
        raise CaptchaTrainingError("HOG 训练特征无效")
    classes = np.asarray(sorted(set(labels.tolist())))
    if len(classes) < 2 or len(classes) > _MAX_SVM_CLASSES:
        raise CaptchaTrainingError("线性 SVM 标签种类数量无效")
    parameters = [
        _train_binary_linear_svm(features, labels == label) for label in classes
    ]
    return (
        classes.astype(str),
        np.stack([item[0] for item in parameters]).astype(np.float32),
        np.asarray([item[1] for item in parameters], dtype=np.float32),
    )


def _validate_npz_container(artifact: bytes, *, maximum_members: int) -> None:
    if not artifact or len(artifact) > MAX_MODEL_ARTIFACT_BYTES:
        raise CaptchaTrainingError("候选模型为空或大小超过限制")
    try:
        with zipfile.ZipFile(io.BytesIO(artifact), "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > maximum_members:
                raise ValueError("invalid model member count")
            total_size = 0
            members = set()
            for info in infos:
                if (
                    info.flag_bits & 0x1
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ValueError("unsafe model path")
                name = _normalize_archive_member(info.filename)
                if name.endswith("/") or name in members:
                    raise ValueError("duplicate or empty model path")
                members.add(name)
                total_size += int(info.file_size)
                if total_size > MAX_MODEL_UNCOMPRESSED_BYTES:
                    raise ValueError("model expands beyond limit")
    except CaptchaTrainingError:
        raise
    except Exception as exc:
        raise CaptchaTrainingError(f"候选模型容器无效：{exc}") from exc


class HogLinearSvmCaptchaModel:
    """Portable array-only HOG + one-vs-rest linear SVM model."""

    ALGORITHM = HOG_SVM_ALGORITHM
    SCHEMA_VERSION = 2

    def __init__(
        self,
        captcha_type: str,
        version: str,
        labels: np.ndarray,
        class_counts: np.ndarray,
        weights: np.ndarray,
        biases: np.ndarray,
    ):
        if captcha_type not in {"numeric", "click"}:
            raise CaptchaTrainingError("模型验证码类型无效")
        version = str(version)
        labels = np.asarray(labels)
        class_counts = np.asarray(class_counts)
        weights = np.asarray(weights)
        biases = np.asarray(biases)
        positions = 4 if captcha_type == "numeric" else 1
        maximum_classes = 10 if captcha_type == "numeric" else _MAX_SVM_CLASSES
        if (
            not version
            or len(version) > 128
            or labels.dtype.kind not in {"U", "S"}
            or labels.ndim != 2
            or labels.shape[0] != positions
            or labels.shape[1] < 2
            or labels.shape[1] > maximum_classes
            or class_counts.ndim != 1
            or class_counts.shape != (positions,)
            or class_counts.dtype.kind not in {"i", "u"}
            or class_counts.dtype.itemsize != 4
            or weights.dtype.kind != "f"
            or weights.dtype.itemsize != 4
            or weights.shape
            != (positions, labels.shape[1], _HOG_FEATURE_WIDTH)
            or biases.dtype.kind != "f"
            or biases.dtype.itemsize != 4
            or biases.shape != (positions, labels.shape[1])
            or not np.isfinite(weights).all()
            or not np.isfinite(biases).all()
        ):
            raise CaptchaTrainingError("HOG 线性 SVM 模型数组无效")
        labels = labels.astype(str)
        class_counts = class_counts.astype(np.int32)
        for position, count_value in enumerate(class_counts):
            count = int(count_value)
            active = labels[position, :count].tolist()
            padding = labels[position, count:].tolist()
            if (
                count < 2
                or count > labels.shape[1]
                or len(set(active)) != count
                or any(not label or len(label) > 4 for label in active)
                or any(padding)
            ):
                raise CaptchaTrainingError("HOG 线性 SVM 标签无效")
            if captcha_type == "numeric" and any(
                len(label) != 1 or label not in "0123456789" for label in active
            ):
                raise CaptchaTrainingError("数字模型标签无效")
            if (
                np.any(np.linalg.norm(weights[position, :count], axis=1) <= 1e-12)
                or np.any(weights[position, count:] != 0)
                or np.any(biases[position, count:] != 0)
            ):
                raise CaptchaTrainingError("HOG 线性 SVM 分类器参数无效")
        self.captcha_type = captcha_type
        self.version = version
        self.labels = labels
        self.class_counts = class_counts
        self.weights = np.ascontiguousarray(weights, dtype=np.float32)
        self.biases = np.ascontiguousarray(biases, dtype=np.float32)

    def _predict_feature(self, feature: np.ndarray, position: int = 0) -> str:
        feature = np.asarray(feature, dtype=np.float32)
        if feature.shape != (_HOG_FEATURE_WIDTH,) or not np.isfinite(feature).all():
            raise CaptchaTrainingError("HOG 预测特征无效")
        count = int(self.class_counts[position])
        scores = self.weights[position, :count] @ feature
        scores += self.biases[position, :count]
        return str(self.labels[position, int(np.argmax(scores))])

    def predict_numeric(self, image_bytes: bytes) -> str:
        if self.captcha_type != "numeric":
            return ""
        feature = _numeric_hog_feature(image_bytes)
        return "".join(
            self._predict_feature(feature, position) for position in range(4)
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
                label = self._predict_feature(_bbox_hog_feature(image, bbox))
                x1, y1, x2, y2 = (int(value) for value in bbox)
                positions.setdefault(label, ((x1 + x2) // 2, (y1 + y2) // 2))
            except (CaptchaTrainingError, TypeError, ValueError, OverflowError):
                continue
        return positions

    def to_bytes(self) -> bytes:
        output = io.BytesIO()
        np.savez_compressed(
            output,
            schema_version=np.asarray([self.SCHEMA_VERSION], dtype=np.int16),
            captcha_type=np.asarray([self.captcha_type]),
            version=np.asarray([self.version]),
            algorithm=np.asarray([self.ALGORITHM]),
            feature_width=np.asarray([_HOG_FEATURE_WIDTH], dtype=np.int32),
            labels=self.labels,
            class_counts=self.class_counts.astype(np.int32),
            weights=self.weights,
            biases=self.biases,
        )
        artifact = output.getvalue()
        if len(artifact) > MAX_MODEL_ARTIFACT_BYTES:
            raise CaptchaTrainingError("HOG 线性 SVM 模型大小超过限制")
        return artifact

    @classmethod
    def from_bytes(cls, artifact: bytes) -> HogLinearSvmCaptchaModel:
        _validate_npz_container(artifact, maximum_members=16)
        try:
            with np.load(io.BytesIO(artifact), allow_pickle=False) as data:
                required = {
                    "schema_version",
                    "captcha_type",
                    "version",
                    "algorithm",
                    "feature_width",
                    "labels",
                    "class_counts",
                    "weights",
                    "biases",
                }
                if set(data.files) != required:
                    raise ValueError("unexpected model members")
                if (
                    data["schema_version"].shape != (1,)
                    or data["schema_version"].dtype.kind not in {"i", "u"}
                    or int(data["schema_version"][0]) != cls.SCHEMA_VERSION
                    or data["algorithm"].shape != (1,)
                    or data["algorithm"].dtype.kind not in {"U", "S"}
                    or str(data["algorithm"][0]) != cls.ALGORITHM
                    or data["captcha_type"].shape != (1,)
                    or data["captcha_type"].dtype.kind not in {"U", "S"}
                    or data["version"].shape != (1,)
                    or data["version"].dtype.kind not in {"U", "S"}
                    or data["feature_width"].shape != (1,)
                    or data["feature_width"].dtype.kind not in {"i", "u"}
                    or int(data["feature_width"][0]) != _HOG_FEATURE_WIDTH
                ):
                    raise ValueError("invalid model metadata")
                return cls(
                    str(data["captcha_type"][0]),
                    str(data["version"][0]),
                    data["labels"],
                    data["class_counts"],
                    data["weights"],
                    data["biases"],
                )
        except CaptchaTrainingError:
            raise
        except Exception as exc:
            raise CaptchaTrainingError(f"无法读取 HOG 线性 SVM 模型：{exc}") from exc


def _cnn_click_tensor(image: Image.Image, bbox) -> np.ndarray:
    try:
        return cnn_click_tensor(image, bbox)
    except ValueError as exc:
        raise CaptchaTrainingError("点选候选框尺寸无效") from exc


class TinyCnnOnnxCaptchaModel:
    """Inference-only adapter for an enhanced component's portable ONNX model."""

    ALGORITHM = ENHANCED_ONNX_ALGORITHM

    def __init__(self, captcha_type: str, version: str, session, labels=None):
        self.captcha_type = captcha_type
        self.version = version
        self.session = session
        self.labels = list(labels or [])
        self.input_name = session.get_inputs()[0].name
        self.output_name = session.get_outputs()[0].name

    @staticmethod
    def _fixed_dimension(value, expected: int) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == expected
        )

    @staticmethod
    def _matching_dynamic_batch(input_value, output_value) -> bool:
        if input_value is None or output_value is None:
            return input_value is None and output_value is None
        return (
            isinstance(input_value, str)
            and isinstance(output_value, str)
            and bool(input_value)
            and input_value == output_value
            and len(input_value) <= 128
        )

    @staticmethod
    def _smoke_test(session, input_name: str, output_name: str, tensor, shape) -> None:
        try:
            result = session.run([output_name], {input_name: tensor})
        except Exception as exc:
            raise ValueError("ONNX runtime smoke test failed") from exc
        if not isinstance(result, (list, tuple)) or len(result) != 1:
            raise ValueError("ONNX runtime returned an invalid output list")
        output = np.asarray(result[0])
        if (
            output.shape != shape
            or output.dtype != np.dtype(np.float32)
            or not np.isfinite(output).all()
        ):
            raise ValueError("ONNX runtime output failed validation")

    @classmethod
    def from_bytes(
        cls,
        captcha_type: str,
        version: str,
        artifact: bytes,
    ) -> TinyCnnOnnxCaptchaModel:
        if (
            captcha_type not in {"numeric", "click"}
            or not version
            or len(str(version)) > 128
            or not artifact
            or len(artifact) > MAX_MODEL_ARTIFACT_BYTES
        ):
            raise CaptchaTrainingError("强化 ONNX 模型信息无效")
        try:
            import onnxruntime as ort

            session = ort.InferenceSession(
                artifact,
                providers=["CPUExecutionProvider"],
            )
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            metadata = dict(session.get_modelmeta().custom_metadata_map or {})
            if (
                len(inputs) != 1
                or len(outputs) != 1
                or inputs[0].type != "tensor(float)"
                or outputs[0].type != "tensor(float)"
                or metadata.get("algorithm") != cls.ALGORITHM
                or metadata.get("captcha_type") != captcha_type
                or metadata.get("version") != str(version)
            ):
                raise ValueError("ONNX metadata or I/O type mismatch")
            input_shape = list(inputs[0].shape)
            output_shape = list(outputs[0].shape)
            input_name = inputs[0].name
            output_name = outputs[0].name
            labels = []
            if captcha_type == "numeric":
                if (
                    len(input_shape) != 4
                    or not all(
                        cls._fixed_dimension(actual, expected)
                        for actual, expected in zip(input_shape, (1, 1, 48, 112))
                    )
                    or len(output_shape) != 3
                    or not all(
                        cls._fixed_dimension(actual, expected)
                        for actual, expected in zip(output_shape, (1, 4, 10))
                    )
                ):
                    raise ValueError("numeric ONNX shape mismatch")
                cls._smoke_test(
                    session,
                    input_name,
                    output_name,
                    np.zeros((1, 1, 48, 112), dtype=np.float32),
                    (1, 4, 10),
                )
            else:
                labels = json.loads(metadata.get("labels_json") or "")
                class_count = output_shape[1] if len(output_shape) == 2 else None
                if (
                    len(input_shape) != 4
                    or any(
                        not cls._fixed_dimension(actual, expected)
                        for actual, expected in zip(input_shape[1:], (3, 64, 64))
                    )
                    or len(output_shape) != 2
                    or not cls._matching_dynamic_batch(
                        input_shape[0],
                        output_shape[0],
                    )
                    or not isinstance(class_count, int)
                    or isinstance(class_count, bool)
                    or class_count < 2
                    or class_count > _MAX_SVM_CLASSES
                    or not isinstance(labels, list)
                    or len(labels) != class_count
                    or len(set(labels)) != len(labels)
                    or any(
                        not isinstance(label, str) or not label or len(label) > 4
                        for label in labels
                    )
                ):
                    raise ValueError("click ONNX shape or labels mismatch")
                for batch_size in (1, 2):
                    cls._smoke_test(
                        session,
                        input_name,
                        output_name,
                        np.zeros((batch_size, 3, 64, 64), dtype=np.float32),
                        (batch_size, class_count),
                    )
            return cls(captcha_type, str(version), session, labels)
        except CaptchaTrainingError:
            raise
        except Exception as exc:
            raise CaptchaTrainingError(f"无法加载强化 ONNX 模型：{exc}") from exc

    def predict_numeric(self, image_bytes: bytes) -> str:
        if self.captcha_type != "numeric":
            return ""
        image = _open_captcha_image(image_bytes, "L").resize(
            (112, 48),
            Image.Resampling.LANCZOS,
        )
        tensor = np.asarray(image, dtype=np.float32)[None, None, :, :] / 255.0
        try:
            logits = np.asarray(
                self.session.run([self.output_name], {self.input_name: tensor})[0],
                dtype=np.float32,
            )
        except Exception as exc:
            raise CaptchaTrainingError(f"强化数字模型推理失败：{exc}") from exc
        if logits.shape != (1, 4, 10) or not np.isfinite(logits).all():
            raise CaptchaTrainingError("强化数字模型输出无效")
        return "".join(str(int(value)) for value in np.argmax(logits[0], axis=1))

    def predict_click_regions(
        self,
        image_bytes: bytes,
        bboxes,
    ) -> dict[str, tuple[int, int]]:
        if self.captcha_type != "click":
            return {}
        try:
            image = _open_captcha_image(image_bytes, "RGB")
            boxes = [tuple(int(value) for value in bbox) for bbox in bboxes]
            if not boxes:
                return {}
            tensor = np.stack([_cnn_click_tensor(image, bbox) for bbox in boxes])
            logits = np.asarray(
                self.session.run([self.output_name], {self.input_name: tensor})[0],
                dtype=np.float32,
            )
            if logits.shape != (len(boxes), len(self.labels)) or not np.isfinite(
                logits
            ).all():
                raise CaptchaTrainingError("强化点选模型输出无效")
        except (CaptchaTrainingError, TypeError, ValueError, OverflowError):
            return {}
        except Exception:
            return {}
        positions = {}
        for bbox, class_index in zip(boxes, np.argmax(logits, axis=1)):
            x1, y1, x2, y2 = bbox
            positions.setdefault(
                self.labels[int(class_index)],
                ((x1 + x2) // 2, (y1 + y2) // 2),
            )
        return positions


def _npz_algorithm(artifact: bytes) -> str:
    _validate_npz_container(artifact, maximum_members=16)
    try:
        with np.load(io.BytesIO(artifact), allow_pickle=False) as data:
            value = data["algorithm"]
            if value.shape != (1,) or value.dtype.kind not in {"U", "S"}:
                raise ValueError("invalid algorithm field")
            algorithm = str(value[0])
            if len(algorithm) > 80:
                raise ValueError("algorithm field too long")
            return algorithm
    except CaptchaTrainingError:
        raise
    except Exception as exc:
        raise CaptchaTrainingError(f"无法读取候选模型算法：{exc}") from exc


class CaptchaModelManager:
    def __init__(self):
        self._models = {}
        self._lock = threading.RLock()

    def install(
        self,
        captcha_type: str,
        version: str,
        artifact: bytes,
        *,
        expected_algorithm: str | None = None,
    ) -> None:
        artifact = bytes(artifact or b"")
        if (
            expected_algorithm is not None
            and expected_algorithm
            not in {HOG_SVM_ALGORITHM, ENHANCED_ONNX_ALGORITHM}
        ):
            raise CaptchaTrainingError("不支持服务端清单声明的模型算法")
        if artifact.startswith(b"PK"):
            algorithm = _npz_algorithm(artifact)
            if expected_algorithm is not None and algorithm != expected_algorithm:
                raise CaptchaTrainingError("模型算法与服务端清单不一致")
            if algorithm == HogLinearSvmCaptchaModel.ALGORITHM:
                model = HogLinearSvmCaptchaModel.from_bytes(artifact)
            elif algorithm == KnnCaptchaModel.ALGORITHM:
                # Legacy artifacts remain loadable during the v1.1 migration,
                # but train_candidate never creates them.
                model = KnnCaptchaModel.from_bytes(artifact)
            else:
                raise CaptchaTrainingError(f"不支持的验证码模型算法：{algorithm}")
        else:
            if (
                expected_algorithm is not None
                and expected_algorithm != ENHANCED_ONNX_ALGORITHM
            ):
                raise CaptchaTrainingError("模型算法与服务端清单不一致")
            model = TinyCnnOnnxCaptchaModel.from_bytes(
                captcha_type,
                version,
                artifact,
            )
        if model.captcha_type != captcha_type or model.version != version:
            raise CaptchaTrainingError("模型类型或版本与清单不一致")
        with self._lock:
            self._models[captcha_type] = model

    def clear(self, captcha_type: str) -> None:
        with self._lock:
            self._models.pop(captcha_type, None)

    def get(self, captcha_type: str):
        with self._lock:
            return self._models.get(captcha_type)

    def version(self, captcha_type: str) -> str:
        model = self.get(captcha_type)
        return model.version if model is not None else BUILTIN_MODEL_VERSION

    def algorithm(self, captcha_type: str) -> str:
        model = self.get(captcha_type)
        return model.ALGORITHM if model is not None else BUILTIN_MODEL_VERSION


def _candidate_version(captcha_type: str, samples: list[dict]) -> str:
    digest = hashlib.sha256()
    for item in sorted(samples, key=lambda sample: sample["fingerprint"]):
        digest.update(item["fingerprint"].encode("ascii", errors="ignore"))
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    return f"{captcha_type}-hog-svm-{stamp}-{digest.hexdigest()[:8]}"


def _train_numeric(samples: list[dict]) -> CaptchaCandidate:
    if len(samples) < MIN_NUMERIC_SAMPLES:
        raise CaptchaTrainingError(
            f"数字验证码至少需要 {MIN_NUMERIC_SAMPLES} 个成功样本"
        )
    train, test = _split_numeric_samples(samples)
    features = []
    values = []
    valid_train = 0
    for item in train:
        value = str(item["answer"].get("value") or "")
        if len(value) != 4 or any(character not in "0123456789" for character in value):
            continue
        try:
            feature = _numeric_hog_feature(item["image"])
        except CaptchaTrainingError:
            continue
        features.append(feature)
        values.append(value)
        valid_train += 1
    if valid_train < 10:
        raise CaptchaTrainingError("数字验证码有效训练样本或字符种类不足")
    version = _candidate_version("numeric", samples)
    feature_matrix = np.stack(features).astype(np.float32)
    classifiers = [
        _train_ovr_linear_svm(
            feature_matrix,
            [value[position] for value in values],
        )
        for position in range(4)
    ]
    maximum_classes = max(len(item[0]) for item in classifiers)
    model_labels = np.full((4, maximum_classes), "", dtype="<U4")
    class_counts = np.zeros(4, dtype=np.int32)
    weights = np.zeros(
        (4, maximum_classes, _HOG_FEATURE_WIDTH),
        dtype=np.float32,
    )
    biases = np.zeros((4, maximum_classes), dtype=np.float32)
    for position, (position_labels, position_weights, position_biases) in enumerate(
        classifiers
    ):
        count = len(position_labels)
        class_counts[position] = count
        model_labels[position, :count] = position_labels
        weights[position, :count] = position_weights
        biases[position, :count] = position_biases
    model = HogLinearSvmCaptchaModel(
        "numeric",
        version,
        model_labels,
        class_counts,
        weights,
        biases,
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
            "feature": "hog-64x64",
            "classifiers": "four_position_ovr_linear_svm_shared_full_image",
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
                features.append(_click_hog_feature(item["image"], point))
                labels.append(str(character))
                trained += 1
            except (CaptchaTrainingError, KeyError, TypeError, ValueError):
                continue
        valid_train += int(trained == len(prompt))
    if valid_train < 15 or len(set(labels)) < 2:
        raise CaptchaTrainingError("点选验证码有效训练样本或文字种类不足")
    version = _candidate_version("click", samples)
    click_labels, click_weights, click_biases = _train_ovr_linear_svm(
        np.stack(features),
        np.asarray(labels),
    )
    model = HogLinearSvmCaptchaModel(
        "click",
        version,
        click_labels.reshape(1, -1),
        np.asarray([len(click_labels)], dtype=np.int32),
        click_weights.reshape(1, len(click_labels), _HOG_FEATURE_WIDTH),
        click_biases.reshape(1, -1),
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
                    model._predict_feature(_click_hog_feature(item["image"], point))
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
            "feature": "hog-64x64",
            "classifiers": "candidate_crop_ovr_linear_svm",
            "note": "检测框仍由内置检测器提供",
        },
    )


def train_candidate(
    archive_bytes: bytes,
    captcha_type: str,
    mode: str = "standard",
) -> CaptchaCandidate:
    if captcha_type not in {"numeric", "click"}:
        raise CaptchaTrainingError("验证码类型无效")
    if str(mode).strip().lower() != "standard":
        raise CaptchaTrainingError("强化模式训练必须由本机强化训练组件执行")
    inspection = inspect_training_dataset(archive_bytes, captcha_type)
    samples = list(inspection.valid_samples)
    minimum = (
        MIN_NUMERIC_SAMPLES if captcha_type == "numeric" else MIN_CLICK_SAMPLES
    )
    if len(samples) < minimum:
        raise CaptchaTrainingError(
            f"服务器记录 {inspection.declared_samples} 条，实际有效且不重复的"
            f"{('数字' if captcha_type == 'numeric' else '点选')}验证码只有 {len(samples)} 条，"
            f"至少需要 {minimum} 条。过滤原因：{inspection.rejection_summary()}"
        )
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
