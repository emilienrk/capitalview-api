"""add_group_uuid_drop_fees_enc_from_crypto_transactions

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-02-24 08:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('crypto_transactions', sa.Column('group_uuid', sa.TEXT(), nullable=True))
    op.create_index('ix_crypto_transactions_group_uuid', 'crypto_transactions', ['group_uuid'])

    op.drop_column('crypto_transactions', 'fees_enc')
    op.drop_column('crypto_transactions', 'fees_symbol_enc')


def downgrade() -> None:
    op.drop_index('ix_crypto_transactions_group_uuid', table_name='crypto_transactions')
    op.drop_column('crypto_transactions', 'group_uuid')

    op.add_column('crypto_transactions', sa.Column('fees_enc', sa.TEXT(), nullable=False, server_default=''))
    op.add_column('crypto_transactions', sa.Column('fees_symbol_enc', sa.TEXT(), nullable=True))
