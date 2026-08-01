from __future__ import annotations

import base64
import io
import zipfile
from datetime import UTC, datetime

from .conftest import auth_header, changed_admin, login

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
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


def _numeric_attempt(*, success: bool) -> dict:
    payload = {
        "captcha_type": "numeric",
        "source": "transport_numeric",
        "model_version": "ddddocr-builtin",
        "success": success,
        "assisted": True,
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

    overview = client.get(
        "/api/v1/admin/ml/overview",
        headers=auth_header(admin),
    )
    assert overview.status_code == 200
    body = overview.json()
    assert body["dataset"]["total_count"] == 1
    assert body["dataset"]["numeric_count"] == 1
    assert body["dataset"]["total_bytes"] == len(PNG_1X1)
    metric = body["attempts"][0]
    assert metric["attempt_count"] == 4
    assert metric["success_count"] == 3
    assert metric["success_rate"] == 3 / 4


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

    exported = client.get(
        "/api/v1/admin/ml/dataset/export",
        headers=auth_header(admin),
    )
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert len([name for name in archive.namelist() if name.startswith("images/")]) == 1

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
        "duplicate_count": 1,
        "skipped_count": 0,
    }

    artifact = b"PK\x03\x04authorized-model"
    created = client.post(
        "/api/v1/admin/ml/models",
        headers=auth_header(admin),
        json={
            "captcha_type": "numeric",
            "version": "numeric-test-1",
            "algorithm": "knn-pixels-v1",
            "artifact_base64": base64.b64encode(artifact).decode("ascii"),
            "sample_count": 10,
            "test_count": 4,
            "correct_count": 3,
            "metrics": {"exact_code_accuracy": 0.75},
        },
    )
    assert created.status_code == 200, created.text
    model = created.json()
    assert model["status"] == "candidate"
    assert model["accuracy"] == 0.75

    activated = client.post(
        f"/api/v1/admin/ml/models/{model['id']}/activate",
        headers=auth_header(admin),
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "current"

    policy = client.get(
        "/api/v1/captcha/policy",
        headers=auth_header(user),
    )
    assert policy.status_code == 200
    assert policy.json()["active_models"]["numeric"]["version"] == "numeric-test-1"

    downloaded = client.get(
        "/api/v1/captcha/models/numeric/current",
        headers=auth_header(user),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == artifact
    assert downloaded.headers["x-captcha-model-version"] == "numeric-test-1"

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
