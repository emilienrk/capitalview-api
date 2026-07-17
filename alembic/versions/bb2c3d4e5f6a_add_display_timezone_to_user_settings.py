"""add display_timezone to user_settings

Revision ID: bb2c3d4e5f6a
Revises: aa1b2c3d4e5f
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "bb2c3d4e5f6a"
down_revision = "aa1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("display_timezone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "display_timezone")
