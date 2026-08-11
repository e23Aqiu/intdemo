from __future__ import annotations

import json

import pytest

from app.config import get_settings


@pytest.fixture
def update_manifest_file():
    path = get_settings().updates_dir / "test.json"
    payload = {
        "schema_version": 1,
        "channel": "test",
        "version": "0.2.5",
        "installer_path": "/updates/files/IntDemoOnline-Setup-0.2.5.exe",
        "sha256": "a" * 64,
        "size": 500_000_000,
        "full": {
            "installer_path": "/updates/files/IntDemoOnline-Setup-0.2.5.exe",
            "sha256": "a" * 64,
            "size": 500_000_000,
        },
        "deltas": [
            {
                "from_version": "0.2.4",
                "installer_path": (
                    "/updates/files/IntDemoOnline-Patch-0.2.4-to-0.2.5.exe"
                ),
                "sha256": "b" * 64,
                "size": 14_000_000,
            }
        ],
        "notes": "版本识别测试",
        "platforms": {
            "windows-x86_64": {
                "full": {
                    "installer_path": (
                        "/updates/files/IntDemoOnline-Setup-0.2.5.exe"
                    ),
                    "sha256": "a" * 64,
                    "size": 500_000_000,
                },
                "deltas": [
                    {
                        "from_version": "0.2.4",
                        "installer_path": (
                            "/updates/files/IntDemoOnline-Patch-0.2.4-to-0.2.5.exe"
                        ),
                        "sha256": "b" * 64,
                        "size": 14_000_000,
                    }
                ],
            },
            "linux-aarch64": {
                "full": {
                    "installer_path": (
                        "/updates/files/IntDemo-UOS-arm64-0.2.5.deb"
                    ),
                    "sha256": "c" * 64,
                    "size": 620_000_000,
                }
            },
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        yield path, payload
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("version", ["0.1.0", "0.2.2", "0.2.3", "0.2.5"])
def test_nonmatching_versions_receive_full_installer(
    client,
    update_manifest_file,
    version,
):
    _path, canonical = update_manifest_file
    response = client.get(
        "/updates/test.json",
        headers={"User-Agent": f"IntDemoUpdater/{version}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["installer_path"] == canonical["full"]["installer_path"]
    assert payload["sha256"] == canonical["full"]["sha256"]
    assert payload["size"] == canonical["full"]["size"]
    assert payload["primary_kind"] == "full"
    assert "primary_from_version" not in payload
    assert response.headers["x-intdemo-update-package"] == "full"
    assert response.headers["x-intdemo-client-version"] == version
    assert response.headers["x-intdemo-platform"] == "windows-x86_64"


def test_uos_arm64_client_receives_deb_package(client, update_manifest_file):
    response = client.get(
        "/updates/test.json",
        headers={
            "X-IntDemo-Version": "0.2.4",
            "X-IntDemo-Platform": "linux-aarch64",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_platform"] == "linux-aarch64"
    assert payload["installer_path"].endswith(".deb")
    assert payload["primary_kind"] == "full"
    assert response.headers["x-intdemo-platform"] == "linux-aarch64"


def test_exact_version_receives_legacy_compatible_delta(client, update_manifest_file):
    _path, canonical = update_manifest_file
    response = client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/0.2.4"},
    )

    assert response.status_code == 200
    payload = response.json()
    delta = canonical["deltas"][0]
    assert payload["installer_path"] == delta["installer_path"]
    assert payload["sha256"] == delta["sha256"]
    assert payload["size"] == delta["size"]
    assert payload["primary_kind"] == "delta"
    assert payload["primary_from_version"] == "0.2.4"
    assert payload["full"] == canonical["full"]
    assert response.headers["x-intdemo-update-package"] == "delta"


def test_explicit_version_header_takes_precedence(client, update_manifest_file):
    response = client.get(
        "/updates/test.json",
        headers={
            "User-Agent": "IntDemoUpdater/0.2.3",
            "X-IntDemo-Version": "0.2.4",
        },
    )

    assert response.status_code == 200
    assert response.json()["primary_kind"] == "delta"
    assert response.headers["x-intdemo-client-version"] == "0.2.4"


def test_missing_or_invalid_version_defaults_to_full(client, update_manifest_file):
    missing = client.get("/updates/test.json")
    invalid = client.get(
        "/updates/test.json",
        headers={
            "User-Agent": "IntDemoUpdater/not-a-version",
            "X-IntDemo-Version": "../../0.2.4",
        },
    )

    assert missing.status_code == 200
    assert invalid.status_code == 200
    assert missing.json()["primary_kind"] == "full"
    assert invalid.json()["primary_kind"] == "full"
    assert "x-intdemo-client-version" not in invalid.headers


def test_manifest_source_is_not_modified(client, update_manifest_file):
    path, canonical = update_manifest_file

    client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/0.2.4"},
    )

    assert json.loads(path.read_text(encoding="utf-8")) == canonical
    assert client.get("/updates/preview.json").status_code == 404


def test_paused_manifest_suppresses_updates_for_old_and_headerless_clients(
    client,
    update_manifest_file,
):
    path, _canonical = update_manifest_file
    marker = {
        "schema_version": 1,
        "channel": "test",
        "version": "0.0.0",
        "paused": True,
        "paused_version": "0.2.5",
        "paused_at": "2026-07-31T08:00:00Z",
    }
    path.write_text(json.dumps(marker), encoding="utf-8")

    versioned = client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/0.2.4"},
    )
    headerless = client.get("/updates/test.json")

    assert versioned.status_code == 200
    assert versioned.json() == {
        "schema_version": 1,
        "channel": "test",
        "version": "0.2.4",
        "paused": True,
        "paused_version": "0.2.5",
        "selected_platform": "windows-x86_64",
    }
    assert versioned.headers["x-intdemo-update-package"] == "paused"
    assert versioned.headers["x-intdemo-update-distribution"] == "paused"
    assert headerless.status_code == 204
    assert headerless.content == b""
    assert json.loads(path.read_text(encoding="utf-8")) == marker
