"""rename_community_pick_symbol_to_asset_key

Revision ID: acbbf762e7ac
Revises: 2f624844c4f5
Create Date: 2026-04-01 15:41:34.003786

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'acbbf762e7ac'
down_revision: Union[str, None] = '2f624844c4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('uq_user_pick', 'community_picks', type_='unique')
    op.alter_column('community_picks', 'symbol', new_column_name='asset_key')
    op.create_unique_constraint('uq_user_pick', 'community_picks', ['user_id', 'asset_key', 'asset_type'])


def downgrade() -> None:
    op.drop_constraint('uq_user_pick', 'community_picks', type_='unique')
    op.alter_column('community_picks', 'asset_key', new_column_name='symbol')
    op.create_unique_constraint('uq_user_pick', 'community_picks', ['user_id', 'symbol', 'asset_type'])
