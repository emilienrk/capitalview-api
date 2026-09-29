"""add notes module setting

Off by default, except for users who already wrote notes: hiding them behind a
setting they never saw would look like losing them.

Revision ID: e7a1c3d5f9b2
Revises: d5e9f3a2b7c4
Create Date: 2026-09-29
"""
from alembic import op
import sqlalchemy as sa

revision = "e7a1c3d5f9b2"
down_revision = "d5e9f3a2b7c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("notes_module_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE user_settings SET notes_module_enabled = true "
        "WHERE user_uuid_bidx IN (SELECT DISTINCT user_uuid_bidx FROM notes)"
    )


def downgrade() -> None:
    op.drop_column("user_settings", "notes_module_enabled")
