"""add interest rate, boosted rate and interest method to bank_accounts

All NULL on existing accounts: a rate is the user's to enter, and without one
no interest is estimated.

Revision ID: 80ffa53951ed
Revises: c1d2e3f4a5b6
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa

revision = "80ffa53951ed"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("interest_rate_enc", sa.TEXT(), nullable=True))
    op.add_column("bank_accounts", sa.Column("boosted_rate_enc", sa.TEXT(), nullable=True))
    op.add_column("bank_accounts", sa.Column("boosted_until", sa.Date(), nullable=True))
    op.add_column("bank_accounts", sa.Column("interest_method_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_accounts", "interest_method_enc")
    op.drop_column("bank_accounts", "boosted_until")
    op.drop_column("bank_accounts", "boosted_rate_enc")
    op.drop_column("bank_accounts", "interest_rate_enc")
