"""rename_assets_columns_to_asset_key

Revision ID: 2f624844c4f5
Revises: b38e3b3aff98
Create Date: 2026-03-30 20:04:13.788152

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '2f624844c4f5'
down_revision: Union[str, None] = 'b38e3b3aff98'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('community_positions', 'symbol_encrypted', new_column_name='asset_key_enc')
    op.alter_column('community_positions', 'pru_encrypted', new_column_name='pru_enc')
    op.alter_column('crypto_transactions', 'symbol_enc', new_column_name='asset_key_enc')
    op.alter_column('market_assets', 'isin', new_column_name='asset_key')
    op.drop_index('ix_market_assets_isin', table_name='market_assets')
    op.create_index(op.f('ix_market_assets_asset_key'), 'market_assets', ['asset_key'], unique=True)
    op.alter_column('stock_transactions', 'isin_enc', new_column_name='asset_key_enc')

def downgrade() -> None:
    op.alter_column('stock_transactions', 'asset_key_enc', new_column_name='isin_enc')
    op.drop_index(op.f('ix_market_assets_asset_key'), table_name='market_assets')
    op.create_index('ix_market_assets_isin', 'market_assets', ['isin'], unique=True)
    op.alter_column('market_assets', 'asset_key', new_column_name='isin')
    op.alter_column('crypto_transactions', 'asset_key_enc', new_column_name='symbol_enc')
    op.alter_column('community_positions', 'pru_enc', new_column_name='pru_encrypted')
    op.alter_column('community_positions', 'asset_key_enc', new_column_name='symbol_encrypted')
