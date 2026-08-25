"""Store the yellow-plate subset for activity statistics.

Revision ID: 0013_yellow_vehicle_statistics
Revises: 0012_last_login_system
Create Date: 2026-08-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0013_yellow_vehicle_statistics"
down_revision = "0012_last_login_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("activity_events")
    }
    if "yellow_amount" not in columns:
        with op.batch_alter_table("activity_events") as batch:
            batch.add_column(sa.Column("yellow_amount", sa.Integer()))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("activity_events")
    }
    if "yellow_amount" in columns:
        with op.batch_alter_table("activity_events") as batch:
            batch.drop_column("yellow_amount")
