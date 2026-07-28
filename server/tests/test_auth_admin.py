from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.config import get_settings
from app.database import SessionLocal
from app.models import Account

from .conftest import auth_header, changed_admin, device_uid, login


def test_bootstrap_requires_admin_password_change(client):
    with SessionLocal() as db:
        password_hash = db.scalar(select(Account.password_hash).where(Account.username == "admin"))
    assert password_hash.startswith("$argon2id$")

    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "admin_password_change_required"

    initial = login(client, "admin", "123456")
    assert initial["account"]["must_change_password"] is True
    blocked = client.get("/api/v1/admin/accounts", headers=auth_header(initial))
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"

    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(initial),
        json={"current_password": "123456", "new_password": "Admin!23456"},
    )
    assert changed.status_code == 200
    assert changed.json()["account"]["must_change_password"] is False
    assert client.get("/api/v1/health/ready").status_code == 200


def test_bootstrap_station_can_login_from_multiple_computers(client):
    first = login(client, "luogang", "123456", device=30)
    second = login(client, "luogang", "123456", device=31)

    assert first["account"]["is_active"] is True
    assert first["account"]["must_change_password"] is True
    assert first["device"]["device_uid"] == device_uid(30)
    assert second["device"]["device_uid"] == device_uid(31)


def test_errors_use_the_stable_envelope(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert set(response.json()) == {
        "code",
        "message",
        "retryable",
        "details",
        "request_id",
    }
    assert response.json()["code"] == "not_found"


def test_maintenance_mode_rejects_api_writes_but_keeps_health(monkeypatch, client):
    maintenance_settings = replace(get_settings(), maintenance_mode=True)
    monkeypatch.setattr(main_module, "get_settings", lambda: maintenance_settings)
    maintenance_app = main_module.create_app()
    with TestClient(maintenance_app) as maintenance_client:
        assert maintenance_client.get("/api/v1/health/live").status_code == 200
        assert maintenance_client.get("/api/v1/health/ready").status_code == 503
        blocked = maintenance_client.post(
            "/api/v1/auth/login",
            json={},
        )
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "maintenance_mode"
    assert blocked.json()["retryable"] is True


def test_refresh_rotation_detects_reuse(client):
    bundle = changed_admin(client)
    rotated = client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": bundle["refresh_token"],
            "device_uid": bundle["device"]["device_uid"],
        },
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != bundle["refresh_token"]

    reuse = client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": bundle["refresh_token"],
            "device_uid": bundle["device"]["device_uid"],
        },
    )
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "refresh_token_reuse"

    family_revoked = client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": rotated.json()["refresh_token"],
            "device_uid": bundle["device"]["device_uid"],
        },
    )
    assert family_revoked.status_code == 401


def test_logout_invalidates_current_device_access_token(client):
    bundle = changed_admin(client)
    response = client.post(
        "/api/v1/auth/logout",
        headers=auth_header(bundle),
        json={"refresh_token": bundle["refresh_token"]},
    )
    assert response.status_code == 204
    denied = client.get("/api/v1/sync/pull", headers=auth_header(bundle))
    assert denied.status_code == 401
    assert denied.json()["code"] == "session_revoked"


def test_login_throttle_locks_username_and_ip(client):
    payload = {
        "username": "admin",
        "password": "wrong-password",
        "device_uid": device_uid(5),
        "device_name": "lock-test",
        "client_version": "test",
    }
    for _ in range(5):
        response = client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401
    locked = client.post(
        "/api/v1/auth/login",
        json={**payload, "password": "123456"},
    )
    assert locked.status_code == 429
    assert locked.json()["code"] == "login_locked"


def test_account_lifecycle_multi_device_login_and_audit(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    created = client.post(
        "/api/v1/admin/accounts",
        headers=headers,
        json={
            "username": "station_test",
            "display_name": "测试站",
            "stats_scope": "own",
            "device_limit": 1,
            "is_active": True,
        },
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    station = login(client, "station_test", "123456", device=20)
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(station),
        json={"current_password": "123456", "new_password": "Station!234"},
    )
    assert changed.status_code == 200
    second = client.post(
        "/api/v1/auth/login",
        json={
            "username": "station_test",
            "password": "Station!234",
            "device_uid": device_uid(21),
            "device_name": "second",
            "client_version": "test",
        },
    )
    assert second.status_code == 200
    assert second.json()["device"]["device_uid"] == device_uid(21)

    too_low = client.patch(
        f"/api/v1/admin/accounts/{account_id}",
        headers=headers,
        json={"device_limit": 1},
    )
    assert too_low.status_code == 200

    devices = client.get(
        f"/api/v1/admin/accounts/{account_id}/devices",
        headers=headers,
    )
    assert devices.status_code == 200
    assert len(devices.json()) == 2
    device_id = next(
        item["id"]
        for item in devices.json()
        if item["device_uid"] == device_uid(20)
    )
    revoked = client.post(
        f"/api/v1/admin/devices/{device_id}/revoke",
        headers=headers,
    )
    assert revoked.status_code == 204

    after_revoke = login(client, "station_test", "Station!234", device=21)
    assert after_revoke["device"]["device_uid"] == device_uid(21)

    archived = client.post(
        f"/api/v1/admin/accounts/{account_id}/archive",
        headers=headers,
    )
    assert archived.status_code == 200
    denied = client.post(
        "/api/v1/auth/login",
        json={
            "username": "station_test",
            "password": "Station!234",
            "device_uid": device_uid(21),
            "device_name": "second",
            "client_version": "test",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "account_archived"

    restored = client.post(
        f"/api/v1/admin/accounts/{account_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["is_active"] is False
    audit = client.get("/api/v1/admin/audit", headers=headers)
    actions = {item["action"] for item in audit.json()["items"]}
    assert {"account.create", "device.revoke", "account.archive", "account.restore"} <= actions


def test_legacy_device_limit_does_not_block_additional_computers(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    own_id = admin["account"]["id"]
    expanded = client.patch(
        f"/api/v1/admin/accounts/{own_id}",
        headers=headers,
        json={"device_limit": 2},
    )
    assert expanded.status_code == 200
    login(client, "admin", "Admin!23456", device=2)

    lowered = client.patch(
        f"/api/v1/admin/accounts/{own_id}",
        headers=headers,
        json={"device_limit": 1},
    )
    assert lowered.status_code == 200
    third = login(client, "admin", "Admin!23456", device=3)
    assert third["device"]["device_uid"] == device_uid(3)


def test_account_update_rejects_explicit_null(client):
    admin = changed_admin(client)
    response = client.patch(
        f"/api/v1/admin/accounts/{admin['account']['id']}",
        headers=auth_header(admin),
        json={"stats_scope": None},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_archived_account_can_be_permanently_deleted(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    created = client.post(
        "/api/v1/admin/accounts",
        headers=headers,
        json={
            "username": "delete_archive",
            "display_name": "待删除归档站点",
            "role": "user",
            "stats_scope": "own",
            "device_limit": 3,
            "is_active": True,
        },
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    not_archived = client.delete(
        f"/api/v1/admin/accounts/{account_id}",
        headers=headers,
    )
    assert not_archived.status_code == 409
    assert not_archived.json()["code"] == "account_not_archived"

    assert client.post(
        f"/api/v1/admin/accounts/{account_id}/archive",
        headers=headers,
    ).status_code == 200
    deleted = client.delete(
        f"/api/v1/admin/accounts/{account_id}",
        headers=headers,
    )
    assert deleted.status_code == 204
    accounts = client.get("/api/v1/admin/accounts", headers=headers).json()
    assert account_id not in {str(account["id"]) for account in accounts}
    audit_rows = client.get("/api/v1/admin/audit", headers=headers).json()["items"]
    assert any(
        row["action"] == "account.delete" and row["target_id"] == account_id
        for row in audit_rows
    )
