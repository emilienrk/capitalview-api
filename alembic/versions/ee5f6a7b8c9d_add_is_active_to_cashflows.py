"""add is_active to cashflows

Revision ID: ee5f6a7b8c9d
Revises: dd4e5f6a7b8c
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "ee5f6a7b8c9d"
down_revision = "dd4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means active, so existing rows need no backfill.
    op.add_column("cashflows", sa.Column("is_active_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("cashflows", "is_active_enc")
