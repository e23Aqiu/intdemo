"""Shared image-to-tensor preprocessing for enhanced CAPTCHA models."""

from __future__ import annotations

import numpy as np
from PIL import Image

CLICK_TENSOR_SIZE = 64
CLICK_BBOX_PADDING = 3


def click_candidate_bbox(
    image_size: tuple[int, int],
    point: dict,
) -> tuple[int, int, int, int]:
    """Approximate the detector box around a normalized training point."""

    width, height = image_size
    center_x = float(point["x"]) * width
    center_y = float(point["y"]) * height
    radius = max(12, int(min(width, height) * 0.11))
    return (
        max(0, int(center_x - radius) + CLICK_BBOX_PADDING),
        max(0, int(center_y - radius) + CLICK_BBOX_PADDING),
        min(width, int(center_x + radius) - CLICK_BBOX_PADDING),
        min(height, int(center_y + radius) - CLICK_BBOX_PADDING),
    )


def cnn_click_tensor(image: Image.Image, bbox) -> np.ndarray:
    """Apply the production crop, resize, padding and channel layout."""

    x1, y1, x2, y2 = (int(value) for value in bbox)
    crop = image.crop(
        (
            max(0, x1 - CLICK_BBOX_PADDING),
            max(0, y1 - CLICK_BBOX_PADDING),
            min(image.width, x2 + CLICK_BBOX_PADDING),
            min(image.height, y2 + CLICK_BBOX_PADDING),
        )
    ).convert("RGB")
    source = np.asarray(crop, dtype=np.uint8)
    source_height, source_width = source.shape[:2]
    if source_height <= 0 or source_width <= 0:
        raise ValueError("click candidate bbox is empty")
    scale = min(CLICK_TENSOR_SIZE / source_width, CLICK_TENSOR_SIZE / source_height)
    target_width = max(
        1,
        min(CLICK_TENSOR_SIZE, int(round(source_width * scale))),
    )
    target_height = max(
        1,
        min(CLICK_TENSOR_SIZE, int(round(source_height * scale))),
    )
    resampling = (
        Image.Resampling.BOX if scale < 1.0 else Image.Resampling.BICUBIC
    )
    resized = crop.resize((target_width, target_height), resampling)
    canvas = Image.new("RGB", (CLICK_TENSOR_SIZE, CLICK_TENSOR_SIZE))
    canvas.paste(
        resized,
        (
            (CLICK_TENSOR_SIZE - target_width) // 2,
            (CLICK_TENSOR_SIZE - target_height) // 2,
        ),
    )
    return np.transpose(
        np.asarray(canvas, dtype=np.float32) / 255.0,
        (2, 0, 1),
    )


__all__ = ["click_candidate_bbox", "cnn_click_tensor"]
