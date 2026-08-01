"""Add three-mode CAPTCHA upload policy.

Revision ID: 0006_captcha_upload_modes
Revises: 0005_captcha_learning
Create Date: 2026-07-31
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_captcha_upload_modes"
down_revision = "0005_captcha_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("captcha_learning_policy")
    }
    if "upload_mode" not in columns:
        op.add_column(
            "captcha_learning_policy",
            sa.Column(
                "upload_mode",
                sa.String(length=32),
                nullable=False,
                server_default="off",
            ),
        )
    bind.execute(
        sa.text(
            """
            UPDATE captcha_learning_policy
            SET upload_mode = CASE
                WHEN upload_enabled THEN 'samples_and_metrics'
                ELSE 'off'
            END
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("captcha_learning_policy")
    }
    if "upload_mode" in columns:
        bind.execute(
            sa.text(
                """
                UPDATE captcha_learning_policy
                SET upload_enabled = CASE
                    WHEN upload_mode = 'samples_and_metrics' THEN true
                    ELSE false
                END
                """
            )
        )
        op.drop_column("captcha_learning_policy", "upload_mode")
