"""add bank_contribution_decisions

What the user said of an operation facing a deposit or a withdrawal declared
on an investment account (services/banking/contribution_decisions.py).

Revision ID: 7968262b9b90
Revises: 2f3c9d4d7778
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7968262b9b90"
down_revision: Union[str, None] = "2f3c9d4d7778"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bank_contribution_decisions",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("kind_enc", sa.TEXT(), nullable=False),
        sa.Column("transaction_ref_bidx", sa.TEXT(), nullable=False),
        sa.Column("account_bidx", sa.TEXT(), nullable=False),
        sa.Column("contribution_ref_bidx", sa.TEXT(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uuid"),
        sa.UniqueConstraint(
            "user_uuid_bidx", "transaction_ref_bidx", name="uq_bank_contribution_decisions_transaction"
        ),
    )
    op.create_index(
        op.f("ix_bank_contribution_decisions_user_uuid_bidx"),
        "bank_contribution_decisions", ["user_uuid_bidx"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bank_contribution_decisions_user_uuid_bidx"), table_name="bank_contribution_decisions")
    op.drop_table("bank_contribution_decisions")
