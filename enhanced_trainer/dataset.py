"""Safe reader and image preparation for exported CAPTCHA dataset ZIPs."""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from integrated_client.captcha_tensors import (
    click_candidate_bbox,
    cnn_click_tensor,
)

DATASET_SCHEMA_VERSION = 1
MAX_DATASET_BYTES = 100 * 1024 * 1024
MAX_DATASET_FILES = 50_003
MAX_SAMPLE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_TRAINING_SAMPLES = 5_000
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptchaSample:
    captcha_type: str
    image: bytes
    answer: dict
    fingerprint: str


def _normalized_member(name: str, *, directory: bool = False) -> str:
    value = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/")
    if (
        not value
        or "\x00" in value
        or value.startswith("/")
        or _DRIVE_PREFIX.match(value)
    ):
        raise DatasetError("数据集包含绝对路径或空路径")
    if directory and value.endswith("/"):
        value = value[:-1]
    parts = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise DatasetError("数据集包含路径穿越")
        parts.append(part)
    if not parts:
        raise DatasetError("数据集包含无效路径")
    return str(PurePosixPath(*parts))


def _is_regular(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    return kind in {0, stat.S_IFREG}


def load_dataset(path: Path | str, captcha_type: str) -> list[CaptchaSample]:
    """Read raw image bytes and labels from a schema-v1 exported dataset."""

    selected_type = str(captcha_type or "").casefold()
    if selected_type not in {"numeric", "click"}:
        raise DatasetError("验证码类型必须是 numeric 或 click")
    archive_path = Path(path).expanduser().resolve()
    try:
        size = archive_path.stat().st_size
    except OSError as exc:
        raise DatasetError(f"无法读取验证码数据集：{exc}") from exc
    if not 0 < size <= MAX_DATASET_BYTES:
        raise DatasetError("验证码数据集为空或大小超限")
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise DatasetError("验证码数据集不是有效 ZIP") from exc
    try:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DATASET_FILES:
            raise DatasetError("验证码数据集文件数量超限")
        members = {}
        total_size = 0
        for info in infos:
            if info.is_dir():
                _normalized_member(info.filename, directory=True)
                continue
            if (
                not _is_regular(info)
                or info.flag_bits & 0x1
                or info.compress_type
                not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise DatasetError("验证码数据集包含特殊文件或不支持的压缩")
            name = _normalized_member(info.filename)
            identity = name.casefold()
            if identity in members:
                raise DatasetError("验证码数据集包含重复路径")
            members[identity] = (name, info)
            total_size += info.file_size
            if total_size > MAX_DATASET_BYTES:
                raise DatasetError("验证码数据集解压后大小超限")
        try:
            manifest_info = members["manifest.json"][1]
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
            raise DatasetError("验证码数据集 manifest.json 无效") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != DATASET_SCHEMA_VERSION
            or not isinstance(manifest.get("samples"), list)
            or len(manifest["samples"]) > MAX_DATASET_FILES - 1
        ):
            raise DatasetError("验证码数据集清单版本或样本列表无效")
        result = []
        fingerprints = set()
        for item in manifest["samples"]:
            if not isinstance(item, dict) or item.get("captcha_type") != selected_type:
                continue
            answer = item.get("answer")
            if not isinstance(answer, dict):
                continue
            if selected_type == "numeric":
                value = str(answer.get("value") or "").strip()
                if not re.fullmatch(r"[0-9]{4}", value):
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
                    continue
                normalized_prompt = []
                normalized_points = []
                valid = True
                for label, point in zip(prompt, points):
                    label = str(label or "").strip()
                    if (
                        not label
                        or len(label) > 4
                        or not isinstance(point, dict)
                        or set(point) != {"x", "y"}
                    ):
                        valid = False
                        break
                    try:
                        x = float(point["x"])
                        y = float(point["y"])
                    except (TypeError, ValueError):
                        valid = False
                        break
                    if not math.isfinite(x) or not math.isfinite(y):
                        valid = False
                        break
                    if not 0 <= x <= 1 or not 0 <= y <= 1:
                        valid = False
                        break
                    normalized_prompt.append(label)
                    normalized_points.append({"x": x, "y": y})
                if not valid:
                    continue
                normalized_answer = {
                    "prompt": normalized_prompt,
                    "points": normalized_points,
                }
            try:
                image_name = _normalized_member(str(item.get("image") or ""))
                image_info = members[image_name.casefold()][1]
            except (DatasetError, KeyError):
                continue
            if not 0 < image_info.file_size <= MAX_SAMPLE_BYTES:
                continue
            try:
                image_bytes = archive.read(image_info)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        continue
                    image.verify()
            except Exception:  # noqa: BLE001, S112 - invalid samples are skipped
                continue
            fingerprint = str(item.get("fingerprint") or "").strip().casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                digest = hashlib.sha256()
                digest.update(selected_type.encode("ascii"))
                digest.update(b"\0")
                digest.update(image_bytes)
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
                continue
            fingerprints.add(fingerprint)
            result.append(
                CaptchaSample(
                    selected_type,
                    image_bytes,
                    normalized_answer,
                    fingerprint,
                )
            )
        return sorted(result, key=lambda item: item.fingerprint)[:MAX_TRAINING_SAMPLES]
    finally:
        archive.close()


def split_samples(
    samples: list[CaptchaSample],
) -> tuple[list[CaptchaSample], list[CaptchaSample]]:
    test = [sample for index, sample in enumerate(samples) if index % 5 == 0]
    train = [sample for index, sample in enumerate(samples) if index % 5 != 0]
    if not train or not test:
        raise DatasetError("验证码数据集无法划分训练集和测试集")
    return train, test


def open_image(sample: CaptchaSample, mode: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(sample.image)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise DatasetError("验证码图片像素数量超限")
            return source.convert(mode)
    except DatasetError:
        raise
    except Exception as exc:
        raise DatasetError(f"无法读取验证码图片：{exc}") from exc


def numeric_array(
    sample: CaptchaSample,
    *,
    augment: bool,
    random_state: random.Random,
) -> np.ndarray:
    """Prepare the entire four-digit image without character segmentation."""

    image = open_image(sample, "L")
    if augment:
        image = _augment_common(image, random_state)
        # The production CAPTCHA deliberately truncates its final glyph.
        # Randomly cover/crop only the right edge, never a guessed digit cell.
        if random_state.random() < 0.65:
            width, _height = image.size
            hidden = random_state.randint(1, max(1, round(width * 0.18)))
            background = int(np.asarray(image)[:, : max(1, width // 12)].mean())
            pixels = np.asarray(image).copy()
            pixels[:, width - hidden :] = background
            image = Image.fromarray(pixels, mode="L")
    # Match the desktop inference adapter exactly: it resizes the full raw
    # image to 112x48 and never segments it into four cells.
    image = image.resize((112, 48), Image.Resampling.LANCZOS)
    return np.asarray(image, dtype=np.float32)[None, :, :] / 255.0


def _click_crop(sample: CaptchaSample, point: dict) -> Image.Image:
    image = open_image(sample, "RGB")
    bbox = click_candidate_bbox(image.size, point)
    return image.crop(
        (
            max(0, bbox[0] - 3),
            max(0, bbox[1] - 3),
            min(image.width, bbox[2] + 3),
            min(image.height, bbox[3] + 3),
        )
    )


def click_array(
    sample: CaptchaSample,
    point: dict,
    *,
    augment: bool,
    random_state: random.Random,
) -> np.ndarray:
    image = open_image(sample, "RGB")
    bbox = click_candidate_bbox(image.size, point)
    if augment:
        crop = _click_crop(sample, point)
        crop = _augment_common(crop, random_state)
        crop = crop.rotate(
            random_state.uniform(-180, 180),
            resample=Image.Resampling.BICUBIC,
            expand=False,
        )
        channels = list(crop.split())
        random_state.shuffle(channels)
        crop = Image.merge("RGB", channels)
        return cnn_click_tensor(crop, (3, 3, crop.width - 3, crop.height - 3))
    return cnn_click_tensor(image, bbox)


def _augment_common(image: Image.Image, random_state: random.Random) -> Image.Image:
    image = image.rotate(
        random_state.uniform(-6, 6),
        resample=Image.Resampling.BICUBIC,
        expand=False,
    )
    image = ImageEnhance.Contrast(image).enhance(random_state.uniform(0.75, 1.3))
    image = ImageEnhance.Brightness(image).enhance(random_state.uniform(0.8, 1.2))
    if random_state.random() < 0.25:
        image = image.filter(ImageFilter.GaussianBlur(random_state.uniform(0.2, 0.8)))
    return image
