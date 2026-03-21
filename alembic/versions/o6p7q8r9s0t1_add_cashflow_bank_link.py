"""add bank_account link to cashflows and balance_updated_at to bank_accounts

Revision ID: o6p7q8r9s0t1
Revises: n5o6p7q8r9s0
Create Date: 2026-03-21
"""
from alembic import op
import sqlalchemy as sa

revision = "o6p7q8r9s0t1"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Blind index to link a cashflow to a specific bank account (queryable without decryption)
    op.add_column(
        "cashflows",
        sa.Column("bank_account_uuid_bidx", sa.Text(), nullable=True, index=True),
    )
    # Track the last date the bank account balance was auto-updated via cashflows
    op.add_column(
        "bank_accounts",
        sa.Column("balance_updated_at", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cashflows", "bank_account_uuid_bidx")
    op.drop_column("bank_accounts", "balance_updated_at")
