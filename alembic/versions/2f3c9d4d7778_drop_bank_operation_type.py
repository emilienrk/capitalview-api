"""drop the payment method read off bank labels

The label already says "VIR…" or "CB…", nothing was decided on it, and the
lexicon reading it was bank-specific (docs/bank-sorting.md).

Revision ID: 2f3c9d4d7778
Revises: a0eb6eed48b9
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f3c9d4d7778"
down_revision: Union[str, None] = "a0eb6eed48b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("bank_transactions", "operation_type_enc")


def downgrade() -> None:
    # Comes back empty: nothing reads it any more to fill it again.
    op.add_column("bank_transactions", sa.Column("operation_type_enc", sa.TEXT(), nullable=True))
