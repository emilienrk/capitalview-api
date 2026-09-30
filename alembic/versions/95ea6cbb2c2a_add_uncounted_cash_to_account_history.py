"""add uncounted cash to account history

Revision ID: 95ea6cbb2c2a
Revises: e7a1c3d5f9b2
Create Date: 2026-09-30 08:22:53.037399

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '95ea6cbb2c2a'
down_revision: Union[str, None] = 'e7a1c3d5f9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account_history", sa.Column("uncounted_cash_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("account_history", "uncounted_cash_enc")
