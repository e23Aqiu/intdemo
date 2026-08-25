"""Add road administrators and hierarchical statistics scopes.

Revision ID: 0014_road_account_hierarchy
Revises: 0013_yellow_vehicle_statistics
Create Date: 2026-08-25
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0014_road_account_hierarchy"
down_revision = "0013_yellow_vehicle_statistics"
branch_labels = None
depends_on = None

DEFAULT_ROAD_NAME = "广深高速"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "roads" not in tables:
        op.create_table(
            "roads",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_roads_name", "roads", ["name"], unique=True)

    account_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("accounts")
    }
    with op.batch_alter_table("accounts") as batch:
        if "account_type" not in account_columns:
            batch.add_column(
                sa.Column(
                    "account_type",
                    sa.String(length=20),
                    nullable=False,
                    server_default="station",
                )
            )
        if "data_scope" not in account_columns:
            batch.add_column(
                sa.Column(
                    "data_scope",
                    sa.String(length=8),
                    nullable=False,
                    server_default="own",
                )
            )
        if "road_id" not in account_columns:
            batch.add_column(sa.Column("road_id", sa.Uuid(), nullable=True))

    default_road_id = bind.execute(
        sa.text("SELECT id FROM roads WHERE name=:name"),
        {"name": DEFAULT_ROAD_NAME},
    ).scalar_one_or_none()
    if default_road_id is None:
        default_road_id = uuid.uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO roads(id, name, created_at, updated_at)
                VALUES (:id, :name, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
            {"id": default_road_id, "name": DEFAULT_ROAD_NAME},
        )
    else:
        default_road_id = uuid.UUID(str(default_road_id))

    bind.execute(
        sa.text(
            """
            UPDATE accounts
            SET account_type=CASE WHEN role='admin' THEN 'admin' ELSE 'station' END,
                data_scope=CASE
                    WHEN role='admin' OR stats_scope='all' THEN 'all'
                    ELSE 'own'
                END,
                stats_scope=CASE
                    WHEN role='admin' OR stats_scope='all' THEN 'all'
                    ELSE 'own'
                END,
                road_id=CASE WHEN role='admin' THEN NULL ELSE :road_id END
            """
        ).bindparams(sa.bindparam("road_id", type_=sa.Uuid())),
        {"road_id": default_road_id},
    )

    inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("accounts")}
    foreign_keys = inspector.get_foreign_keys("accounts")
    has_road_foreign_key = any(
        key.get("referred_table") == "roads"
        and key.get("constrained_columns") == ["road_id"]
        for key in foreign_keys
    )
    with op.batch_alter_table("accounts") as batch:
        if "ix_accounts_road_id" not in index_names:
            batch.create_index("ix_accounts_road_id", ["road_id"], unique=False)
        if not has_road_foreign_key:
            batch.create_foreign_key(
                "fk_accounts_road_id_roads",
                "roads",
                ["road_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "accounts" in tables:
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns("accounts")}
        index_names = {index["name"] for index in inspector.get_indexes("accounts")}
        foreign_key_names = {
            key.get("name") for key in inspector.get_foreign_keys("accounts")
        }
        with op.batch_alter_table("accounts") as batch:
            if "fk_accounts_road_id_roads" in foreign_key_names:
                batch.drop_constraint("fk_accounts_road_id_roads", type_="foreignkey")
            if "ix_accounts_road_id" in index_names:
                batch.drop_index("ix_accounts_road_id")
            for column_name in ("road_id", "data_scope", "account_type"):
                if column_name in columns:
                    batch.drop_column(column_name)
    if "roads" in tables:
        op.drop_index("ix_roads_name", table_name="roads")
        op.drop_table("roads")
