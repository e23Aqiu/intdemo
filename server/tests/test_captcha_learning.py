from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import struct
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from .conftest import auth_header, changed_admin, login

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _npz_artifact() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("schema.npy", b"\x93NUMPYtest-model")
    return output.getvalue()


def _npy_array(descriptor: str, shape: tuple[int, ...], raw: bytes) -> bytes:
    header = repr(
        {
            "descr": descriptor,
            "fortran_order": False,
            "shape": shape,
        }
    )
    padding = (-((10 + len(header) + 1) % 64)) % 64
    encoded_header = (header + " " * padding + "\n").encode("latin1")
    return (
        b"\x93NUMPY\x01\x00"
        + struct.pack("<H", len(encoded_header))
        + encoded_header
        + raw
    )


def _npy_text(value: str) -> bytes:
    return _npy_array(
        f"<U{len(value)}",
        (1,),
        value.encode("utf-32-le"),
    )


def _hog_npz_artifact(
    *,
    captcha_type: str = "numeric",
    version: str = "numeric-test-1",
    maximum_classes: int = 2,
    class_counts: tuple[int, ...] | None = None,
    zero_active_weight: bool = False,
    nonzero_padding_weight: bool = False,
    nonzero_padding_bias: bool = False,
) -> bytes:
    positions = 4 if captcha_type == "numeric" else 1
    classes = maximum_classes
    counts = class_counts or (2,) * positions
    assert len(counts) == positions
    weights = bytearray(positions * classes * 1_764 * 4)
    for position, count in enumerate(counts):
        for classifier in range(count):
            if not (zero_active_weight and position == 0 and classifier == 0):
                offset = (position * classes + classifier) * 1_764 * 4
                struct.pack_into("<f", weights, offset, 1.0)
    if nonzero_padding_weight:
        assert counts[0] < classes
        offset = counts[0] * 1_764 * 4
        struct.pack_into("<f", weights, offset, 1.0)
    biases = bytearray(positions * classes * 4)
    if nonzero_padding_bias:
        assert counts[0] < classes
        struct.pack_into("<f", biases, counts[0] * 4, 1.0)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("schema_version.npy", _npy_array("<i2", (1,), b"\x02\x00"))
        archive.writestr("captcha_type.npy", _npy_text(captcha_type))
        archive.writestr("version.npy", _npy_text(version))
        archive.writestr("algorithm.npy", _npy_text("hog-linear-svm-v1"))
        archive.writestr(
            "feature_width.npy",
            _npy_array("<i4", (1,), struct.pack("<i", 1_764)),
        )
        labels = "".join(
            "12"[:count] + "".join("\x00" for _ in range(classes - count))
            for count in counts
        ).encode("utf-32-le")
        archive.writestr(
            "labels.npy",
            _npy_array("<U1", (positions, classes), labels),
        )
        archive.writestr(
            "class_counts.npy",
            _npy_array(
                "<i4",
                (positions,),
                struct.pack(f"<{positions}i", *counts),
            ),
        )
        archive.writestr(
            "weights.npy",
            _npy_array(
                "<f4",
                (positions, classes, 1_764),
                bytes(weights),
            ),
        )
        archive.writestr(
            "biases.npy",
            _npy_array(
                "<f4",
                (positions, classes),
                bytes(biases),
            ),
        )
    return output.getvalue()


def _unsafe_npz_artifact() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../model.npy", b"\x93NUMPYunsafe-model")
    return output.getvalue()


def _protobuf_varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _protobuf_field(number: int, wire_type: int, value: int | bytes) -> bytes:
    encoded = _protobuf_varint((number << 3) | wire_type)
    if wire_type == 0:
        return encoded + _protobuf_varint(int(value))
    assert wire_type == 2 and isinstance(value, bytes)
    return encoded + _protobuf_varint(len(value)) + value


def _onnx_dimension(value: int | str) -> bytes:
    return (
        _protobuf_field(1, 0, value)
        if isinstance(value, int)
        else _protobuf_field(2, 2, value.encode("utf-8"))
    )


def _onnx_value_info(name: str, shape: tuple[int | str, ...]) -> bytes:
    tensor_shape = b"".join(
        _protobuf_field(1, 2, _onnx_dimension(dimension))
        for dimension in shape
    )
    tensor_type = _protobuf_field(1, 0, 1) + _protobuf_field(2, 2, tensor_shape)
    value_type = _protobuf_field(1, 2, tensor_type)
    return _protobuf_field(1, 2, name.encode("utf-8")) + _protobuf_field(
        2,
        2,
        value_type,
    )


def _onnx_metadata(key: str, value: str) -> bytes:
    return _protobuf_field(1, 2, key.encode("utf-8")) + _protobuf_field(
        2,
        2,
        value.encode("utf-8"),
    )


def _onnx_attribute_integer(name: str, value: int) -> bytes:
    return (
        _protobuf_field(1, 2, name.encode("utf-8"))
        + _protobuf_field(3, 0, value)
        + _protobuf_field(20, 0, 2)
    )


def _onnx_node(
    inputs: tuple[str, ...],
    output: str,
    operation: str,
    *attributes: bytes,
) -> bytes:
    return (
        b"".join(
            _protobuf_field(1, 2, value.encode("utf-8")) for value in inputs
        )
        + _protobuf_field(2, 2, output.encode("utf-8"))
        + _protobuf_field(4, 2, operation.encode("utf-8"))
        + b"".join(_protobuf_field(5, 2, value) for value in attributes)
    )


def _onnx_float_tensor(
    name: str,
    shape: tuple[int, ...],
    values: tuple[float, ...],
) -> bytes:
    assert len(values) == math.prod(shape)
    return (
        b"".join(_protobuf_field(1, 0, value) for value in shape)
        + _protobuf_field(2, 0, 1)
        + _protobuf_field(8, 2, name.encode("utf-8"))
        + _protobuf_field(9, 2, struct.pack(f"<{len(values)}f", *values))
    )


def _onnx_int64_tensor(
    name: str,
    shape: tuple[int, ...],
    values: tuple[int, ...],
) -> bytes:
    assert len(values) == math.prod(shape)
    return (
        b"".join(_protobuf_field(1, 0, value) for value in shape)
        + _protobuf_field(2, 0, 7)
        + _protobuf_field(8, 2, name.encode("utf-8"))
        + _protobuf_field(9, 2, struct.pack(f"<{len(values)}q", *values))
    )


def _onnx_artifact(
    *,
    captcha_type: str = "click",
    version: str = "click-cnn-1",
    algorithm: str = "tiny-cnn-onnx-v1",
) -> bytes:
    if captcha_type == "numeric":
        input_shape: tuple[int | str, ...] = (1, 1, 48, 112)
        output_shape: tuple[int | str, ...] = (1, 4, 10)
        output_layout = "batch,position,digit"
        labels = [str(value) for value in range(10)]
    else:
        input_shape = ("N", 3, 64, 64)
        output_shape = ("N", 2)
        output_layout = "batch,class"
        labels = ["深", "比"]
    if captcha_type == "numeric":
        nodes = (
            _onnx_node(("input",), "pooled", "GlobalAveragePool"),
            _onnx_node(
                ("pooled",),
                "flat",
                "Flatten",
                _onnx_attribute_integer("axis", 1),
            ),
            _onnx_node(
                ("flat", "weight", "bias"),
                "flat_logits",
                "Gemm",
            ),
            _onnx_node(("flat_logits", "output_shape"), "logits", "Reshape"),
        )
        initializers = (
            _onnx_float_tensor("weight", (1, 40), (0.1,) * 40),
            _onnx_float_tensor("bias", (40,), (0.0,) * 40),
            _onnx_int64_tensor("output_shape", (3,), (1, 4, 10)),
        )
    else:
        nodes = (
            _onnx_node(("input",), "pooled", "GlobalAveragePool"),
            _onnx_node(
                ("pooled",),
                "flat",
                "Flatten",
                _onnx_attribute_integer("axis", 1),
            ),
            _onnx_node(("flat", "weight", "bias"), "logits", "Gemm"),
        )
        initializers = (
            _onnx_float_tensor(
                "weight",
                (3, 2),
                (0.1, 0.2, 0.3, 0.4, 0.5, 0.6),
            ),
            _onnx_float_tensor("bias", (2,), (0.0, 0.0)),
        )
    graph = (
        b"".join(_protobuf_field(1, 2, node) for node in nodes)
        + _protobuf_field(2, 2, b"intdemo-test")
        + b"".join(
            _protobuf_field(5, 2, initializer)
            for initializer in initializers
        )
        + _protobuf_field(11, 2, _onnx_value_info("input", input_shape))
        + _protobuf_field(12, 2, _onnx_value_info("logits", output_shape))
    )
    metadata = {
        "algorithm": algorithm,
        "captcha_type": captcha_type,
        "version": version,
        "training_mode": "enhanced",
        "labels_json": json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
        "output_layout": output_layout,
        "onnx_opset": "17",
    }
    opset = _protobuf_field(2, 0, 17)
    return (
        _protobuf_field(1, 0, 9)
        + _protobuf_field(7, 2, graph)
        + _protobuf_field(8, 2, opset)
        + b"".join(
            _protobuf_field(14, 2, _onnx_metadata(key, value))
            for key, value in metadata.items()
        )
    )


def _shape_only_identity_onnx() -> bytes:
    input_shape: tuple[int | str, ...] = ("N", 3, 64, 64)
    output_shape: tuple[int | str, ...] = ("N", 2)
    node = _onnx_node(("input",), "logits", "Identity")
    graph = (
        _protobuf_field(1, 2, node)
        + _protobuf_field(2, 2, b"intdemo-invalid-test")
        + _protobuf_field(11, 2, _onnx_value_info("input", input_shape))
        + _protobuf_field(12, 2, _onnx_value_info("logits", output_shape))
    )
    metadata = {
        "algorithm": "tiny-cnn-onnx-v1",
        "captcha_type": "click",
        "version": "click-shape-only",
        "training_mode": "enhanced",
        "labels_json": json.dumps(["a", "b"], separators=(",", ":")),
        "output_layout": "batch,class",
        "onnx_opset": "17",
    }
    return (
        _protobuf_field(1, 0, 9)
        + _protobuf_field(7, 2, graph)
        + _protobuf_field(8, 2, _protobuf_field(2, 0, 17))
        + b"".join(
            _protobuf_field(14, 2, _onnx_metadata(key, value))
            for key, value in metadata.items()
        )
    )


def _changed_user(client):
    bundle = login(client, "luogang", "123456", device=210)
    response = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(bundle),
        json={
            "current_password": "123456",
            "new_password": "Station!23456",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _numeric_attempt(*, success: bool, assisted: bool = False) -> dict:
    payload = {
        "captcha_type": "numeric",
        "source": "transport_numeric",
        "model_version": "ddddocr-builtin",
        "success": success,
        "assisted": assisted,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if success:
        payload.update(
            {
                "image_mime": "image/png",
                "image_base64": base64.b64encode(PNG_1X1).decode("ascii"),
                "answer": {"value": "1234"},
            }
        )
    return payload


def _click_attempt() -> dict:
    return {
        "captcha_type": "click",
        "source": "business_click",
        "model_version": "human-manual",
        "success": True,
        "assisted": True,
        "occurred_at": datetime.now(UTC).isoformat(),
        "image_mime": "image/png",
        "image_base64": base64.b64encode(PNG_1X1).decode("ascii"),
        "answer": {
            "prompt": ["京", "海"],
            "points": [
                {"x": 0.25, "y": 0.4},
                {"x": 0.75, "y": 0.6},
            ],
        },
    }


def test_authorized_sample_collection_policy_metrics_and_deduplication(client):
    admin = changed_admin(client)
    user = _changed_user(client)

    disabled = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    assert disabled.status_code == 200
    assert disabled.json()["stored"] is False

    metrics_only = client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "metrics_only"},
    )
    assert metrics_only.status_code == 200
    assert metrics_only.json()["upload_mode"] == "metrics_only"
    assert metrics_only.json()["upload_enabled"] is False
    assert metrics_only.json()["revision"] == 2

    failure = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=False),
    )
    assert failure.status_code == 200
    assert failure.json()["stored"] is True
    assert failure.json()["sample_stored"] is False

    metrics_success = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    assert metrics_success.status_code == 200
    assert metrics_success.json()["stored"] is True
    assert metrics_success.json()["sample_stored"] is False

    samples_and_metrics = client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "samples_and_metrics"},
    )
    assert samples_and_metrics.status_code == 200
    assert samples_and_metrics.json()["upload_enabled"] is True
    assert samples_and_metrics.json()["revision"] == 3

    first = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    duplicate = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["sample_stored"] is True
    assert duplicate.json()["sample_stored"] is False
    assert duplicate.json()["sample_id"] == first.json()["sample_id"]

    manual = _numeric_attempt(success=True, assisted=True)
    manual["model_version"] = "human-manual"
    manual_attempt = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=manual,
    )
    assert manual_attempt.status_code == 200

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    body = overview.json()
    assert body["dataset"]["total_count"] == 1
    assert body["dataset"]["numeric_count"] == 1
    assert body["dataset"]["total_bytes"] == len(PNG_1X1)
    assert len(body["attempts"]) == 1
    metric = body["attempts"][0]
    assert metric["attempt_count"] == 4
    assert metric["success_count"] == 3
    assert metric["success_rate"] == 3 / 4


def test_metrics_filter_selected_model_and_hide_human_manual_records(client):
    admin = changed_admin(client)
    user = _changed_user(client)
    policy = client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "metrics_only"},
    )
    assert policy.status_code == 200

    first = _numeric_attempt(success=True)
    first["model_version"] = "numeric-v1"
    second = _numeric_attempt(success=False)
    second["model_version"] = "numeric-v1"
    other = _numeric_attempt(success=True)
    other["model_version"] = "numeric-v2"
    manual = _numeric_attempt(success=True, assisted=False)
    manual["model_version"] = "human-manual"
    for payload in (first, second, other, manual):
        response = client.post(
            "/api/v1/captcha/attempts",
            headers=auth_header(user),
            json=payload,
        )
        assert response.status_code == 200, response.text
        if payload is manual:
            assert response.json()["stored"] is False

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    metrics = overview.json()["attempts"]
    assert {
        (item["captcha_type"], item["model_version"])
        for item in metrics
    } == {("numeric", "numeric-v1"), ("numeric", "numeric-v2")}

    selected = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
        params={"captcha_type": "numeric", "model_version": "numeric-v1"},
    )
    assert selected.status_code == 200
    assert selected.json()["attempts"] == [
        {
            "captcha_type": "numeric",
            "model_version": "numeric-v1",
            "attempt_count": 2,
            "success_count": 1,
            "success_rate": 0.5,
        }
    ]

    human_selected = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
        params={"model_version": "human-manual"},
    )
    assert human_selected.status_code == 200
    assert human_selected.json()["attempts"] == []


def test_dataset_export_import_and_model_activation(client):
    admin = changed_admin(client)
    user = _changed_user(client)
    client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "samples_and_metrics"},
    )
    client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_click_attempt(),
    )

    exported = client.get(
        "/api/v1/admin/ml/dataset/export",
        headers=auth_header(admin),
    )
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "manifest.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["captcha_type"] == "mixed"
        assert manifest["categories"]["numeric"]["sample_count"] == 1
        assert manifest["categories"]["click"]["sample_count"] == 1
        assert not any(
            name == "images/" or name.startswith("images/")
            for name in archive.namelist()
        )
        assert "numeric/" in archive.namelist()
        assert "click/" in archive.namelist()
        assert manifest["categories"]["numeric"]["image_directory"] == "numeric/"
        assert manifest["categories"]["click"]["image_directory"] == "click/"
        assert len(
            [
                name
                for name in archive.namelist()
                if name.startswith("numeric/") and not name.endswith("/")
            ]
        ) == 1
        assert len(
            [
                name
                for name in archive.namelist()
                if name.startswith("click/") and not name.endswith("/")
            ]
        ) == 1
        assert {
            item["captcha_type"]: item["image"].split("/", 1)[0]
            for item in manifest["samples"]
        } == {"numeric": "numeric", "click": "click"}

    for captcha_type in ("numeric", "click"):
        classified = client.get(
            "/api/v1/admin/ml/dataset/export",
            headers=auth_header(admin),
            params={"captcha_type": captcha_type},
        )
        assert classified.status_code == 200
        with zipfile.ZipFile(io.BytesIO(classified.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["captcha_type"] == captcha_type
            assert manifest["sample_count"] == 1
            assert {
                item["captcha_type"] for item in manifest["samples"]
            } == {captcha_type}

        classified_import = client.post(
            "/api/v1/admin/ml/dataset/import",
            headers={
                **auth_header(admin),
                "Content-Type": "application/zip",
            },
            content=classified.content,
        )
        assert classified_import.status_code == 200
        assert classified_import.json() == {
            "imported_count": 0,
            "duplicate_count": 1,
            "skipped_count": 0,
        }

    invalid_export = client.get(
        "/api/v1/admin/ml/dataset/export",
        headers=auth_header(admin),
        params={"captcha_type": "unsupported"},
    )
    assert invalid_export.status_code == 422

    imported = client.post(
        "/api/v1/admin/ml/dataset/import",
        headers={
            **auth_header(admin),
            "Content-Type": "application/zip",
        },
        content=exported.content,
    )
    assert imported.status_code == 200
    assert imported.json() == {
        "imported_count": 0,
        "duplicate_count": 2,
        "skipped_count": 0,
    }

    artifact = _hog_npz_artifact()
    created = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json={
            "captcha_type": "numeric",
            "version": "numeric-test-1",
            "algorithm": "hog-linear-svm-v1",
            "artifact_base64": base64.b64encode(artifact).decode("ascii"),
            "sample_count": 10,
            "test_count": 4,
            "correct_count": 3,
            "metrics": {
                "exact_code_accuracy": 0.75,
                "trainer_version": "1.1.0",
            },
        },
    )
    assert created.status_code == 200, created.text
    model = created.json()
    assert model["status"] == "candidate"
    assert model["accuracy"] == 0.75
    assert model["display_name"] == "numeric-test-1"
    assert model["algorithm"] == "hog-linear-svm-v1"
    assert model["metrics"]["training_mode"] == "standard"
    assert model["metrics"]["trainer_version"] == "1.1.0"

    renamed = client.patch(
        f"/api/v1/admin/ml/models/{model['id']}",
        headers=auth_header(admin),
        json={"display_name": "运输证数字模型"},
    )
    assert renamed.status_code == 200, renamed.text
    renamed_model = renamed.json()
    assert renamed_model["display_name"] == "运输证数字模型"
    assert renamed_model["version"] == model["version"]
    assert renamed_model["artifact_sha256"] == model["artifact_sha256"]
    assert renamed_model["artifact_size"] == model["artifact_size"]

    blank_name = client.patch(
        f"/api/v1/admin/ml/models/{model['id']}",
        headers=auth_header(admin),
        json={"display_name": "   "},
    )
    assert blank_name.status_code == 422

    forbidden = client.patch(
        f"/api/v1/admin/ml/models/{model['id']}",
        headers=auth_header(user),
        json={"display_name": "普通用户不应修改"},
    )
    assert forbidden.status_code == 403

    missing = client.patch(
        "/api/v1/admin/ml/models/00000000-0000-0000-0000-000000000000",
        headers=auth_header(admin),
        json={"display_name": "不存在"},
    )
    assert missing.status_code == 404

    activated = client.post(
        f"/api/v1/admin/ml/models/{model['id']}/activate",
        headers=auth_header(admin),
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "current"
    assert activated.json()["display_name"] == "运输证数字模型"

    policy = client.get(
        "/api/v1/captcha/policy",
        headers=auth_header(user),
    )
    assert policy.status_code == 200
    assert policy.json()["active_models"]["numeric"]["version"] == "numeric-test-1"
    assert (
        policy.json()["active_models"]["numeric"]["display_name"]
        == "运输证数字模型"
    )

    downloaded = client.get(
        "/api/v1/captcha/models/numeric/current",
        headers=auth_header(user),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == artifact
    assert downloaded.headers["x-captcha-model-version"] == "numeric-test-1"
    assert downloaded.headers["x-captcha-model-algorithm"] == "hog-linear-svm-v1"
    assert downloaded.headers["x-captcha-training-mode"] == "standard"
    assert downloaded.headers["x-captcha-trainer-version"] == "1.1.0"

    builtin = client.post(
        "/api/v1/admin/ml/models/numeric/use-builtin",
        headers=auth_header(admin),
    )
    assert builtin.status_code == 200
    assert "numeric" not in builtin.json()["active_models"]

    no_custom_model = client.get(
        "/api/v1/captcha/models/numeric/current",
        headers=auth_header(user),
    )
    assert no_custom_model.status_code == 404


def test_captcha_model_upload_accepts_enhanced_onnx_and_validates_metadata(client):
    admin = changed_admin(client)
    artifact = _onnx_artifact()
    payload = {
        "captcha_type": "click",
        "version": "click-cnn-1",
        "algorithm": "tiny-cnn-onnx-v1",
        "artifact_base64": base64.b64encode(artifact).decode("ascii"),
        "sample_count": 100,
        "test_count": 20,
        "correct_count": 17,
        "metrics": {
            "training_mode": "enhanced",
            "trainer_version": "1.1.0+uos-arm64",
        },
    }

    created = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=payload,
    )
    assert created.status_code == 200, created.text
    model = created.json()
    assert model["algorithm"] == "tiny-cnn-onnx-v1"
    assert model["metrics"]["training_mode"] == "enhanced"
    assert model["metrics"]["trainer_version"] == "1.1.0+uos-arm64"

    activated = client.post(
        f"/api/v1/admin/ml/models/{model['id']}/activate",
        headers=auth_header(admin),
    )
    assert activated.status_code == 200, activated.text
    downloaded = client.get(
        "/api/v1/captcha/models/click/current",
        headers=auth_header(admin),
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == artifact
    assert downloaded.headers["x-captcha-model-algorithm"] == "tiny-cnn-onnx-v1"
    assert downloaded.headers["x-captcha-training-mode"] == "enhanced"

    mismatched = dict(payload)
    mismatched["version"] = "click-cnn-wrong-mode"
    mismatched["metrics"] = {"training_mode": "standard"}
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=mismatched,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model_metadata"

    invalid_trainer = dict(payload)
    invalid_trainer["version"] = "click-cnn-invalid-trainer"
    invalid_trainer["metrics"] = {
        "training_mode": "enhanced",
        "trainer_version": "../trainer",
    }
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=invalid_trainer,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model_metadata"


def test_captcha_model_upload_rejects_onnx_that_cannot_run_client_contract(client):
    admin = changed_admin(client)
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json={
            "captcha_type": "click",
            "version": "click-shape-only",
            "algorithm": "tiny-cnn-onnx-v1",
            "artifact_base64": base64.b64encode(
                _shape_only_identity_onnx()
            ).decode("ascii"),
            "sample_count": 20,
            "test_count": 4,
            "correct_count": 3,
            "metrics": {"training_mode": "enhanced"},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"


def test_captcha_model_upload_rejects_retired_algorithm_and_invalid_artifacts(client):
    admin = changed_admin(client)
    base_payload = {
        "captcha_type": "numeric",
        "version": "numeric-invalid-1",
        "algorithm": "knn-pixels-v1",
        "artifact_base64": base64.b64encode(_npz_artifact()).decode("ascii"),
        "sample_count": 10,
        "test_count": 4,
        "correct_count": 3,
        "metrics": {},
    }

    retired = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=base_payload,
    )
    assert retired.status_code == 422
    assert retired.json()["code"] == "unsupported_captcha_model_algorithm"

    invalid_npz = dict(base_payload)
    invalid_npz["version"] = "numeric-invalid-npz"
    invalid_npz["algorithm"] = "hog-linear-svm-v1"
    invalid_npz["artifact_base64"] = base64.b64encode(
        b"PK\x03\x04not-really-an-npz"
    ).decode("ascii")
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=invalid_npz,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"

    unsafe_npz = dict(invalid_npz)
    unsafe_npz["version"] = "numeric-unsafe-npz"
    unsafe_npz["artifact_base64"] = base64.b64encode(
        _unsafe_npz_artifact()
    ).decode("ascii")
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=unsafe_npz,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"

    invalid_onnx = dict(base_payload)
    invalid_onnx["version"] = "numeric-invalid-onnx"
    invalid_onnx["algorithm"] = "tiny-cnn-onnx-v1"
    invalid_onnx["artifact_base64"] = base64.b64encode(b"not-onnx").decode("ascii")
    response = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json=invalid_onnx,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"


def test_captcha_model_upload_binds_declared_identity_and_full_payload(client):
    admin = changed_admin(client)

    def upload(artifact: bytes, *, version: str, algorithm: str = "hog-linear-svm-v1"):
        return client.post(
            "/api/v1/admin/ml/models",
            headers=auth_header(admin),
            json={
                "captcha_type": "numeric",
                "version": version,
                "algorithm": algorithm,
                "artifact_base64": base64.b64encode(artifact).decode("ascii"),
                "sample_count": 10,
                "test_count": 4,
                "correct_count": 3,
                "metrics": {},
            },
        )

    mismatched_version = upload(
        _hog_npz_artifact(version="numeric-internal"),
        version="numeric-declared",
    )
    assert mismatched_version.status_code == 422
    assert mismatched_version.json()["code"] == "invalid_captcha_model"

    truncated = io.BytesIO()
    with zipfile.ZipFile(
        io.BytesIO(_hog_npz_artifact(version="numeric-truncated"))
    ) as source, zipfile.ZipFile(
        truncated,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as target:
        for item in source.infolist():
            payload = source.read(item)
            if item.filename == "weights.npy":
                payload = payload[:-4]
            target.writestr(item.filename, payload)
    response = upload(truncated.getvalue(), version="numeric-truncated")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"

    zero_classifier = upload(
        _hog_npz_artifact(
            version="numeric-zero-classifier",
            zero_active_weight=True,
        ),
        version="numeric-zero-classifier",
    )
    assert zero_classifier.status_code == 422
    assert zero_classifier.json()["code"] == "invalid_captcha_model"

    for version, artifact in (
        (
            "numeric-padding-weight",
            _hog_npz_artifact(
                version="numeric-padding-weight",
                maximum_classes=3,
                nonzero_padding_weight=True,
            ),
        ),
        (
            "numeric-padding-bias",
            _hog_npz_artifact(
                version="numeric-padding-bias",
                maximum_classes=3,
                nonzero_padding_bias=True,
            ),
        ),
    ):
        response = upload(artifact, version=version)
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_captcha_model"

    fake_envelope = b"\x08\x09\x3a\x03\x12\x01g"
    response = upload(
        fake_envelope,
        version="numeric-fake-onnx",
        algorithm="tiny-cnn-onnx-v1",
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_captcha_model"


def test_captcha_model_revalidates_stored_artifact_before_activation_and_download(client):
    from app.database import SessionLocal
    from app.models import CaptchaModel

    admin = changed_admin(client)
    artifact = _hog_npz_artifact(version="numeric-tampered")
    created = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json={
            "captcha_type": "numeric",
            "version": "numeric-tampered",
            "algorithm": "hog-linear-svm-v1",
            "artifact_base64": base64.b64encode(artifact).decode("ascii"),
            "sample_count": 10,
            "test_count": 4,
            "correct_count": 3,
            "metrics": {},
        },
    )
    assert created.status_code == 200, created.text
    model_id = uuid.UUID(created.json()["id"])
    with SessionLocal() as db:
        model = db.get(CaptchaModel, model_id)
        assert model is not None
        model.artifact_sha256 = "0" * 64
        db.commit()

    activate = client.post(
        f"/api/v1/admin/ml/models/{model_id}/activate",
        headers=auth_header(admin),
    )
    assert activate.status_code == 409
    assert activate.json()["code"] == "invalid_captcha_model"

    with SessionLocal() as db:
        model = db.get(CaptchaModel, model_id)
        assert model is not None
        model.status = "current"
        db.commit()
    policy = client.get("/api/v1/captcha/policy", headers=auth_header(admin))
    assert (
        policy.json()["active_models"]["numeric"]["version"]
        == "numeric-tampered"
    )
    download = client.get(
        "/api/v1/captcha/models/numeric/current",
        headers=auth_header(admin),
    )
    assert download.status_code == 404


def test_legacy_knn_history_is_hidden_and_cannot_be_reactivated(client):
    from app.database import SessionLocal
    from app.models import CaptchaModel

    admin = changed_admin(client)
    legacy_id = uuid.uuid4()
    artifact = _npz_artifact()
    with SessionLocal() as db:
        db.add(
            CaptchaModel(
                id=legacy_id,
                captcha_type="numeric",
                version="numeric-knn-legacy",
                display_name="Legacy KNN",
                algorithm="knn-pixels-v1",
                status="current",
                artifact_sha256="0" * 64,
                artifact_size=len(artifact),
                artifact=artifact,
                sample_count=10,
                test_count=4,
                correct_count=3,
                accuracy=0.75,
                metrics={"historical": True},
            )
        )
        db.commit()

    policy = client.get(
        "/api/v1/captcha/policy",
        headers=auth_header(admin),
    )
    assert policy.status_code == 200
    assert "numeric" not in policy.json()["active_models"]

    download = client.get(
        "/api/v1/captcha/models/numeric/current",
        headers=auth_header(admin),
    )
    assert download.status_code == 404

    activate = client.post(
        f"/api/v1/admin/ml/models/{legacy_id}/activate",
        headers=auth_header(admin),
    )
    assert activate.status_code == 409
    assert activate.json()["code"] == "captcha_model_algorithm_retired"

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    historical = next(
        item for item in overview.json()["models"] if item["id"] == str(legacy_id)
    )
    assert historical["algorithm"] == "knn-pixels-v1"
    assert historical["metrics"] == {"historical": True}


def test_retire_knn_migration_archives_only_active_knn_and_is_idempotent(tmp_path):
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0008_retire_knn_captcha_models.py"
    )
    spec = importlib.util.spec_from_file_location("retire_knn_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE captcha_models (
                    id TEXT PRIMARY KEY,
                    algorithm TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact BLOB,
                    metrics JSON
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO captcha_models (id, algorithm, status, artifact, metrics)
                VALUES
                    ('legacy', 'knn-pixels-v1', 'current', :artifact, :metrics),
                    ('standard', 'hog-linear-svm-v1', 'current', :artifact, :metrics)
                """
            ),
            {"artifact": b"historical-artifact", "metrics": '{"kept":true}'},
        )
        original_get_bind = migration.op.get_bind
        migration.op.get_bind = lambda: connection
        try:
            migration.upgrade()
            migration.upgrade()
        finally:
            migration.op.get_bind = original_get_bind
        rows = {
            row.id: row
            for row in connection.execute(
                text(
                    "SELECT id, algorithm, status, artifact, metrics "
                    "FROM captcha_models"
                )
            )
        }
    engine.dispose()

    assert rows["legacy"].status == "archived"
    assert rows["standard"].status == "current"
    assert rows["legacy"].artifact == b"historical-artifact"
    assert rows["legacy"].metrics == '{"kept":true}'


def test_empty_dataset_export_keeps_both_category_directories(client):
    admin = changed_admin(client)

    exported = client.get(
        "/api/v1/admin/ml/dataset/export",
        headers=auth_header(admin),
    )

    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "numeric/" in archive.namelist()
        assert "click/" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["sample_count"] == 0
    assert manifest["categories"]["numeric"]["sample_count"] == 0
    assert manifest["categories"]["click"]["sample_count"] == 0


def test_admin_can_list_and_delete_selected_captcha_samples(client):
    admin = changed_admin(client)
    user = _changed_user(client)
    client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "samples_and_metrics"},
    )
    numeric = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_numeric_attempt(success=True),
    )
    click = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_click_attempt(),
    )
    assert numeric.status_code == click.status_code == 200

    listed = client.get(
        "/api/v1/admin/ml/samples",
        headers=auth_header(admin),
        params={"limit": 1, "offset": 0},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 2
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["answer"]

    numeric_only = client.get(
        "/api/v1/admin/ml/samples",
        headers=auth_header(admin),
        params={"captcha_type": "numeric"},
    )
    assert numeric_only.status_code == 200
    assert numeric_only.json()["total"] == 1
    numeric_id = numeric_only.json()["items"][0]["id"]

    deleted = client.request(
        "DELETE",
        "/api/v1/admin/ml/samples",
        headers=auth_header(admin),
        json={"sample_ids": [numeric_id]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted_count": 1, "missing_count": 0}

    deleted_again = client.request(
        "DELETE",
        "/api/v1/admin/ml/samples",
        headers=auth_header(admin),
        json={"sample_ids": [numeric_id]},
    )
    assert deleted_again.status_code == 200
    assert deleted_again.json() == {"deleted_count": 0, "missing_count": 1}

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    assert overview.json()["dataset"]["total_count"] == 1
    assert overview.json()["dataset"]["numeric_count"] == 0
    assert overview.json()["dataset"]["click_count"] == 1


def test_sample_listing_filters_model_and_image_can_be_read(client):
    admin = changed_admin(client)
    user = _changed_user(client)
    enabled = client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "samples_and_metrics"},
    )
    assert enabled.status_code == 200
    payload = _numeric_attempt(success=True)
    payload["model_version"] = "numeric-sample-v1"
    created = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=payload,
    )
    assert created.status_code == 200, created.text
    sample_id = created.json()["sample_id"]

    listed = client.get(
        "/api/v1/admin/ml/samples",
        headers=auth_header(admin),
        params={"captcha_type": "numeric", "model_version": "numeric-sample-v1"},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["image_mime"] == "image/png"
    assert body["items"][0]["image_available"] is True

    image = client.get(
        f"/api/v1/admin/ml/samples/{sample_id}/image",
        headers=auth_header(admin),
    )
    assert image.status_code == 200, image.text
    assert image.headers["content-type"].startswith("image/png")
    assert image.content == PNG_1X1


def test_dataset_import_accepts_legacy_images_directory(client):
    admin = changed_admin(client)
    manifest = {
        "schema_version": 1,
        "captcha_type": "numeric",
        "sample_count": 1,
        "samples": [
            {
                "id": "legacy-id",
                "captcha_type": "numeric",
                "source": "transport_numeric",
                "image": "images/numeric/legacy.png",
                "image_mime": "image/png",
                "answer": {"value": "9876"},
                "model_version": "legacy-model",
                "captured_at": datetime.now(UTC).isoformat(),
                "fingerprint": "legacy-fingerprint",
            }
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("images/numeric/legacy.png", PNG_1X1)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest).encode("utf-8"),
        )
    imported = client.post(
        "/api/v1/admin/ml/dataset/import",
        headers={
            **auth_header(admin),
            "Content-Type": "application/zip",
        },
        content=output.getvalue(),
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {
        "imported_count": 1,
        "duplicate_count": 0,
        "skipped_count": 0,
    }


def test_dataset_import_normalizes_windows_members_and_rejects_traversal(client):
    admin = changed_admin(client)
    manifest = {
        "schema_version": 1,
        "captcha_type": "numeric",
        "samples": [
            {
                "captcha_type": "numeric",
                "source": "transport_numeric",
                "image": ".\\images\\numeric\\windows.png",
                "answer": {"value": "1357"},
                "model_version": "windows-model",
                "captured_at": datetime.now(UTC).isoformat(),
            }
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".\\manifest.json",
            b"\xef\xbb\xbf" + json.dumps(manifest).encode("utf-8"),
        )
        archive.writestr("images\\numeric\\windows.png", PNG_1X1)
    imported = client.post(
        "/api/v1/admin/ml/dataset/import",
        headers={
            **auth_header(admin),
            "Content-Type": "application/zip",
        },
        content=output.getvalue(),
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {
        "imported_count": 1,
        "duplicate_count": 0,
        "skipped_count": 0,
    }

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "samples": [],
                }
            ),
        )
        archive.writestr(r"..\outside.png", PNG_1X1)
    rejected = client.post(
        "/api/v1/admin/ml/dataset/import",
        headers={
            **auth_header(admin),
            "Content-Type": "application/zip",
        },
        content=unsafe.getvalue(),
    )
    assert rejected.status_code == 422


def test_failed_attempt_rejects_image_and_non_admin_cannot_manage_dataset(client):
    user = _changed_user(client)
    invalid = _numeric_attempt(success=False)
    invalid.update(
        {
            "image_mime": "image/png",
            "image_base64": base64.b64encode(PNG_1X1).decode("ascii"),
            "answer": {"value": "1234"},
        }
    )
    response = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=invalid,
    )
    assert response.status_code == 422

    wrong_source = _numeric_attempt(success=False)
    wrong_source["source"] = "business_click"
    response = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=wrong_source,
    )
    assert response.status_code == 422

    forbidden = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(user),
    )
    assert forbidden.status_code == 403


def test_click_samples_require_matching_normalized_coordinates(client):
    admin = changed_admin(client)
    user = _changed_user(client)
    client.patch(
        "/api/v1/admin/ml/policy",
        headers=auth_header(admin),
        json={"upload_mode": "samples_and_metrics"},
    )

    accepted = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=_click_attempt(),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["sample_stored"] is True

    invalid = _click_attempt()
    invalid["answer"]["points"][0]["x"] = 1.1
    rejected = client.post(
        "/api/v1/captcha/attempts",
        headers=auth_header(user),
        json=invalid,
    )
    assert rejected.status_code == 422

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    assert overview.json()["dataset"]["click_count"] == 1
