"""add analysis_hidden_sections to user_settings

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-09-06
"""
from alembic import op
import sqlalchemy as sa

revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("analysis_hidden_sections", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "analysis_hidden_sections")
