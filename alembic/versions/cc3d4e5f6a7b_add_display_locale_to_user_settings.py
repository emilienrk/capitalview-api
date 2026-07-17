"""add display_locale to user_settings

Revision ID: cc3d4e5f6a7b
Revises: bb2c3d4e5f6a
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "cc3d4e5f6a7b"
down_revision = "bb2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("display_locale", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "display_locale")
