"""add bank_transactions

Revision ID: a7e3d9c1f204
Revises: 9b2d7f5e3c41
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7e3d9c1f204"
down_revision: Union[str, None] = "9b2d7f5e3c41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bank_transactions",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("account_id_bidx", sa.TEXT(), nullable=False),
        sa.Column("period_bidx", sa.TEXT(), nullable=False),
        sa.Column("entry_ref_bidx", sa.TEXT(), nullable=True),
        sa.Column("dedup_bidx", sa.TEXT(), nullable=False),
        sa.Column("amount_enc", sa.TEXT(), nullable=False),
        sa.Column("currency_enc", sa.TEXT(), nullable=False),
        sa.Column("credit_debit_enc", sa.TEXT(), nullable=False),
        sa.Column("status_enc", sa.TEXT(), nullable=False),
        sa.Column("booking_date_enc", sa.TEXT(), nullable=True),
        sa.Column("value_date_enc", sa.TEXT(), nullable=True),
        sa.Column("transaction_date_enc", sa.TEXT(), nullable=True),
        sa.Column("remittance_enc", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("uuid"),
        sa.UniqueConstraint(
            "account_id_bidx", "entry_ref_bidx", name="uq_bank_transactions_account_entry_ref"
        ),
    )
    op.create_index(
        op.f("ix_bank_transactions_account_id_bidx"),
        "bank_transactions",
        ["account_id_bidx"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bank_transactions_period_bidx"),
        "bank_transactions",
        ["period_bidx"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bank_transactions_dedup_bidx"),
        "bank_transactions",
        ["dedup_bidx"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bank_transactions_dedup_bidx"), table_name="bank_transactions")
    op.drop_index(op.f("ix_bank_transactions_period_bidx"), table_name="bank_transactions")
    op.drop_index(op.f("ix_bank_transactions_account_id_bidx"), table_name="bank_transactions")
    op.drop_table("bank_transactions")
