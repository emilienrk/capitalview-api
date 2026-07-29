"""add bank_auto_sync_enabled to user_settings

Revision ID: ff6a7b8c9d0e
Revises: ee5f6a7b8c9d
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "ff6a7b8c9d0e"
down_revision = "ee5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("bank_auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "bank_auto_sync_enabled")
