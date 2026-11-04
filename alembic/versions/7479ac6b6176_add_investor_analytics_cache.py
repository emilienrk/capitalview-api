"""add investor analytics cache

Revision ID: 7479ac6b6176
Revises: c0d1e2f3a4b5
Create Date: 2026-11-04 18:21:47.104566

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7479ac6b6176'
down_revision: Union[str, None] = 'c0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Derived data only: an empty table is rebuilt on the next read of the
    # analysis page, so nothing is backfilled here.
    op.create_table('investor_analytics_cache',
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('source_bidx', sa.TEXT(), nullable=False),
    sa.Column('content_enc', sa.TEXT(), nullable=False),
    sa.Column('built_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('user_uuid_bidx', 'kind')
    )


def downgrade() -> None:
    op.drop_table('investor_analytics_cache')
