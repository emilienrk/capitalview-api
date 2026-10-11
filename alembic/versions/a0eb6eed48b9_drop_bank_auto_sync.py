"""drop the recurring forecasts: no auto-sync setting, no cashflow tied to an account

A declared cashflow no longer writes anything on a bank account
(docs/bank-ledger.md). The forecast operations themselves cannot be told apart
here: their origin is encrypted with the user's master key.

Revision ID: a0eb6eed48b9
Revises: d1e2f3a4b5c6
Create Date: 2026-10-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a0eb6eed48b9"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("user_settings", "bank_auto_sync_enabled")
    op.drop_index("ix_cashflows_bank_account_uuid_bidx", table_name="cashflows")
    op.drop_column("cashflows", "bank_account_uuid_bidx")
    op.drop_column("cashflows", "is_active_enc")


def downgrade() -> None:
    # Links and paused flags are gone: every flow comes back unlinked and active.
    op.add_column("cashflows", sa.Column("is_active_enc", sa.TEXT(), nullable=True))
    op.add_column("cashflows", sa.Column("bank_account_uuid_bidx", sa.Text(), nullable=True))
    op.create_index("ix_cashflows_bank_account_uuid_bidx", "cashflows", ["bank_account_uuid_bidx"])
    op.add_column(
        "user_settings",
        sa.Column("bank_auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
