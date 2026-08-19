"""Add the backward-compatible test-account marker.

Revision ID: 0011_test_accounts
Revises: 0010_contact_message_attachments
Create Date: 2026-08-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_test_accounts"
down_revision = "0010_contact_message_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("accounts")
    }
    if "is_test" not in columns:
        with op.batch_alter_table("accounts") as batch:
            batch.add_column(
                sa.Column(
                    "is_test",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("accounts")
    }
    if "is_test" in columns:
        with op.batch_alter_table("accounts") as batch:
            batch.drop_column("is_test")
