"""Add authorized CAPTCHA learning policy, samples, attempts, and models.

Revision ID: 0005_captcha_learning
Revises: 0004_connection_test_devices
Create Date: 2026-07-30
"""

from datetime import UTC, datetime

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0005_captcha_learning"
down_revision = "0004_connection_test_devices"
branch_labels = None
depends_on = None


TABLES = (
    "captcha_learning_policy",
    "captcha_attempts",
    "captcha_samples",
    "captcha_models",
)


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in TABLES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)
    policy_table = Base.metadata.tables["captcha_learning_policy"]
    existing_policy = bind.execute(
        policy_table.select().where(policy_table.c.id == 1)
    ).first()
    if existing_policy is None:
        bind.execute(
            policy_table.insert().values(
                id=1,
                upload_enabled=False,
                revision=1,
                updated_at=datetime.now(UTC),
            )
        )


def downgrade() -> None:
    for table_name in reversed(TABLES):
        op.drop_table(table_name)
