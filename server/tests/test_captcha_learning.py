from __future__ import annotations

import base64
import io
import json
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
        assert "数字验证码/" in archive.namelist()
        assert "文字点选验证码/" in archive.namelist()
        assert len(
            [
                name
                for name in archive.namelist()
                if name.startswith("数字验证码/") and not name.endswith("/")
            ]
        ) == 1
        assert len(
            [
                name
                for name in archive.namelist()
                if name.startswith("文字点选验证码/") and not name.endswith("/")
            ]
        ) == 1

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
