"""add history_confirmed_on to bank_accounts

NULL on existing accounts: nothing was vouched for yet.

Revision ID: 3b7e1c9d2a40
Revises: 80ffa53951ed
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa

revision = "3b7e1c9d2a40"
down_revision = "80ffa53951ed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("history_confirmed_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_accounts", "history_confirmed_on")
