"""add ui_style and ui_palette to user_settings

Existing users get "current", the look they already have.

Revision ID: c4d8e2f1a6b3
Revises: 3b7e1c9d2a40
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d8e2f1a6b3"
down_revision = "3b7e1c9d2a40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("ui_style", sa.String(), nullable=False, server_default="current"))
    op.add_column("user_settings", sa.Column("ui_palette", sa.String(), nullable=False, server_default="current"))


def downgrade() -> None:
    op.drop_column("user_settings", "ui_palette")
    op.drop_column("user_settings", "ui_style")
