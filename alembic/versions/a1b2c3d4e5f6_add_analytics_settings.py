"""add benchmark_asset_key and investment_plan_enc to user_settings

Revision ID: a1b2c3d4e5f6
Revises: ff6a7b8c9d0e
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "ff6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("benchmark_asset_key", sa.TEXT(), nullable=True))
    op.add_column("user_settings", sa.Column("investment_plan_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "investment_plan_enc")
    op.drop_column("user_settings", "benchmark_asset_key")
