"""Retire active legacy KNN CAPTCHA models without deleting history.

Revision ID: 0008_retire_knn_captcha_models
Revises: 0007_captcha_model_display_names
Create Date: 2026-08-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_retire_knn_captcha_models"
down_revision = "0007_captcha_model_display_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve every artifact, metric and audit-relevant field.  Only stop an
    # active legacy model from being distributed after a v1.1.0 deployment.
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "captcha_models" not in tables:
        return
    bind.execute(
        sa.text(
            """
            UPDATE captcha_models
            SET status = 'archived'
            WHERE status = 'current' AND algorithm = 'knn-pixels-v1'
            """
        )
    )


def downgrade() -> None:
    # Archiving is intentionally irreversible: restoring "current" could
    # conflict with a newer active model and reactivate a retired algorithm.
    pass

