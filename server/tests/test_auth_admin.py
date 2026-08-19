from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

import app.main as main_module
from app.config import get_settings
from app.connection_test import connection_test_gate
from app.database import SessionLocal
from app.models import Account, ActivityEvent, WorkflowBatch, WorkflowRun

from .conftest import auth_header, changed_admin, device_uid, login


def _control_admin(client, *, device: int = 900) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "Admin!23456",
            "device_uid": device_uid(device),
            "device_name": "release-publisher",
            "client_version": "test-control",
            "control_client": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_connection_blocked(response) -> None:
    assert response.status_code == 503
    assert response.json()["code"] == "test_connection_blocked"
    assert response.json()["retryable"] is True


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


def test_connection_test_control_login_and_client_listing(client):
    connection_test_gate.reset()
    try:
        business_admin = changed_admin(client)
        regular_headers = auth_header(business_admin)
        regular_denied = client.get(
            "/api/v1/admin/connection-test/clients",
            headers=regular_headers,
        )
        assert regular_denied.status_code == 403
        assert regular_denied.json()["code"] == "control_client_required"

        limited = client.patch(
            f"/api/v1/admin/accounts/{business_admin['account']['id']}",
            headers=regular_headers,
            json={"device_limit": 1},
        )
        assert limited.status_code == 200, limited.text

        non_admin_control = client.post(
            "/api/v1/auth/login",
            json={
                "username": "luogang",
                "password": "123456",
                "device_uid": device_uid(902),
                "device_name": "invalid-control",
                "client_version": "test-control",
                "control_client": True,
            },
        )
        assert non_admin_control.status_code == 403
        assert non_admin_control.json()["code"] == "admin_required"

        control = _control_admin(client)
        assert control["account"]["active_device_count"] == 1
        assert control["account"]["online_device_count"] == 1
        overview = client.get(
            "/api/v1/admin/connection-test/clients",
            headers=auth_header(control),
        )
        assert overview.status_code == 200, overview.text
        assert overview.json()["global_blocked"] is False
        listed_ids = {row["id"] for row in overview.json()["clients"]}
        assert business_admin["device"]["id"] in listed_ids
        assert control["device"]["id"] not in listed_ids

        account_devices = client.get(
            f"/api/v1/admin/accounts/{control['account']['id']}/devices",
            headers=auth_header(control),
        )
        assert account_devices.status_code == 200
        assert control["device"]["id"] in {
            row["id"] for row in account_devices.json()
        }

        connection_test_gate.reset()
        after_restart = client.get(
            "/api/v1/admin/connection-test/clients",
            headers=auth_header(control),
        )
        assert after_restart.status_code == 200, after_restart.text
        assert after_restart.json()["global_blocked"] is False
    finally:
        connection_test_gate.reset()


def test_connection_test_blocks_http_login_and_refresh_until_restored(client):
    connection_test_gate.reset()
    try:
        business_admin = changed_admin(client)
        control = _control_admin(client)
        control_headers = auth_header(control)
        target_id = business_admin["device"]["id"]

        disconnected = client.post(
            f"/api/v1/admin/connection-test/devices/{target_id}/disconnect",
            headers=control_headers,
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json() == {
            "global_blocked": False,
            "device_id": target_id,
            "blocked": True,
            "closed_websockets": 0,
        }

        _assert_connection_blocked(
            client.get(
                "/api/v1/sync/pull",
                headers=auth_header(business_admin),
            )
        )
        _assert_connection_blocked(
            client.post(
                "/api/v1/auth/refresh",
                json={
                    "refresh_token": business_admin["refresh_token"],
                    "device_uid": business_admin["device"]["device_uid"],
                },
            )
        )
        _assert_connection_blocked(
            client.post(
                "/api/v1/auth/login",
                json={
                    "username": "admin",
                    "password": "Admin!23456",
                    "device_uid": business_admin["device"]["device_uid"],
                    "device_name": "blocked-business-client",
                    "client_version": "test",
                },
            )
        )

        restored = client.post(
            f"/api/v1/admin/connection-test/devices/{target_id}/restore",
            headers=control_headers,
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["blocked"] is False
        assert client.get(
            "/api/v1/sync/pull",
            headers=auth_header(business_admin),
        ).status_code == 200
        refreshed = client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": business_admin["refresh_token"],
                "device_uid": business_admin["device"]["device_uid"],
            },
        )
        assert refreshed.status_code == 200, refreshed.text
    finally:
        connection_test_gate.reset()


def test_connection_test_global_block_allows_one_device_override(client):
    connection_test_gate.reset()
    try:
        first = changed_admin(client)
        second = login(client, "admin", "Admin!23456", device=903)
        control = _control_admin(client, device=904)
        control_headers = auth_header(control)

        disconnected = client.post(
            "/api/v1/admin/connection-test/all/disconnect",
            headers=control_headers,
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["global_blocked"] is True
        _assert_connection_blocked(
            client.get("/api/v1/sync/pull", headers=auth_header(first))
        )
        _assert_connection_blocked(
            client.get("/api/v1/sync/pull", headers=auth_header(second))
        )
        assert client.get("/api/v1/health/live").status_code == 200

        restored_first = client.post(
            (
                "/api/v1/admin/connection-test/devices/"
                f"{first['device']['id']}/restore"
            ),
            headers=control_headers,
        )
        assert restored_first.status_code == 200, restored_first.text
        assert restored_first.json()["global_blocked"] is True
        assert client.get(
            "/api/v1/sync/pull",
            headers=auth_header(first),
        ).status_code == 200
        _assert_connection_blocked(
            client.get("/api/v1/sync/pull", headers=auth_header(second))
        )

        restored_all = client.post(
            "/api/v1/admin/connection-test/all/restore",
            headers=control_headers,
        )
        assert restored_all.status_code == 200, restored_all.text
        assert restored_all.json()["global_blocked"] is False
        assert client.get(
            "/api/v1/sync/pull",
            headers=auth_header(second),
        ).status_code == 200
    finally:
        connection_test_gate.reset()


def test_connection_test_immediately_closes_only_target_websocket(client):
    connection_test_gate.reset()
    try:
        first = changed_admin(client)
        second = login(client, "admin", "Admin!23456", device=905)
        control = _control_admin(client, device=906)
        control_headers = auth_header(control)

        with client.websocket_connect(
            "/api/v1/ws/updates",
            headers=auth_header(first),
        ) as first_socket:
            with client.websocket_connect(
                "/api/v1/ws/updates",
                headers=auth_header(second),
            ) as second_socket:
                assert first_socket.receive_json()["type"] == "connected"
                assert second_socket.receive_json()["type"] == "connected"

                disconnected = client.post(
                    (
                        "/api/v1/admin/connection-test/devices/"
                        f"{first['device']['id']}/disconnect"
                    ),
                    headers=control_headers,
                )
                assert disconnected.status_code == 200, disconnected.text
                assert disconnected.json()["closed_websockets"] == 1
                assert first_socket.receive_json() == {
                    "type": "test_connection_blocked"
                }
                with pytest.raises(WebSocketDisconnect) as closed:
                    first_socket.receive_text()
                assert closed.value.code == 4410

                second_socket.send_text("ping")
                assert second_socket.receive_text() == "pong"

                with pytest.raises(WebSocketDisconnect) as rejected:
                    with client.websocket_connect(
                        "/api/v1/ws/updates",
                        headers=auth_header(first),
                    ):
                        pass
                assert rejected.value.code == 4403

                restored = client.post(
                    (
                        "/api/v1/admin/connection-test/devices/"
                        f"{first['device']['id']}/restore"
                    ),
                    headers=control_headers,
                )
                assert restored.status_code == 200
                with client.websocket_connect(
                    "/api/v1/ws/updates",
                    headers=auth_header(first),
                ) as restored_socket:
                    assert restored_socket.receive_json()["type"] == "connected"
    finally:
        connection_test_gate.reset()


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


def test_account_lifecycle_device_limit_and_audit(client):
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
    assert created.json()["last_login_at"] is None

    station = login(client, "station_test", "123456", device=20)
    listed_accounts = client.get(
        "/api/v1/admin/accounts",
        headers=headers,
    ).json()
    station_row = next(row for row in listed_accounts if row["id"] == account_id)
    assert station_row["last_login_at"] is not None
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
    assert second.status_code == 409
    assert second.json()["code"] == "device_limit_reached"
    assert second.json()["details"] == {
        "device_limit": 1,
        "active_device_count": 1,
    }

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
    assert len(devices.json()) == 1
    device_id = devices.json()[0]["id"]
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


def test_device_limit_cannot_be_lowered_below_active_count(client):
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
    assert lowered.status_code == 409
    assert lowered.json()["code"] == "device_limit_below_active_count"
    assert lowered.json()["details"] == {"active_device_count": 2}


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
    with SessionLocal() as db:
        occurred_at = datetime.now(UTC)
        db.add_all(
            [
                ActivityEvent(
                    account_id=uuid.UUID(account_id),
                    event_uid=uuid.uuid4(),
                    metric_key="workflow_detail_total",
                    amount=7,
                    business_date=date.today(),
                    source="test",
                    summary={},
                    occurred_at=occurred_at,
                ),
                WorkflowBatch(
                    account_id=uuid.UUID(account_id),
                    entity_id="deleted-account-batch",
                    event_uid=uuid.uuid4(),
                    revision=1,
                    status="succeeded",
                    input_fingerprint="a" * 64,
                    business_date=date.today(),
                    occurred_at=occurred_at,
                ),
                WorkflowRun(
                    account_id=uuid.UUID(account_id),
                    entity_id="deleted-account-run",
                    batch_entity_id="deleted-account-batch",
                    event_uid=uuid.uuid4(),
                    revision=1,
                    status="succeeded",
                    occurred_at=occurred_at,
                ),
            ]
        )
        db.commit()
    deleted = client.delete(
        f"/api/v1/admin/accounts/{account_id}",
        headers=headers,
    )
    assert deleted.status_code == 204
    accounts = client.get("/api/v1/admin/accounts", headers=headers).json()
    assert account_id not in {str(account["id"]) for account in accounts}
    with SessionLocal() as db:
        assert db.get(Account, uuid.UUID(account_id)) is None
        assert db.scalar(
            select(ActivityEvent.id).where(
                ActivityEvent.account_id == uuid.UUID(account_id)
            )
        ) is None
        assert db.scalar(
            select(WorkflowBatch.id).where(
                WorkflowBatch.account_id == uuid.UUID(account_id)
            )
        ) is None
        assert db.scalar(
            select(WorkflowRun.id).where(
                WorkflowRun.account_id == uuid.UUID(account_id)
            )
        ) is None
    audit_rows = client.get("/api/v1/admin/audit", headers=headers).json()["items"]
    assert any(
        row["action"] == "account.delete" and row["target_id"] == account_id
        for row in audit_rows
    )
