"""add cumulative pnl, fees and dividends to account history

Revision ID: r9s0t1u2v3w4
Revises: a2088aa4e15f
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa


revision = "r9s0t1u2v3w4"
down_revision = "a2088aa4e15f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_history", sa.Column("cumulative_pnl_enc", sa.Text(), nullable=True))
    op.add_column("account_history", sa.Column("total_fees_enc", sa.Text(), nullable=True))
    op.add_column("account_history", sa.Column("total_dividends_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("account_history", "total_dividends_enc")
    op.drop_column("account_history", "total_fees_enc")
    op.drop_column("account_history", "cumulative_pnl_enc")
