"""Supported CAPTCHA model algorithms and their upload contracts.

The legacy KNN identifier remains here so historical database rows can be
recognized and retained for audit, but it is intentionally not part of the
supported upload/activation allowlist.
"""

from __future__ import annotations

from collections.abc import Collection

LEGACY_KNN_PIXELS_ALGORITHM = "knn-pixels-v1"
HOG_LINEAR_SVM_ALGORITHM = "hog-linear-svm-v1"
TINY_CNN_ONNX_ALGORITHM = "tiny-cnn-onnx-v1"

STANDARD_TRAINING_MODE = "standard"
ENHANCED_TRAINING_MODE = "enhanced"

MAX_CAPTCHA_MODEL_BYTES = 20 * 1024 * 1024

SUPPORTED_CAPTCHA_MODEL_ALGORITHMS: dict[str, frozenset[str]] = {
    "numeric": frozenset(
        {
            HOG_LINEAR_SVM_ALGORITHM,
            TINY_CNN_ONNX_ALGORITHM,
        }
    ),
    "click": frozenset(
        {
            HOG_LINEAR_SVM_ALGORITHM,
            TINY_CNN_ONNX_ALGORITHM,
        }
    ),
}

TRAINING_MODE_BY_ALGORITHM = {
    HOG_LINEAR_SVM_ALGORITHM: STANDARD_TRAINING_MODE,
    TINY_CNN_ONNX_ALGORITHM: ENHANCED_TRAINING_MODE,
}

ARTIFACT_FORMAT_BY_ALGORITHM = {
    HOG_LINEAR_SVM_ALGORITHM: "npz",
    TINY_CNN_ONNX_ALGORITHM: "onnx",
}

MAX_ARTIFACT_BYTES_BY_ALGORITHM = {
    HOG_LINEAR_SVM_ALGORITHM: MAX_CAPTCHA_MODEL_BYTES,
    TINY_CNN_ONNX_ALGORITHM: MAX_CAPTCHA_MODEL_BYTES,
}


def supported_algorithms(captcha_type: str) -> Collection[str]:
    """Return the allowlisted algorithms for a CAPTCHA category."""

    return SUPPORTED_CAPTCHA_MODEL_ALGORITHMS.get(captcha_type, frozenset())


def is_supported_model(captcha_type: str, algorithm: str) -> bool:
    """Return whether this category/algorithm combination may be used."""

    return algorithm in supported_algorithms(captcha_type)

