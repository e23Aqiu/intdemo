"""Prompt-conditioned click CAPTCHA detection, features and assignment."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageStat

CLICK_FEATURE_SCHEMA = "click-pair-features-v1"
CLICK_FEATURE_COUNT = 33
MAX_CLICK_CANDIDATES = 12
VIEW_NAMES = ("original", "sharpen", "contrast", "gray")


class ClickEngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClickCandidate:
    box: tuple[int, int, int, int]
    view_votes: int = 1

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.box
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True)
class ClickFeatures:
    features: np.ndarray
    base_scores: np.ndarray
    candidates: tuple[ClickCandidate, ...]
    width: int
    height: int


@dataclass(frozen=True)
class ClickResolution:
    status: str
    points: tuple[dict[str, float], ...]
    confidence: float
    reason: str = ""


def _png_bytes(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _image_variants(image: Image.Image) -> tuple[Image.Image, ...]:
    rgb = image.convert("RGB")
    return (
        rgb,
        rgb.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3)),
        ImageEnhance.Contrast(rgb).enhance(1.5),
        rgb.convert("L").convert("RGB"),
    )


def _normalize_box(raw, width: int, height: int) -> tuple[int, int, int, int] | None:
    try:
        if len(raw) == 4 and all(not isinstance(value, (list, tuple)) for value in raw):
            left, top, right, bottom = (round(float(value)) for value in raw)
        else:
            points = list(raw)
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            left, top, right, bottom = (
                round(min(xs)),
                round(min(ys)),
                round(max(xs)),
                round(max(ys)),
            )
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    left = max(0, min(width - 1, left))
    top = max(0, min(height - 1, top))
    right = max(left + 1, min(width, right))
    bottom = max(top + 1, min(height, bottom))
    if right - left < 3 or bottom - top < 3:
        return None
    return left, top, right, bottom


def _intersection_over_union(first, second) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if not intersection:
        return 0.0
    first_area = max(1, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection / max(1, first_area + second_area - intersection)


def _near_same_box(first, second) -> bool:
    if _intersection_over_union(first, second) >= 0.45:
        return True
    first_x = (first[0] + first[2]) / 2
    first_y = (first[1] + first[3]) / 2
    second_x = (second[0] + second[2]) / 2
    second_y = (second[1] + second[3]) / 2
    scale = max(
        4.0,
        min(
            first[2] - first[0],
            first[3] - first[1],
            second[2] - second[0],
            second[3] - second[1],
        )
        * 0.35,
    )
    return math.hypot(first_x - second_x, first_y - second_y) <= scale


def _merge_boxes(
    detected: Sequence[tuple[tuple[int, int, int, int], int]],
) -> tuple[ClickCandidate, ...]:
    clusters: list[dict[str, object]] = []
    for box, view_index in detected:
        match = None
        for cluster in clusters:
            if _near_same_box(cluster["box"], box):
                match = cluster
                break
        if match is None:
            clusters.append({"boxes": [box], "views": {view_index}, "box": box})
            continue
        boxes = match["boxes"]
        boxes.append(box)
        match["views"].add(view_index)
        match["box"] = tuple(
            round(sum(item[position] for item in boxes) / len(boxes))
            for position in range(4)
        )
    candidates = [
        ClickCandidate(tuple(cluster["box"]), len(cluster["views"]))
        for cluster in clusters
    ]
    if len(candidates) > MAX_CLICK_CANDIDATES:
        candidates = sorted(
            candidates,
            key=lambda item: (
                -item.view_votes,
                (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]),
                item.box[0],
            ),
        )[:MAX_CLICK_CANDIDATES]
    return tuple(sorted(candidates, key=lambda item: (item.box[0], item.box[1])))


def _probability_matrix(result) -> tuple[np.ndarray | None, list[str], str, float]:
    if not isinstance(result, dict):
        return None, [], str(result or "").strip(), 0.0
    # ddddocr 1.5.x uses ``charsets``/``probability`` while 1.6.x uses
    # ``charset``/``probabilities`` and also returns decoded text/confidence.
    charset = result.get("charset")
    if not isinstance(charset, list):
        charset = result.get("charsets")
    probabilities = (
        result.get("probabilities")
        if "probabilities" in result
        else result.get("probability")
    )
    text = str(result.get("text") or "").strip()
    if not isinstance(charset, list) or not charset:
        return None, [], text, 0.0
    try:
        matrix = np.asarray(probabilities, dtype=np.float32)
        while matrix.ndim > 2 and matrix.shape[0] == 1:
            matrix = matrix[0]
        if (
            matrix.ndim != 2
            or matrix.shape[1] != len(charset)
            or not np.isfinite(matrix).all()
            or np.any(matrix < 0)
        ):
            return None, [], text, 0.0
    except (TypeError, ValueError, OverflowError):
        return None, [], text, 0.0
    row_sums = np.sum(matrix, axis=1, keepdims=True)
    matrix = np.divide(
        matrix,
        row_sums,
        out=np.zeros_like(matrix),
        where=row_sums > 1e-12,
    )
    normalized_charset = [str(value) for value in charset]
    if not text and matrix.shape[0]:
        decoded = []
        previous = -1
        for raw_index in np.argmax(matrix, axis=1):
            index = int(raw_index)
            if index != previous and normalized_charset[index]:
                decoded.append(normalized_charset[index])
            previous = index
        text = "".join(decoded)
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError, OverflowError):
        confidence = float(np.mean(np.max(matrix, axis=1))) if matrix.shape[0] else 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    return (
        matrix,
        normalized_charset,
        text,
        max(0.0, min(1.0, confidence)),
    )


def _crop_geometry(
    image: Image.Image,
    candidate: ClickCandidate,
) -> tuple[Image.Image, list[float]]:
    left, top, right, bottom = candidate.box
    padding = 3
    crop = image.crop(
        (
            max(0, left - padding),
            max(0, top - padding),
            min(image.width, right + padding),
            min(image.height, bottom + padding),
        )
    ).convert("RGB")
    width = max(1, right - left)
    height = max(1, bottom - top)
    gray = crop.convert("L")
    contrast = float(ImageStat.Stat(gray).stddev[0]) / 128.0
    edges = gray.filter(ImageFilter.FIND_EDGES)
    sharpness = float(ImageStat.Stat(edges).mean[0]) / 255.0
    geometry = [
        ((left + right) / 2) / max(1, image.width),
        ((top + bottom) / 2) / max(1, image.height),
        width / max(1, image.width),
        height / max(1, image.height),
        (width * height) / max(1, image.width * image.height),
        min(4.0, width / max(1, height)) / 4.0,
        candidate.view_votes / len(VIEW_NAMES),
        max(0.0, min(2.0, contrast)) / 2.0,
        max(0.0, min(1.0, sharpness)),
    ]
    return crop, geometry


class ClickFeatureExtractor:
    """Own the OCR engines so training and online inference stay identical."""

    def __init__(self, *, detector=None, recognizer=None):
        if detector is None or recognizer is None:
            try:
                from ddddocr import DdddOcr
            except ImportError as exc:
                raise ClickEngineError("验证码推理运行时缺少 ddddocr 运行库") from exc
            detector = detector or DdddOcr(det=True, ocr=False, show_ad=False)
            recognizer = recognizer or DdddOcr(det=False, ocr=True, show_ad=False)
        self.detector = detector
        self.recognizer = recognizer

    def detect(self, image: Image.Image) -> tuple[ClickCandidate, ...]:
        detected = []
        for view_index, variant in enumerate(_image_variants(image)):
            try:
                boxes = self.detector.detection(_png_bytes(variant)) or []
            except Exception:  # noqa: BLE001 - one view may fail independently
                boxes = []
            for raw in boxes:
                box = _normalize_box(raw, image.width, image.height)
                if box is not None:
                    detected.append((box, view_index))
        return _merge_boxes(detected)

    def extract(
        self,
        image_bytes: bytes,
        targets: Sequence[str],
        *,
        candidates: Sequence[ClickCandidate] | None = None,
    ) -> ClickFeatures:
        try:
            with Image.open(io.BytesIO(bytes(image_bytes))) as opened:
                image = opened.convert("RGB")
        except Exception as exc:
            raise ClickEngineError(f"无法读取点选验证码图片：{exc}") from exc
        target_list = [str(value or "").strip() for value in targets]
        if not target_list or len(target_list) > 8 or any(not value for value in target_list):
            raise ClickEngineError("点选验证码目标文字无效")
        candidate_list = tuple(candidates) if candidates is not None else self.detect(image)
        if not candidate_list:
            return ClickFeatures(
                np.zeros((len(target_list), 0, CLICK_FEATURE_COUNT), dtype=np.float32),
                np.zeros((len(target_list), 0), dtype=np.float32),
                (),
                image.width,
                image.height,
            )

        view_target_sum = np.zeros(
            (len(target_list), len(candidate_list), len(VIEW_NAMES)),
            dtype=np.float32,
        )
        view_target_max = np.zeros_like(view_target_sum)
        view_top_match = np.zeros_like(view_target_sum)
        view_confidence = np.zeros_like(view_target_sum)
        geometries = []
        for candidate_index, candidate in enumerate(candidate_list):
            crop, geometry = _crop_geometry(image, candidate)
            geometries.append(geometry)
            for view_index, variant in enumerate(_image_variants(crop)):
                try:
                    result = self.recognizer.classification(
                        _png_bytes(variant),
                        probability=True,
                    )
                except Exception:  # noqa: BLE001,S112 - one failed view remains zero
                    continue
                matrix, charset, text, confidence = _probability_matrix(result)
                indexes = {value: index for index, value in enumerate(charset)}
                for target_index, target in enumerate(target_list):
                    character_index = indexes.get(target)
                    if matrix is not None and character_index is not None:
                        column = matrix[:, character_index]
                        view_target_sum[target_index, candidate_index, view_index] = float(
                            np.sum(column)
                        )
                        view_target_max[target_index, candidate_index, view_index] = float(
                            np.max(column)
                        )
                    view_top_match[target_index, candidate_index, view_index] = float(
                        text == target
                    )
                    view_confidence[target_index, candidate_index, view_index] = confidence

        denominators = np.sum(view_target_sum, axis=1, keepdims=True)
        normalized = np.divide(
            view_target_sum,
            denominators,
            out=np.zeros_like(view_target_sum),
            where=denominators > 1e-12,
        )
        base_scores = np.mean(normalized, axis=2, dtype=np.float32)
        features = np.zeros(
            (len(target_list), len(candidate_list), CLICK_FEATURE_COUNT),
            dtype=np.float32,
        )
        for target_index in range(len(target_list)):
            for candidate_index in range(len(candidate_list)):
                normalized_values = normalized[target_index, candidate_index]
                vector = np.concatenate(
                    (
                        view_target_sum[target_index, candidate_index],
                        view_target_max[target_index, candidate_index],
                        normalized_values,
                        view_top_match[target_index, candidate_index],
                        view_confidence[target_index, candidate_index],
                        np.asarray(
                            [
                                float(np.mean(normalized_values)),
                                float(np.max(normalized_values)),
                                float(np.min(normalized_values)),
                                float(np.std(normalized_values)),
                            ],
                            dtype=np.float32,
                        ),
                        np.asarray(geometries[candidate_index], dtype=np.float32),
                    )
                ).astype(np.float32, copy=False)
                if vector.shape != (CLICK_FEATURE_COUNT,):
                    raise ClickEngineError("点选特征维度不一致")
                features[target_index, candidate_index] = vector
        return ClickFeatures(
            np.ascontiguousarray(features),
            np.ascontiguousarray(base_scores),
            candidate_list,
            image.width,
            image.height,
        )


def point_candidate_index(
    candidates: Sequence[ClickCandidate],
    point: dict,
    *,
    width: int,
    height: int,
) -> int | None:
    try:
        x = float(point["x"]) * width
        y = float(point["y"]) * height
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    matches = []
    for index, candidate in enumerate(candidates):
        left, top, right, bottom = candidate.box
        center_x, center_y = candidate.center
        distance = math.hypot(center_x - x, center_y - y)
        if left <= x <= right and top <= y <= bottom:
            matches.append((0, distance, index))
        else:
            allowance = max(5.0, min(right - left, bottom - top) * 0.3)
            if distance <= allowance:
                matches.append((1, distance, index))
    return min(matches)[2] if matches else None


def _top_two_assignments(scores: np.ndarray) -> list[tuple[float, tuple[int, ...]]]:
    target_count, candidate_count = scores.shape
    if not target_count or candidate_count < target_count or candidate_count > 20:
        return []
    states: dict[int, list[tuple[float, tuple[int, ...]]]] = {0: [(0.0, ())]}
    for target_index in range(target_count):
        updated: dict[int, list[tuple[float, tuple[int, ...]]]] = {}
        for mask, values in states.items():
            for total, path in values:
                for candidate_index in range(candidate_count):
                    bit = 1 << candidate_index
                    if mask & bit:
                        continue
                    new_mask = mask | bit
                    bucket = updated.setdefault(new_mask, [])
                    bucket.append(
                        (
                            total + float(scores[target_index, candidate_index]),
                            path + (candidate_index,),
                        )
                    )
        states = {
            mask: sorted(values, key=lambda item: (-item[0], item[1]))[:2]
            for mask, values in updated.items()
        }
    results = [item for values in states.values() for item in values]
    return sorted(results, key=lambda item: (-item[0], item[1]))[:2]


def best_click_assignment(scores: np.ndarray) -> tuple[int, ...] | None:
    matrix = np.asarray(scores, dtype=np.float32)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        return None
    assignments = _top_two_assignments(matrix)
    return assignments[0][1] if assignments else None


def click_assignment_diagnostics(
    scores: np.ndarray,
) -> tuple[tuple[int, ...] | None, float, float, float]:
    """Return path, minimum selected probability and ambiguity margins."""

    matrix = np.asarray(scores, dtype=np.float32)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ClickEngineError("点选排序模型输出无效")
    assignments = _top_two_assignments(matrix)
    if not assignments:
        return None, 0.0, 0.0, 0.0
    best_total, best_path = assignments[0]
    second_total = assignments[1][0] if len(assignments) > 1 else -math.inf
    probabilities = np.zeros_like(matrix)
    for row_index, row in enumerate(matrix):
        shifted = row - float(np.max(row))
        exponentials = np.exp(np.clip(shifted, -40.0, 40.0))
        probabilities[row_index] = exponentials / max(
            1e-12,
            float(np.sum(exponentials)),
        )
    selected_probabilities = []
    row_margins = []
    assigned_candidates = set(best_path)
    for target_index, candidate_index in enumerate(best_path):
        row = probabilities[target_index]
        selected = float(row[candidate_index])
        alternatives = np.asarray(
            [
                probability
                for index, probability in enumerate(row)
                if index not in assigned_candidates
            ],
            dtype=np.float32,
        )
        runner_up = float(np.max(alternatives)) if alternatives.size else 0.0
        selected_probabilities.append(selected)
        row_margins.append(selected - runner_up)
    assignment_margin = (
        float(best_total - second_total)
        if math.isfinite(second_total)
        else float("inf")
    )
    confidence = min(selected_probabilities) if selected_probabilities else 0.0
    row_margin = min(row_margins) if row_margins else 0.0
    return best_path, confidence, row_margin, assignment_margin


def resolve_click_features(
    prepared: ClickFeatures,
    scores: np.ndarray,
    *,
    min_probability: float = 0.0,
    min_row_margin: float = 0.0,
    min_assignment_margin: float = 0.0,
) -> ClickResolution:
    matrix = np.asarray(scores, dtype=np.float32)
    if matrix.shape != prepared.base_scores.shape or not np.isfinite(matrix).all():
        raise ClickEngineError("点选排序模型输出无效")
    best_path, confidence, row_margin, assignment_margin = (
        click_assignment_diagnostics(matrix)
    )
    if best_path is None:
        return ClickResolution("abstain", (), 0.0, "候选文字区域不足")
    if confidence < min_probability:
        return ClickResolution("abstain", (), confidence, "候选框置信度不足")
    if row_margin < min_row_margin:
        return ClickResolution("abstain", (), confidence, "候选框区分度不足")
    if assignment_margin < min_assignment_margin:
        return ClickResolution("abstain", (), confidence, "全局匹配区分度不足")
    points = []
    for candidate_index in best_path:
        x, y = prepared.candidates[candidate_index].center
        points.append(
            {
                "x": round(x / max(1, prepared.width), 6),
                "y": round(y / max(1, prepared.height), 6),
                "confidence": round(confidence, 6),
            }
        )
    return ClickResolution("ok", tuple(points), confidence)


__all__ = [
    "CLICK_FEATURE_COUNT",
    "CLICK_FEATURE_SCHEMA",
    "ClickCandidate",
    "ClickEngineError",
    "ClickFeatureExtractor",
    "ClickFeatures",
    "ClickResolution",
    "best_click_assignment",
    "click_assignment_diagnostics",
    "point_candidate_index",
    "resolve_click_features",
]
