"""Add replyable contact conversations and per-user reply receipts.

Revision ID: 0009_contact_conversations
Revises: 0008_retire_knn_captcha_models
Create Date: 2026-08-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_contact_conversations"
down_revision = "0008_retire_knn_captcha_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "admin_contact_messages" not in set(inspector.get_table_names()):
        return
    columns = {
        column["name"] for column in inspector.get_columns("admin_contact_messages")
    }
    with op.batch_alter_table("admin_contact_messages") as batch:
        if "thread_id" not in columns:
            batch.add_column(sa.Column("thread_id", sa.Uuid(), nullable=True))
        if "status" not in columns:
            batch.add_column(
                sa.Column(
                    "status",
                    sa.String(length=16),
                    nullable=False,
                    server_default="open",
                )
            )
        if "read_by_user_at" not in columns:
            batch.add_column(
                sa.Column("read_by_user_at", sa.DateTime(timezone=True), nullable=True)
            )

    index_names = {
        index["name"] for index in sa.inspect(bind).get_indexes("admin_contact_messages")
    }
    if "ix_admin_contact_messages_thread_id" not in index_names:
        op.create_index(
            "ix_admin_contact_messages_thread_id",
            "admin_contact_messages",
            ["thread_id"],
        )
    if "ix_admin_contact_messages_status" not in index_names:
        op.create_index(
            "ix_admin_contact_messages_status",
            "admin_contact_messages",
            ["status"],
        )
    if "ix_admin_messages_thread_created" not in index_names:
        op.create_index(
            "ix_admin_messages_thread_created",
            "admin_contact_messages",
            ["thread_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "admin_contact_messages" not in set(sa.inspect(bind).get_table_names()):
        return
    index_names = {
        index["name"] for index in sa.inspect(bind).get_indexes("admin_contact_messages")
    }
    for name in (
        "ix_admin_messages_thread_created",
        "ix_admin_contact_messages_status",
        "ix_admin_contact_messages_thread_id",
    ):
        if name in index_names:
            op.drop_index(name, table_name="admin_contact_messages")
    with op.batch_alter_table("admin_contact_messages") as batch:
        batch.drop_column("read_by_user_at")
        batch.drop_column("status")
        batch.drop_column("thread_id")
