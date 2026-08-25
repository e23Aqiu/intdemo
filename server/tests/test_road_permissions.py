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


def test_bootstrap_assigns_existing_accounts_without_revoking_sessions(client):
    admin = changed_admin(client)
    headers = auth_header(admin)

    with SessionLocal() as db:
        road = db.scalar(select(Road).where(Road.name == DEFAULT_ROAD_NAME))
        admin_row = db.scalar(select(Account).where(Account.username == "admin"))
        station = db.scalar(select(Account).where(Account.username == "luogang"))
        assert road is not None
        assert admin_row.account_type == "admin"
        assert admin_row.data_scope == "all"
        assert admin_row.road_id is None
        assert station.account_type == "station"
        assert station.data_scope == "own"
        assert station.road_id == road.id
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
        default_road = db.scalar(
            select(Road).where(Road.name == DEFAULT_ROAD_NAME)
        )
        assert legacy_all.account_type == "station"
        assert legacy_all.data_scope == "all"
        assert legacy_all.road_id == default_road.id
        assert legacy_demoted.account_type == "station"
        assert legacy_demoted.data_scope == "own"
        assert legacy_demoted.road_id == default_road.id
        assert legacy_promoted.account_type == "admin"
        assert legacy_promoted.data_scope == "all"
        assert legacy_promoted.road_id is None


def test_road_manager_can_only_manage_own_road_stations(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    roads = client.get("/api/v1/admin/roads", headers=admin_headers).json()
    guangshen = next(road for road in roads if road["name"] == DEFAULT_ROAD_NAME)

    manager_row = _create_account(
        client,
        admin_headers,
        username="guangshen_manager",
        display_name="广深高速",
        account_type="road_admin",
        data_scope="road",
        road_id=guangshen["id"],
    )
    other_manager = _create_account(
        client,
        admin_headers,
        username="other_manager",
        display_name="京港澳高速",
        account_type="road_admin",
        data_scope="road",
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
    assert {row["account_type"] for row in listed.json()} == {"station"}
    assert {row["road_id"] for row in listed.json()} == {guangshen["id"]}
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


def test_road_data_scope_and_legacy_client_downgrade(client):
    admin = changed_admin(client)
    admin_headers = auth_header(admin)
    guangshen = client.get(
        "/api/v1/admin/roads",
        headers=admin_headers,
    ).json()[0]
    manager_row = _create_account(
        client,
        admin_headers,
        username="scope_manager",
        display_name="广深数据管理员",
        account_type="road_admin",
        data_scope="road",
        road_id=guangshen["id"],
    )
    other_manager = _create_account(
        client,
        admin_headers,
        username="scope_other_manager",
        display_name="沿海高速",
        account_type="road_admin",
        data_scope="road",
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
    road = client.get("/api/v1/admin/roads", headers=admin_headers).json()[0]
    manager = _create_account(
        client,
        admin_headers,
        username="legacy_visible_manager",
        display_name="旧版可见路段管理员",
        account_type="road_admin",
        data_scope="road",
        road_id=road["id"],
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
