"""sorting rules written by hand: contains or starts with, one account or all

Revision ID: d1e2f3a4b5c6
Revises: 4ca577dfa649
Create Date: 2026-10-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "4ca577dfa649"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bank_type_rules", sa.Column("match_enc", sa.TEXT(), nullable=True))
    op.add_column("bank_type_rules", sa.Column("pattern_enc", sa.TEXT(), nullable=True))
    op.alter_column("bank_type_rules", "account_ref_enc", existing_type=sa.TEXT(), nullable=True)


def downgrade() -> None:
    # The rules written by hand have no place in the older shape.
    op.execute("DELETE FROM bank_type_rules WHERE match_enc IS NOT NULL")
    op.alter_column("bank_type_rules", "account_ref_enc", existing_type=sa.TEXT(), nullable=False)
    op.drop_column("bank_type_rules", "pattern_enc")
    op.drop_column("bank_type_rules", "match_enc")
