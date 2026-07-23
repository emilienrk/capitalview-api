"""add calc_version to account_history

Revision ID: dd4e5f6a7b8c
Revises: cc3d4e5f6a7b
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "dd4e5f6a7b8c"
down_revision = "cc3d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default "0" backfills every existing row to legacy version 0
    # without a separate data pass.
    op.add_column(
        "account_history",
        sa.Column("calc_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("account_history", "calc_version")
