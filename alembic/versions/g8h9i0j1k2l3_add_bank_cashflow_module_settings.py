"""add_bank_cashflow_module_settings

Revision ID: g8h9i0j1k2l3
Revises: f7a8b9c0d1e2
Create Date: 2026-03-02 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'g8h9i0j1k2l3'
down_revision: Union[str, None] = '97ba0aa60f3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column('bank_module_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'user_settings',
        sa.Column('cashflow_module_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'user_settings',
        sa.Column('wealth_module_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('user_settings', 'wealth_module_enabled')
    op.drop_column('user_settings', 'cashflow_module_enabled')
    op.drop_column('user_settings', 'bank_module_enabled')
