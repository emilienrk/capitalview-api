"""add auto provision flag to stock and crypto transactions

Revision ID: 7b2e5f9c3a41
Revises: 6a1d4c2e9b7f
Create Date: 2026-09-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b2e5f9c3a41'
down_revision: Union[str, None] = '6a1d4c2e9b7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows stay false here: what tells them apart is their note or
    # their group, both of which need the master key a migration never has.
    # They are marked the first time the deposits are read
    # (services/banking/contributions.py).
    for table in ('stock_transactions', 'crypto_transactions'):
        op.add_column(
            table,
            sa.Column('is_auto_provision', sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    for table in ('stock_transactions', 'crypto_transactions'):
        op.drop_column(table, 'is_auto_provision')
