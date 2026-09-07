"""bank_auto_sync_enabled now defaults to off

Only the default for accounts created from here on: an existing row keeps
whatever the user has set, on or off.

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa

revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("user_settings", "bank_auto_sync_enabled", server_default=sa.false())


def downgrade() -> None:
    op.alter_column("user_settings", "bank_auto_sync_enabled", server_default=sa.true())
