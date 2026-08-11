"""Mark connection-test control devices separately from business devices.

Revision ID: 0004_connection_test_devices
Revises: 0003_announcements_and_messages
Create Date: 2026-07-29
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_connection_test_devices"
down_revision = "0003_announcements_and_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("devices")
    }
    if "is_control_client" not in columns:
        op.add_column(
            "devices",
            sa.Column(
                "is_control_client",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("devices")
    }
    if "is_control_client" in columns:
        op.drop_column("devices", "is_control_client")
