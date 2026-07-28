"""Add targeted announcements, attachments, receipts, and admin messages.

Revision ID: 0003_announcements_and_messages
Revises: 0002_public_multi_device_access
Create Date: 2026-07-28
"""

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0003_announcements_and_messages"
down_revision = "0002_public_multi_device_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "announcements",
        "announcement_targets",
        "announcement_attachments",
        "announcement_receipts",
        "admin_contact_messages",
    ):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    for table_name in (
        "admin_contact_messages",
        "announcement_receipts",
        "announcement_attachments",
        "announcement_targets",
        "announcements",
    ):
        op.drop_table(table_name)
