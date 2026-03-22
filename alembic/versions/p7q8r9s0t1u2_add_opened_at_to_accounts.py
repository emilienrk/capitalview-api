"""add opened_at to bank, stock and crypto accounts

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1
Create Date: 2026-03-22
"""
from alembic import op
import sqlalchemy as sa

revision = "p7q8r9s0t1u2"
down_revision = "o6p7q8r9s0t1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("opened_at", sa.Date(), nullable=True))
    op.add_column("stock_accounts", sa.Column("opened_at", sa.Date(), nullable=True))
    op.add_column("crypto_accounts", sa.Column("opened_at", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("crypto_accounts", "opened_at")
    op.drop_column("stock_accounts", "opened_at")
    op.drop_column("bank_accounts", "opened_at")
