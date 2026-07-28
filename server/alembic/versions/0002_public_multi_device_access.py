"""Enable built-in accounts and remove practical device-count restrictions.

Revision ID: 0002_public_multi_device_access
Revises: 0001_initial
Create Date: 2026-07-28
"""

from alembic import op

revision = "0002_public_multi_device_access"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE accounts
        SET device_limit = 10000,
            is_active = TRUE,
            entitlement_revision = entitlement_revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE is_archived = FALSE
          AND username IN (
              'admin',
              'luogang',
              'taiping',
              'daojiao',
              'baoan',
              'nantou'
          )
          AND (device_limit < 10000 OR is_active = FALSE)
        """
    )


def downgrade() -> None:
    # Account activation and registered devices are user data. Do not silently
    # disable accounts or reduce their capacity during a code rollback.
    pass
