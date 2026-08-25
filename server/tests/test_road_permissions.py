from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.bootstrap import bootstrap_database
from app.database import SessionLocal
from app.models import Account, Road
from app.permissions import DEFAULT_ROAD_NAME
from app.security import hash_password

from .conftest import auth_header, changed_admin, device_uid


def _login(
    client,
    username: str,
    password: str,
    *,
    device: int,
    version: str = "1.2.0",
) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": password,
            "device_uid": device_uid(device),
            "device_name": f"hierarchy-{device}",
            "client_version": version,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _activate(
    client,
    username: str,
    *,
    device: int,
    password: str,
    version: str = "1.2.0",
) -> dict:
    initial = _login(
        client,
        username,
        "123456",
        device=device,
        version=version,
    )
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(initial),
        json={"current_password": "123456", "new_password": password},
    )
    assert changed.status_code == 200, changed.text
    return changed.json()


def _create_account(client, headers, **payload) -> dict:
    response = client.post(
        "/api/v1/admin/accounts",
        headers=headers,
        json={
            "device_limit": 10000,
            "is_active": True,
            **payload,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_road_manager(
    client,
    headers,
    *,
    username: str,
    road_name: str,
) -> dict:
    return _create_account(
        client,
        headers,
        username=username,
        display_name=road_name,
        account_type="road_admin",
        data_scope="road",
        road_name=road_name,
    )


def _activity_item(entity_id: str, amount: int) -> dict:
    return {
        "schema_version": 1,
        "event_uid": str(uuid.uuid4()),
        "kind": "activity_event",
        "entity_id": entity_id,
        "revision": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {
            "metric_key": "workflow_detail_total",
            "amount": amount,
            "business_date": "2026-08-25",
            "source": "hierarchy-test",
            "summary": {"row_count": amount},
        },
    }


def test_bootstrap_preserves_unassigned_accounts_without_revoking_sessions(client):
    admin = changed_admin(client)
    headers = auth_header(admin)

    with SessionLocal() as db:
        road = db.scalar(select(Road).where(Road.name == DEFAULT_ROAD_NAME))
        admin_row = db.scalar(select(Account).where(Account.username == "admin"))
        station = db.scalar(select(Account).where(Account.username == "luogang"))
        assert road is None
        assert admin_row.account_type == "admin"
        assert admin_row.data_scope == "all"
        assert admin_row.road_id is None
        assert station.account_type == "station"
        assert station.data_scope == "own"
        assert station.road_id is None
        bootstrap_database(db)

    # Bootstrap and migration-style backfills do not touch token versions.
    assert client.get("/api/v1/sync/pull", headers=headers).status_code == 200

    legacy = _login(
        client,
        "admin",
        "Admin!23456",
        device=120,
        version="1.1.0",
    )
    assert legacy["account"]["role"] == "admin"
    assert legacy["account"]["stats_scope"] == "all"
    snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(legacy),
    )
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["stats_scope"] == "all"


def test_bootstrap_reconciles_legacy_writes_during_rolling_upgrade(client):
    with SessionLocal() as db:
        legacy_all = Account(
            username="legacy_scope_all",
            display_name="旧版全部范围",
            password_hash=hash_password("123456"),
            role="user",
            account_type="station",
            stats_scope="all",
            data_scope="own",
            road_id=None,
        )
        legacy_demoted = Account(
            username="legacy_demoted_admin",
            display_name="旧版降级管理员",
            password_hash=hash_password("123456"),
            role="user",
            account_type="admin",
            stats_scope="own",
            data_scope="all",
            road_id=None,
        )
        legacy_promoted = Account(
            username="legacy_promoted_admin",
            display_name="旧版提升管理员",
            password_hash=hash_password("123456"),
            role="admin",
            account_type="station",
            stats_scope="all",
            data_scope="own",
            road_id=None,
        )
        db.add_all([legacy_all, legacy_demoted, legacy_promoted])
        db.commit()

        bootstrap_database(db)
        assert legacy_all.account_type == "station"
        assert legacy_all.data_scope == "all"
        assert legacy_all.road_id is None
        assert legacy_demoted.account_type == "station"
        assert legacy_demoted.data_scope == "own"
        assert legacy_demoted.road_id is None
        assert legacy_promoted.account_type == "admin"
        assert legacy_promoted.data_scope == "all"
        assert legacy_promoted.road_id is None


def test_bootstrap_detaches_test_accounts_without_revoking_sessions(client):
    with SessionLocal() as db:
        default_road = Road(name=DEFAULT_ROAD_NAME)
        db.add(default_road)
        db.flush()
        test_account = Account(
            username="bootstrap_direct_test",
            display_name="直属测试账号",
            password_hash=hash_password("123456"),
            role="user",
            account_type="station",
            is_test=True,
            stats_scope="own",
            data_scope="road",
            road_id=default_road.id,
            token_version=7,
        )
        db.add(test_account)
        db.commit()
        account_id = test_account.id

        bootstrap_database(db)
        refreshed = db.get(Account, account_id)
        assert refreshed.account_type == "station"
        assert refreshed.data_scope == "own"
        assert refreshed.stats_scope == "own"
        assert refreshed.road_id is None
        assert refreshed.token_version == 7


def test_test_accounts_are_managed_only_by_global_admin_without_roads(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    assert client.get("/api/v1/admin/roads", headers=admin_headers).json() == []
    manager_row = _create_road_manager(
        client,
        admin_headers,
        username="test_scope_manager",
        road_name="测试路段管理员",
    )
    road = {"id": manager_row["road_id"], "name": manager_row["road_name"]}
    manager = _activate(
        client,
        "test_scope_manager",
        device=221,
        password="Manager!23456",
    )
    manager_headers = auth_header(manager)

    direct_test = _create_account(
        client,
        admin_headers,
        username="direct_test_account",
        display_name="直属测试账号",
        account_type="station",
        is_test=True,
        data_scope="all",
    )
    assert direct_test["is_test"] is True
    assert direct_test["road_id"] is None
    assert direct_test["road_name"] is None
    assert direct_test["data_scope"] == "all"
    assert direct_test["id"] in {
        row["id"]
        for row in client.get(
            "/api/v1/admin/accounts",
            headers=admin_headers,
        ).json()
    }
    assert direct_test["id"] not in {
        row["id"]
        for row in client.get(
            "/api/v1/admin/accounts",
            headers=manager_headers,
        ).json()
    }

    manager_create = client.post(
        "/api/v1/admin/accounts",
        headers=manager_headers,
        json={
            "username": "manager_test_forbidden",
            "display_name": "越权测试账号",
            "account_type": "station",
            "is_test": True,
            "data_scope": "own",
        },
    )
    assert manager_create.status_code == 403
    assert manager_create.json()["code"] == "test_account_not_allowed"
    manager_update = client.patch(
        f"/api/v1/admin/accounts/{direct_test['id']}",
        headers=manager_headers,
        json={"display_name": "越权修改"},
    )
    assert manager_update.status_code == 403
    assert manager_update.json()["code"] == "account_outside_management_scope"

    assign_road = client.patch(
        f"/api/v1/admin/accounts/{direct_test['id']}",
        headers=admin_headers,
        json={"road_id": road["id"]},
    )
    assert assign_road.status_code == 422
    assert assign_road.json()["code"] == "test_account_has_no_road"

    station = _create_account(
        client,
        admin_headers,
        username="station_becomes_test",
        display_name="转测试账号",
        account_type="station",
        data_scope="road",
        road_id=road["id"],
    )
    converted = client.patch(
        f"/api/v1/admin/accounts/{station['id']}",
        headers=admin_headers,
        json={"is_test": True},
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["is_test"] is True
    assert converted.json()["road_id"] is None
    assert converted.json()["data_scope"] == "own"
    assert manager_row["id"] != direct_test["id"]


def test_road_manager_can_only_manage_own_road_stations(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    manager_row = _create_road_manager(
        client,
        admin_headers,
        username="guangshen_manager",
        road_name=DEFAULT_ROAD_NAME,
    )
    guangshen = next(
        road
        for road in client.get(
            "/api/v1/admin/roads",
            headers=admin_headers,
        ).json()
        if road["id"] == manager_row["road_id"]
    )
    other_manager = _create_road_manager(
        client,
        admin_headers,
        username="other_manager",
        road_name="京港澳高速",
    )
    assert manager_row["role"] == "user"
    assert manager_row["stats_scope"] == "own"
    assert manager_row["data_scope"] == "road"
    assert manager_row["road_name"] == DEFAULT_ROAD_NAME

    manager = _activate(
        client,
        "guangshen_manager",
        device=201,
        password="Manager!23456",
    )
    manager_headers = auth_header(manager)
    listed = client.get("/api/v1/admin/accounts", headers=manager_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    assert manager_row["id"] not in {row["id"] for row in listed.json()}
    assert other_manager["id"] not in {row["id"] for row in listed.json()}
    assert client.get("/api/v1/admin/roads", headers=manager_headers).json() == [
        guangshen
    ]

    station = _create_account(
        client,
        manager_headers,
        username="manager_station",
        display_name="路段新站",
        account_type="station",
        data_scope="road",
        road_id=guangshen["id"],
    )
    assert station["road_id"] == guangshen["id"]
    assert station["data_scope"] == "road"

    globally_expanded = client.patch(
        f"/api/v1/admin/accounts/{station['id']}",
        headers=admin_headers,
        json={"data_scope": "all"},
    )
    assert globally_expanded.status_code == 200, globally_expanded.text
    renamed_without_scope_change = client.patch(
        f"/api/v1/admin/accounts/{station['id']}",
        headers=manager_headers,
        json={"display_name": "路段新站（保留全部范围）"},
    )
    assert renamed_without_scope_change.status_code == 200
    assert renamed_without_scope_change.json()["data_scope"] == "all"
    narrowed_by_manager = client.patch(
        f"/api/v1/admin/accounts/{station['id']}",
        headers=manager_headers,
        json={"data_scope": "road"},
    )
    assert narrowed_by_manager.status_code == 200

    forbidden_scope = client.patch(
        f"/api/v1/admin/accounts/{station['id']}",
        headers=manager_headers,
        json={"data_scope": "all"},
    )
    assert forbidden_scope.status_code == 403
    assert forbidden_scope.json()["code"] == "data_scope_not_allowed"

    other_station = _create_account(
        client,
        admin_headers,
        username="other_station",
        display_name="其他路段站",
        account_type="station",
        data_scope="own",
        road_id=other_manager["road_id"],
    )
    outside = client.patch(
        f"/api/v1/admin/accounts/{other_station['id']}",
        headers=manager_headers,
        json={"display_name": "越权修改"},
    )
    assert outside.status_code == 403
    assert outside.json()["code"] == "account_outside_management_scope"
    manager_target = client.patch(
        f"/api/v1/admin/accounts/{manager_row['id']}",
        headers=manager_headers,
        json={"display_name": "越权修改"},
    )
    assert manager_target.status_code == 403

    wrong_road_create = client.post(
        "/api/v1/admin/accounts",
        headers=manager_headers,
        json={
            "username": "wrong_road",
            "display_name": "错误路段",
            "account_type": "station",
            "data_scope": "own",
            "road_id": other_manager["road_id"],
        },
    )
    assert wrong_road_create.status_code == 403
    assert wrong_road_create.json()["code"] == "road_assignment_not_allowed"

    archived_other = client.post(
        f"/api/v1/admin/accounts/{other_station['id']}/archive",
        headers=admin_headers,
    )
    assert archived_other.status_code == 200
    deleted_other = client.delete(
        f"/api/v1/admin/accounts/{other_station['id']}",
        headers=admin_headers,
    )
    assert deleted_other.status_code == 204
    manager_changes = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=manager_headers,
    ).json()["changes"]
    outside_tombstone = next(
        row
        for row in manager_changes
        if row["kind"] == "account"
        and row["operation"] == "delete"
        and row["entity_id"] == other_station["id"]
    )
    assert outside_tombstone["payload"] == {}
    admin_changes = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=admin_headers,
    ).json()["changes"]
    global_tombstone = next(
        row
        for row in admin_changes
        if row["kind"] == "account"
        and row["operation"] == "delete"
        and row["entity_id"] == other_station["id"]
    )
    assert global_tombstone["payload"]["username"] == "other_station"

    no_machine_learning = client.get(
        "/api/v1/admin/ml/overview",
        headers=manager_headers,
    )
    assert no_machine_learning.status_code == 403
    assert no_machine_learning.json()["code"] == "admin_required"

    reset = client.post(
        f"/api/v1/admin/accounts/{station['id']}/reset-password",
        headers=manager_headers,
    )
    assert reset.status_code == 204
    archived = client.post(
        f"/api/v1/admin/accounts/{station['id']}/archive",
        headers=manager_headers,
    )
    assert archived.status_code == 200
    restored = client.post(
        f"/api/v1/admin/accounts/{station['id']}/restore",
        headers=manager_headers,
    )
    assert restored.status_code == 200


def test_deleting_last_road_manager_releases_stations_until_manual_reassignment(
    client,
):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    manager = _create_road_manager(
        client,
        admin_headers,
        username="release_manager",
        road_name=DEFAULT_ROAD_NAME,
    )
    road_id = manager["road_id"]
    own_station = _create_account(
        client,
        admin_headers,
        username="release_own",
        display_name="本人范围中心站",
        account_type="station",
        data_scope="own",
        road_id=road_id,
    )
    road_station = _create_account(
        client,
        admin_headers,
        username="release_road",
        display_name="路段范围中心站",
        account_type="station",
        data_scope="road",
        road_id=road_id,
    )
    all_station = _create_account(
        client,
        admin_headers,
        username="release_all",
        display_name="全部范围中心站",
        account_type="station",
        data_scope="all",
        road_id=road_id,
    )

    archived = client.post(
        f"/api/v1/admin/accounts/{manager['id']}/archive",
        headers=admin_headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["road_id"] == road_id
    assert {
        row["id"]
        for row in client.get(
            "/api/v1/admin/roads",
            headers=admin_headers,
        ).json()
    } == {road_id}

    deleted = client.delete(
        f"/api/v1/admin/accounts/{manager['id']}",
        headers=admin_headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/admin/roads", headers=admin_headers).json() == []
    rows = {
        row["id"]: row
        for row in client.get(
            "/api/v1/admin/accounts",
            headers=admin_headers,
        ).json()
    }
    assert manager["id"] not in rows
    for station in (own_station, road_station, all_station):
        assert rows[station["id"]]["road_id"] is None
        assert rows[station["id"]]["road_name"] is None
    assert rows[own_station["id"]]["data_scope"] == "own"
    assert rows[road_station["id"]]["data_scope"] == "own"
    assert rows[road_station["id"]]["stats_scope"] == "own"
    assert rows[all_station["id"]]["data_scope"] == "all"

    direct_update = client.patch(
        f"/api/v1/admin/accounts/{own_station['id']}",
        headers=admin_headers,
        json={"display_name": "直属管理员中心站"},
    )
    assert direct_update.status_code == 200, direct_update.text
    with SessionLocal() as db:
        assert db.get(Road, uuid.UUID(road_id)) is None

    replacement = _create_road_manager(
        client,
        admin_headers,
        username="release_manager_replacement",
        road_name=DEFAULT_ROAD_NAME,
    )
    assert replacement["road_id"] != road_id
    replacement_bundle = _activate(
        client,
        "release_manager_replacement",
        device=251,
        password="Replacement!23456",
    )
    replacement_headers = auth_header(replacement_bundle)
    assert client.get(
        "/api/v1/admin/accounts",
        headers=replacement_headers,
    ).json() == []

    reassigned = client.patch(
        f"/api/v1/admin/accounts/{own_station['id']}",
        headers=admin_headers,
        json={"road_id": replacement["road_id"]},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["road_id"] == replacement["road_id"]
    assert {
        row["id"]
        for row in client.get(
            "/api/v1/admin/accounts",
            headers=replacement_headers,
        ).json()
    } == {own_station["id"]}
    cleared = client.patch(
        f"/api/v1/admin/accounts/{own_station['id']}",
        headers=admin_headers,
        json={"road_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["road_id"] is None


def test_road_manager_display_name_owns_and_renames_the_category(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    manager = _create_road_manager(
        client,
        admin_headers,
        username="rename_manager",
        road_name=DEFAULT_ROAD_NAME,
    )
    station = _create_account(
        client,
        admin_headers,
        username="rename_station",
        display_name="跟随重命名中心站",
        account_type="station",
        data_scope="own",
        road_id=manager["road_id"],
    )

    renamed = client.patch(
        f"/api/v1/admin/accounts/{manager['id']}",
        headers=admin_headers,
        json={"display_name": "广深高速北段"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["road_id"] == manager["road_id"]
    assert renamed.json()["road_name"] == "广深高速北段"
    assert client.get(
        "/api/v1/admin/roads",
        headers=admin_headers,
    ).json() == [{"id": manager["road_id"], "name": "广深高速北段"}]
    station_row = next(
        row
        for row in client.get(
            "/api/v1/admin/accounts",
            headers=admin_headers,
        ).json()
        if row["id"] == station["id"]
    )
    assert station_row["road_name"] == "广深高速北段"

    duplicate = client.post(
        "/api/v1/admin/accounts",
        headers=admin_headers,
        json={
            "username": "duplicate_manager",
            "display_name": "广深高速北段",
            "account_type": "road_admin",
            "data_scope": "road",
            "road_name": "广深高速北段",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "road_manager_exists"


def test_legacy_account_create_without_road_stays_unassigned(client):
    admin = changed_admin(client)
    response = client.post(
        "/api/v1/admin/accounts",
        headers=auth_header(admin),
        json={
            "username": "legacy_unassigned",
            "display_name": "旧版新建中心站",
            "role": "user",
            "stats_scope": "own",
            "device_limit": 10000,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["road_id"] is None
    assert response.json()["data_scope"] == "own"


def test_road_data_scope_and_legacy_client_downgrade(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    manager_row = _create_road_manager(
        client,
        admin_headers,
        username="scope_manager",
        road_name=DEFAULT_ROAD_NAME,
    )
    guangshen = next(
        road
        for road in client.get(
            "/api/v1/admin/roads",
            headers=admin_headers,
        ).json()
        if road["id"] == manager_row["road_id"]
    )
    other_manager = _create_road_manager(
        client,
        admin_headers,
        username="scope_other_manager",
        road_name="沿海高速",
    )
    same_station = _create_account(
        client,
        admin_headers,
        username="scope_same",
        display_name="同路段站",
        account_type="station",
        data_scope="own",
        road_id=guangshen["id"],
    )
    _create_account(
        client,
        admin_headers,
        username="scope_other",
        display_name="异路段站",
        account_type="station",
        data_scope="own",
        road_id=other_manager["road_id"],
    )

    manager = _activate(
        client,
        "scope_manager",
        device=301,
        password="ScopeManager!234",
    )
    same_bundle = _activate(
        client,
        "scope_same",
        device=302,
        password="ScopeSame!234",
    )
    other_bundle = _activate(
        client,
        "scope_other",
        device=303,
        password="ScopeOther!234",
    )
    same_item = _activity_item("same-road-event", 3)
    other_item = _activity_item("other-road-event", 5)
    assert client.post(
        "/api/v1/sync/push",
        headers=auth_header(same_bundle),
        json={"items": [same_item]},
    ).json()["items"][0]["status"] == "accepted"
    assert client.post(
        "/api/v1/sync/push",
        headers=auth_header(other_bundle),
        json={"items": [other_item]},
    ).json()["items"][0]["status"] == "accepted"

    road_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(manager),
    ).json()
    road_event_ids = {row["event_uid"] for row in road_snapshot["activity_events"]}
    assert road_snapshot["data_scope"] == "road"
    assert road_snapshot["stats_scope"] == "own"
    assert same_item["event_uid"] in road_event_ids
    assert other_item["event_uid"] not in road_event_ids

    moved_out = client.patch(
        f"/api/v1/admin/accounts/{same_station['id']}",
        headers=admin_headers,
        json={"road_id": other_manager["road_id"]},
    )
    assert moved_out.status_code == 200, moved_out.text
    membership_changes = client.get(
        f"/api/v1/sync/pull?after_revision={road_snapshot['revision']}",
        headers=auth_header(manager),
    ).json()["changes"]
    assert any(
        row["kind"] == "road_membership_changed"
        and row["payload"] == {"refresh_scope": True}
        for row in membership_changes
    )
    moved_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(manager),
    ).json()
    assert same_item["event_uid"] not in {
        row["event_uid"] for row in moved_snapshot["activity_events"]
    }
    moved_back = client.patch(
        f"/api/v1/admin/accounts/{same_station['id']}",
        headers=admin_headers,
        json={"road_id": guangshen["id"]},
    )
    assert moved_back.status_code == 200, moved_back.text
    returned_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(manager),
    ).json()
    assert same_item["event_uid"] in {
        row["event_uid"] for row in returned_snapshot["activity_events"]
    }

    legacy_manager = _login(
        client,
        "scope_manager",
        "ScopeManager!234",
        device=304,
        version="1.1.1",
    )
    legacy_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(legacy_manager),
    ).json()
    assert legacy_manager["account"]["role"] == "user"
    assert legacy_manager["account"]["stats_scope"] == "own"
    assert legacy_snapshot["stats_scope"] == "own"
    assert legacy_snapshot["data_scope"] == "own"
    assert same_item["event_uid"] not in {
        row["event_uid"] for row in legacy_snapshot["activity_events"]
    }

    omitted_version = client.post(
        "/api/v1/auth/login",
        json={
            "username": "scope_manager",
            "password": "ScopeManager!234",
            "device_uid": device_uid(306),
            "device_name": "hierarchy-missing-version",
        },
    )
    assert omitted_version.status_code == 200, omitted_version.text
    omitted_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(omitted_version.json()),
    ).json()
    assert omitted_snapshot["data_scope"] == "own"

    expanded = client.patch(
        f"/api/v1/admin/accounts/{manager_row['id']}",
        headers=admin_headers,
        json={"data_scope": "all"},
    )
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()["stats_scope"] == "all"
    expanded_login = _login(
        client,
        "scope_manager",
        "ScopeManager!234",
        device=305,
        version="1.2.0",
    )
    all_snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(expanded_login),
    ).json()
    assert other_item["event_uid"] in {
        row["event_uid"] for row in all_snapshot["activity_events"]
    }


def test_v110_admin_receives_legacy_safe_rows_for_new_account_types(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    manager = _create_road_manager(
        client,
        admin_headers,
        username="legacy_visible_manager",
        road_name="旧版可见路段管理员",
    )

    legacy = _login(
        client,
        "admin",
        "Admin!23456",
        device=401,
        version="1.1.0",
    )
    snapshot = client.get(
        "/api/v1/sync/snapshot",
        headers=auth_header(legacy),
    )
    assert snapshot.status_code == 200, snapshot.text
    manager_row = next(
        row for row in snapshot.json()["accounts"] if row["id"] == manager["id"]
    )
    assert manager_row["role"] == "user"
    assert manager_row["stats_scope"] == "own"

    pulled = client.get(
        "/api/v1/sync/pull?after_revision=0",
        headers=auth_header(legacy),
    )
    assert pulled.status_code == 200, pulled.text
    manager_change = next(
        row
        for row in pulled.json()["changes"]
        if row["kind"] == "account" and row["entity_id"] == manager["id"]
    )
    assert manager_change["payload"]["role"] == "user"
    assert manager_change["payload"]["stats_scope"] == "own"


def test_0014_migration_upgrades_legacy_accounts_in_place(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    accounts = sa.Table(
        "accounts",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("stats_scope", sa.String(8), nullable=False),
    )
    admin_id = uuid.uuid4()
    station_id = uuid.uuid4()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            accounts.insert(),
            [
                {
                    "id": admin_id,
                    "username": "admin",
                    "role": "admin",
                    "stats_scope": "all",
                },
                {
                    "id": station_id,
                    "username": "luogang",
                    "role": "user",
                    "stats_scope": "own",
                },
            ],
        )

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0014_road_account_hierarchy.py"
        )
        spec = spec_from_file_location("migration_0014", migration_path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "roads" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("accounts")}
        assert {"account_type", "data_scope", "road_id"} <= columns
        rows = {
            row.username: row
            for row in connection.execute(
                sa.text(
                    """
                    SELECT username, account_type, data_scope, road_id
                    FROM accounts
                    """
                )
            )
        }
        assert rows["admin"].account_type == "admin"
        assert rows["admin"].data_scope == "all"
        assert rows["admin"].road_id is None
        assert rows["luogang"].account_type == "station"
        assert rows["luogang"].data_scope == "own"
        assert rows["luogang"].road_id is not None
        road_name = connection.execute(
            sa.text("SELECT name FROM roads WHERE id=:id"),
            {"id": rows["luogang"].road_id},
        ).scalar_one()
        assert road_name == DEFAULT_ROAD_NAME


def test_0015_migration_detaches_existing_test_accounts(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    roads = sa.Table(
        "roads",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
    )
    accounts = sa.Table(
        "accounts",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("account_type", sa.String(20), nullable=False),
        sa.Column("stats_scope", sa.String(8), nullable=False),
        sa.Column("data_scope", sa.String(8), nullable=False),
        sa.Column("road_id", sa.Uuid(), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
    )
    road_id = uuid.uuid4()
    test_id = uuid.uuid4()
    station_id = uuid.uuid4()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            roads.insert(),
            {"id": road_id, "name": DEFAULT_ROAD_NAME},
        )
        connection.execute(
            accounts.insert(),
            [
                {
                    "id": test_id,
                    "role": "user",
                    "is_test": True,
                    "account_type": "station",
                    "stats_scope": "own",
                    "data_scope": "road",
                    "road_id": road_id,
                    "token_version": 9,
                },
                {
                    "id": station_id,
                    "role": "user",
                    "is_test": False,
                    "account_type": "station",
                    "stats_scope": "own",
                    "data_scope": "road",
                    "road_id": road_id,
                    "token_version": 5,
                },
            ],
        )

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0015_detach_test_accounts_from_roads.py"
        )
        spec = spec_from_file_location("migration_0015", migration_path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()
        rows = {
            uuid.UUID(str(row.id)): row
            for row in connection.execute(
                sa.text(
                    """
                    SELECT id, is_test, account_type, stats_scope, data_scope,
                           road_id, token_version
                    FROM accounts
                    """
                )
            )
        }
        assert rows[test_id].road_id is None
        assert rows[test_id].account_type == "station"
        assert rows[test_id].stats_scope == "own"
        assert rows[test_id].data_scope == "own"
        assert rows[test_id].token_version == 9
        assert rows[station_id].road_id is not None
        assert rows[station_id].token_version == 5

        migration.downgrade()
        restored_road_id = connection.execute(
            sa.select(accounts.c.road_id).where(accounts.c.id == test_id)
        ).scalar_one()
        assert restored_road_id is not None
