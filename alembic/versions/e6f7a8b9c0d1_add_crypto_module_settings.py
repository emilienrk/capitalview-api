"""add_crypto_module_settings

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-02-25 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column('crypto_module_enabled', sa.Boolean(), server_default='false', nullable=False),
    )
    op.add_column(
        'user_settings',
        sa.Column('crypto_mode', sa.String(10), server_default='SINGLE', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('user_settings', 'crypto_mode')
    op.drop_column('user_settings', 'crypto_module_enabled')
