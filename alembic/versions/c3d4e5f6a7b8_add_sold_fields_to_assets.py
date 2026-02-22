"""
add sold_price_enc and sold_at_enc to assets

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-02-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('assets', sa.Column('sold_price_enc', sa.TEXT(), nullable=True))
    op.add_column('assets', sa.Column('sold_at_enc', sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column('assets', 'sold_at_enc')
    op.drop_column('assets', 'sold_price_enc')
