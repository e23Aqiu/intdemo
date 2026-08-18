"""Add image and file attachments to contact messages.

Revision ID: 0010_contact_message_attachments
Revises: 0009_contact_conversations
Create Date: 2026-08-18
"""

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0010_contact_message_attachments"
down_revision = "0009_contact_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["contact_message_attachments"].create(
        bind=bind,
        checkfirst=True,
    )


def downgrade() -> None:
    op.drop_table("contact_message_attachments")
