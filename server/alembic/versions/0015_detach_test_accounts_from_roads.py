"""Detach test accounts from the road hierarchy.

Revision ID: 0015_detach_test_accounts
Revises: 0014_road_account_hierarchy
Create Date: 2026-08-25
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0015_detach_test_accounts"
down_revision = "0014_road_account_hierarchy"
branch_labels = None
depends_on = None

DEFAULT_ROAD_NAME = "广深高速"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "accounts" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("accounts")}
    if not {
        "is_test",
        "road_id",
        "account_type",
        "data_scope",
        "stats_scope",
    } <= columns:
        return
    bind.execute(
        sa.text(
            """
            UPDATE accounts
            SET road_id=NULL,
                account_type='station',
                data_scope=CASE
                    WHEN data_scope='all' OR stats_scope='all' THEN 'all'
                    ELSE 'own'
                END,
                stats_scope=CASE
                    WHEN data_scope='all' OR stats_scope='all' THEN 'all'
                    ELSE 'own'
                END
            WHERE is_test=TRUE
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"accounts", "roads"} <= tables:
        return
    default_road_id = bind.execute(
        sa.text("SELECT id FROM roads WHERE name=:name"),
        {"name": DEFAULT_ROAD_NAME},
    ).scalar_one_or_none()
    if default_road_id is None:
        return
    bind.execute(
        sa.text(
            """
            UPDATE accounts
            SET road_id=:road_id
            WHERE is_test=TRUE AND role<>'admin' AND road_id IS NULL
            """
        ).bindparams(sa.bindparam("road_id", type_=sa.Uuid())),
        {"road_id": uuid.UUID(str(default_road_id))},
    )
