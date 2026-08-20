"""Record the operating system used for the latest account login.

Revision ID: 0012_last_login_system
Revises: 0011_test_accounts
Create Date: 2026-08-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_last_login_system"
down_revision = "0011_test_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("accounts")
    }
    if "last_login_system" not in columns:
        with op.batch_alter_table("accounts") as batch:
            batch.add_column(sa.Column("last_login_system", sa.String(80)))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("accounts")
    }
    if "last_login_system" in columns:
        with op.batch_alter_table("accounts") as batch:
            batch.drop_column("last_login_system")
