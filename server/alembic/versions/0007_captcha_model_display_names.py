"""Add editable display names for CAPTCHA models.

Revision ID: 0007_captcha_model_display_names
Revises: 0006_captcha_upload_modes
Create Date: 2026-08-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_captcha_model_display_names"
down_revision = "0006_captcha_upload_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("captcha_models")
    }
    if "display_name" not in columns:
        op.add_column(
            "captcha_models",
            sa.Column("display_name", sa.String(length=80), nullable=True),
        )
    bind.execute(
        sa.text(
            """
            UPDATE captcha_models
            SET display_name = version
            WHERE display_name IS NULL OR TRIM(display_name) = ''
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("captcha_models")
    }
    if "display_name" in columns:
        op.drop_column("captcha_models", "display_name")
