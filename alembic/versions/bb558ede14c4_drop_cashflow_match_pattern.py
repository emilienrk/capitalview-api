"""drop match_pattern from cashflows

Revision ID: bb558ede14c4
Revises: fb7c7e2f233b
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bb558ede14c4"
down_revision: Union[str, None] = "fb7c7e2f233b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The declared-versus-observed comparison it served is gone: the recurring
    # payments and income are found in the operations themselves.
    op.drop_column("cashflows", "match_pattern_enc")


def downgrade() -> None:
    op.add_column("cashflows", sa.Column("match_pattern_enc", sa.TEXT(), nullable=True))
