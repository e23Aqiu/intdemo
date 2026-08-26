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
                },
                "deltas": [
                    {
                        "format": "uos-deb-xdelta-v1",
                        "algorithm": "xdelta3",
                        "from_version": "0.2.4",
                        "base_sha256": "d" * 64,
                        "base_size": 619_000_000,
                        "installer_path": (
                            "/updates/files/IntDemo-UOS-arm64-Patch-"
                            "0.2.4-to-0.2.5.intdelta"
                        ),
                        "sha256": "e" * 64,
                        "size": 24_000_000,
                        "target_sha256": "c" * 64,
                        "target_size": 620_000_000,
                    }
                ],
                "layered_updates": [
                    {
                        "format": "uos-layered-v1",
                        "from_version": "0.2.4",
                        "source_layout_sha256": "f" * 64,
                        "target_layout_sha256": "1" * 64,
                        "installer_path": (
                            "/updates/files/IntDemo-UOS-arm64-Layers-"
                            "0.2.4-to-0.2.5.intlayer"
                        ),
                        "sha256": "2" * 64,
                        "size": 8_000_000,
                    }
                ],
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
    assert "deltas" not in payload


def test_update_server_advertises_source_version_targeting(client):
    response = client.get("/updates/capabilities.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "source-version-targeting-v1" in response.json()["capabilities"]
    assert "uos-layered-v1" in response.json()["capabilities"]


def test_targeted_update_only_reaches_exact_client_versions(
    client,
    update_manifest_file,
):
    path, canonical = update_manifest_file
    targeted = dict(canonical)
    targeted["eligible_client_versions"] = ["1.1.0"]
    path.write_text(json.dumps(targeted), encoding="utf-8")

    eligible = client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/1.1.0"},
    )
    other = client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/1.1.1"},
    )
    headerless = client.get("/updates/test.json")

    assert eligible.status_code == 200
    assert eligible.json()["installer_path"].endswith(".exe")
    assert eligible.headers["x-intdemo-update-targeting"] == (
        "source-version-targeting-v1"
    )
    for response in (other, headerless):
        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["x-intdemo-update-package"] == "not-targeted"
        assert response.headers["x-intdemo-update-distribution"] == "not-targeted"
    assert other.headers["x-intdemo-client-version"] == "1.1.1"


@pytest.mark.parametrize(
    "targeting",
    [[], "1.1.0", ["bad"], ["1.1.0", "1.1.0"], [1, 2]],
)
def test_invalid_targeting_manifest_fails_closed(
    client,
    update_manifest_file,
    targeting,
):
    path, canonical = update_manifest_file
    targeted = dict(canonical)
    targeted["eligible_client_versions"] = targeting
    path.write_text(json.dumps(targeted), encoding="utf-8")

    response = client.get(
        "/updates/test.json",
        headers={"User-Agent": "IntDemoUpdater/1.1.0"},
    )

    assert response.status_code == 503


def test_uos_delta_requires_explicit_client_capability(client, update_manifest_file):
    _path, canonical = update_manifest_file
    response = client.get(
        "/updates/test.json",
        headers={
            "X-IntDemo-Version": "0.2.4",
            "X-IntDemo-Platform": "linux-aarch64",
            "X-IntDemo-Update-Capabilities": "uos-deb-xdelta-v1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    delta = canonical["platforms"]["linux-aarch64"]["deltas"][0]
    assert payload["installer_path"] == delta["installer_path"]
    assert payload["primary_kind"] == "delta"
    assert payload["primary_from_version"] == "0.2.4"
    assert payload["full"] == canonical["platforms"]["linux-aarch64"]["full"]
    assert response.headers["x-intdemo-update-package"] == "delta"


def test_uos_layered_update_is_preferred_with_explicit_capability(
    client, update_manifest_file
):
    _path, canonical = update_manifest_file
    response = client.get(
        "/updates/test.json",
        headers={
            "X-IntDemo-Version": "0.2.4",
            "X-IntDemo-Platform": "linux-aarch64",
            "X-IntDemo-Update-Capabilities": (
                "uos-layered-v1, uos-deb-xdelta-v1"
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    layered = canonical["platforms"]["linux-aarch64"]["layered_updates"][0]
    assert payload["installer_path"] == layered["installer_path"]
    assert payload["primary_kind"] == "layered"
    assert payload["primary_from_version"] == "0.2.4"
    assert response.headers["x-intdemo-update-package"] == "layered"


@pytest.mark.parametrize(
    "version", ["1.1.0", "1.1.1", "1.2.0", "1.2.1", "0.2.4"]
)
def test_old_uos_clients_always_receive_full_deb(
    client,
    update_manifest_file,
    version,
):
    response = client.get(
        "/updates/test.json",
        headers={
            "X-IntDemo-Version": version,
            "X-IntDemo-Platform": "linux-aarch64",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["installer_path"].endswith(".deb")
    assert payload["primary_kind"] == "full"
    assert "deltas" not in payload
    assert "layered_updates" not in payload
    nested = payload["platforms"]["linux-aarch64"]
    assert "delta" not in nested
    assert "deltas" not in nested
    assert "layers" not in nested
    assert "layered_updates" not in nested
    assert nested["full"]["installer_path"].endswith(".deb")


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
