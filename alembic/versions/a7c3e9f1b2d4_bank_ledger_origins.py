"""operations as the source of truth of unsynced bank accounts

Adds the origin of an operation, the marker of an account converted to the
operations ledger, and turns the recurring forecasts off for everyone
(docs/bank-ledger.md).

Revision ID: a7c3e9f1b2d4
Revises: 98762d047d7a
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e9f1b2d4"
down_revision: Union[str, None] = "98762d047d7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bank_transactions", sa.Column("origin_enc", sa.TEXT(), nullable=True))
    op.add_column("bank_accounts", sa.Column("ledger_version", sa.Integer(), nullable=True))
    op.execute("UPDATE user_settings SET bank_auto_sync_enabled = false")
    op.alter_column("user_settings", "bank_auto_sync_enabled", server_default=sa.false())


def downgrade() -> None:
    op.drop_column("bank_accounts", "ledger_version")
    op.drop_column("bank_transactions", "origin_enc")
