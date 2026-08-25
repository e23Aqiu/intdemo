from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from .conftest import auth_header, changed_admin, login


def sync_item(
    *,
    event_uid: str | None = None,
    kind: str = "activity_event",
    entity_id: str = "event-1",
    revision: int = 1,
    payload: dict | None = None,
    occurred_at: str | None = None,
) -> dict:
    if payload is None:
        payload = {
            "metric_key": "workflow_detail_total",
            "amount": 3,
            "business_date": "2026-07-27",
            "source": "unified_workflow",
            "summary": {"row_count": 3, "violation_counts": {"超限": 2}},
        }
    return {
        "schema_version": 1,
        "event_uid": event_uid or str(uuid.uuid4()),
        "kind": kind,
        "entity_id": entity_id,
        "revision": revision,
        "occurred_at": occurred_at or datetime.now(UTC).isoformat(),
        "payload": payload,
    }


def test_activity_push_is_idempotent_and_pullable(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    item = sync_item()
    first = client.post("/api/v1/sync/push", headers=headers, json={"items": [item]})
    assert first.status_code == 200, first.text
    assert first.json()["items"][0]["status"] == "accepted"
    first_revision = first.json()["items"][0]["server_revision"]

    duplicate = client.post("/api/v1/sync/push", headers=headers, json={"items": [item]})
    assert duplicate.status_code == 200
    assert duplicate.json()["items"][0] == {
        "event_uid": item["event_uid"],
        "status": "duplicate",
        "server_revision": first_revision,
        "code": None,
        "message": None,
        "retryable": False,
    }
    pulled = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=headers,
    )
    matches = [row for row in pulled.json()["changes"] if row["kind"] == "activity_event"]
    assert len(matches) == 1
    assert matches[0]["payload"]["amount"] == 3

    snapshot = client.get("/api/v1/sync/snapshot", headers=headers)
    assert len(snapshot.json()["activity_events"]) == 1


def test_activity_preserves_yellow_vehicle_subset_and_legacy_compatibility(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    yellow_item = sync_item(
        entity_id="yellow-event",
        payload={
            "metric_key": "workflow_detail_total",
            "amount": 5,
            "yellow_amount": 2,
            "business_date": "2026-08-25",
            "source": "unified_workflow",
            "summary": {
                "row_count": 5,
                "violation_counts": {
                    "混合原因": {"total": 4, "has_phone": 2, "other": 2}
                },
                "yellow_violation_counts": {
                    "混合原因": {"total": 1, "has_phone": 1, "other": 0}
                },
            },
        },
    )
    legacy_item = sync_item(entity_id="legacy-event")
    response = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": [yellow_item, legacy_item]},
    )
    assert response.status_code == 200, response.text
    assert [row["status"] for row in response.json()["items"]] == [
        "accepted",
        "accepted",
    ]

    events = client.get(
        "/api/v1/sync/snapshot",
        headers=headers,
    ).json()["activity_events"]
    by_entity = {row["event_uid"]: row for row in events}
    yellow = by_entity[yellow_item["event_uid"]]
    legacy = by_entity[legacy_item["event_uid"]]
    assert yellow["yellow_amount"] == 2
    assert yellow["summary"]["yellow_violation_counts"]["混合原因"] == {
        "total": 1,
        "has_phone": 1,
        "other": 0,
    }
    assert legacy["yellow_amount"] is None

    invalid = sync_item(
        entity_id="invalid-yellow-event",
        payload={
            "metric_key": "workflow_detail_total",
            "amount": 2,
            "yellow_amount": 3,
            "business_date": "2026-08-25",
            "source": "unified_workflow",
            "summary": {},
        },
    )
    rejected = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": [invalid]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["items"][0]["status"] == "rejected"
    assert rejected.json()["items"][0]["code"] == "invalid_sync_payload"


def test_v110_and_v111_clients_keep_login_push_and_pull_compatibility(client):
    changed_admin(client)

    for device, client_version in enumerate(("1.1.0", "1.1.1"), start=110):
        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "username": "admin",
                "password": "Admin!23456",
                "device_uid": str(uuid.UUID(int=device)),
                "device_name": f"legacy-{client_version}",
                "client_version": client_version,
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        headers = auth_header(logged_in.json())
        item = sync_item(
            entity_id=f"legacy-{client_version}",
            payload={
                "metric_key": "workflow_detail_total",
                "amount": 3,
                "business_date": "2026-08-25",
                "source": "unified_workflow",
                "summary": {
                    "row_count": 3,
                    "violation_counts": {"旧版原因": 2},
                },
            },
        )

        pushed = client.post(
            "/api/v1/sync/push",
            headers=headers,
            json={"items": [item]},
        )
        assert pushed.status_code == 200, pushed.text
        assert pushed.json()["items"][0]["status"] == "accepted"

        pulled = client.get(
            "/api/v1/sync/pull?after_revision=0",
            headers=headers,
        )
        assert pulled.status_code == 200, pulled.text
        activity = next(
            row
            for row in pulled.json()["changes"]
            if row["entity_id"] == item["entity_id"]
        )
        assert activity["payload"]["amount"] == 3
        assert activity["payload"]["yellow_amount"] is None


def test_payload_privacy_allowlist_and_batch_limit(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    forbidden = sync_item()
    forbidden["payload"]["file_name"] = "运输数据.xlsx"
    response = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": [forbidden]},
    )
    assert response.status_code == 200
    result = response.json()["items"][0]
    assert result["status"] == "rejected"
    assert result["code"] == "unknown_sync_fields"

    too_many = [sync_item(entity_id=f"event-{index}") for index in range(101)]
    response = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": too_many},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "sync_batch_too_large"


def test_snapshot_revision_and_success_terminal_are_monotonic(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    fingerprint = hashlib.sha256(b"local-input").hexdigest()
    first = sync_item(
        kind="workflow_batch_snapshot",
        entity_id="batch-1",
        revision=1,
        payload={
            "status": "running",
            "input_fingerprint": fingerprint,
            "business_date": "2026-07-27",
            "active_ms": 100,
            "paused_ms": 0,
            "elapsed_ms": 100,
            "retry_count": 0,
            "counters": {},
        },
    )
    succeeded = sync_item(
        kind="workflow_batch_snapshot",
        entity_id="batch-1",
        revision=2,
        payload={**first["payload"], "status": "succeeded", "active_ms": 200},
    )
    stale = sync_item(
        kind="workflow_batch_snapshot",
        entity_id="batch-1",
        revision=1,
        payload=first["payload"],
    )
    regression = sync_item(
        kind="workflow_batch_snapshot",
        entity_id="batch-1",
        revision=3,
        payload=first["payload"],
    )
    response = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": [first, succeeded, stale, regression]},
    )
    assert response.status_code == 200, response.text
    assert [row["status"] for row in response.json()["items"]] == [
        "accepted",
        "accepted",
        "stale",
        "rejected",
    ]
    assert response.json()["items"][3]["code"] == "terminal_state_regression"


def test_reset_tombstone_rejects_old_offline_upload(client):
    admin = changed_admin(client)
    headers = auth_header(admin)
    reset = client.post(
        "/api/v1/admin/data-reset",
        headers=headers,
        json={"account_id": admin["account"]["id"], "confirmation": "RESET"},
    )
    assert reset.status_code == 204
    old = sync_item(occurred_at="2020-01-01T00:00:00+00:00")
    response = client.post(
        "/api/v1/sync/push",
        headers=headers,
        json={"items": [old]},
    )
    result = response.json()["items"][0]
    assert result["status"] == "rejected"
    assert result["code"] == "data_reset_tombstone"


def test_stats_scope_own_then_all_is_enforced_server_side(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)

    def create_and_change(username, device, scope="own"):
        created = client.post(
            "/api/v1/admin/accounts",
            headers=admin_headers,
            json={
                "username": username,
                "display_name": username,
                "stats_scope": scope,
                "device_limit": 1,
                "is_active": True,
            },
        )
        assert created.status_code == 201
        initial = login(client, username, "123456", device=device)
        changed = client.post(
            "/api/v1/auth/change-password",
            headers=auth_header(initial),
            json={
                "current_password": "123456",
                "new_password": f"{username}!234",
            },
        )
        assert changed.status_code == 200
        return created.json(), changed.json()

    producer_account, producer = create_and_change("producer", 40)
    item = sync_item(entity_id="producer-event")
    pushed = client.post(
        "/api/v1/sync/push",
        headers=auth_header(producer),
        json={"items": [item]},
    )
    assert pushed.json()["items"][0]["status"] == "accepted"

    viewer_account, viewer = create_and_change("viewer", 41, "own")
    own_pull = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=auth_header(viewer),
    )
    assert not any(
        row["account_id"] == producer_account["id"] and row["kind"] == "activity_event"
        for row in own_pull.json()["changes"]
    )

    tightened_token = viewer["access_token"]
    expanded = client.patch(
        f"/api/v1/admin/accounts/{viewer_account['id']}",
        headers=admin_headers,
        json={"stats_scope": "all"},
    )
    assert expanded.status_code == 200
    revoked = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers={"Authorization": f"Bearer {tightened_token}"},
    )
    assert revoked.status_code == 401

    viewer = login(client, "viewer", "viewer!234", device=41)
    all_pull = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=auth_header(viewer),
    )
    assert any(
        row["account_id"] == producer_account["id"] and row["kind"] == "activity_event"
        for row in all_pull.json()["changes"]
    )


def test_test_account_is_user_compatible_but_statistics_are_discarded(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    created = client.post(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        json={
            "username": "untracked_test",
            "display_name": "测试账号",
            "role": "user",
            "is_test": True,
            "stats_scope": "all",
            "device_limit": 1,
            "is_active": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "user"
    assert created.json()["is_test"] is True
    assert created.json()["stats_scope"] == "all"

    first_login = login(client, "untracked_test", "123456", device=77)
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(first_login),
        json={
            "current_password": "123456",
            "new_password": "Untracked!234",
        },
    )
    assert changed.status_code == 200, changed.text
    bundle = changed.json()
    assert bundle["account"]["role"] == "user"
    assert bundle["account"]["is_test"] is True
    assert bundle["account"]["stats_scope"] == "all"

    admin_item = sync_item(entity_id="visible-normal-event")
    normal_push = client.post(
        "/api/v1/sync/push",
        headers=admin_headers,
        json={"items": [admin_item]},
    )
    assert normal_push.json()["items"][0]["status"] == "accepted"

    item = sync_item(entity_id="test-account-event")
    first = client.post(
        "/api/v1/sync/push",
        headers=auth_header(bundle),
        json={"items": [item]},
    )
    duplicate = client.post(
        "/api/v1/sync/push",
        headers=auth_header(bundle),
        json={"items": [item]},
    )
    assert first.json()["items"][0]["status"] == "accepted"
    assert duplicate.json()["items"][0]["status"] == "duplicate"

    snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=admin_headers,
    )
    assert snapshot.status_code == 200
    assert not any(
        row["account_id"] == created.json()["id"]
        for row in snapshot.json()["activity_events"]
    )
    test_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(bundle),
    )
    assert any(
        row["event_uid"] == admin_item["event_uid"]
        for row in test_snapshot.json()["activity_events"]
    )
    pulled = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=admin_headers,
    )
    assert not any(
        row["account_id"] == created.json()["id"]
        and row["kind"] != "account"
        for row in pulled.json()["changes"]
    )
