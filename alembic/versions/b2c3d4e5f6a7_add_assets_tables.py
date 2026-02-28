"""
add assets and asset_valuations tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assets',
        sa.Column('uuid', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False, index=True),
        sa.Column('name_enc', sa.TEXT(), nullable=False),
        sa.Column('description_enc', sa.TEXT(), nullable=True),
        sa.Column('category_enc', sa.TEXT(), nullable=False),
        sa.Column('purchase_price_enc', sa.TEXT(), nullable=True),
        sa.Column('estimated_value_enc', sa.TEXT(), nullable=False),
        sa.Column('currency', sa.TEXT(), nullable=False, server_default='EUR'),
        sa.Column('acquisition_date_enc', sa.TEXT(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('uuid')
    )

    op.create_table(
        'asset_valuations',
        sa.Column('uuid', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('asset_uuid', sa.TEXT(), nullable=False, index=True),
        sa.Column('estimated_value_enc', sa.TEXT(), nullable=False),
        sa.Column('note_enc', sa.TEXT(), nullable=True),
        sa.Column('valued_at_enc', sa.TEXT(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('uuid')
    )


def downgrade() -> None:
    op.drop_table('asset_valuations')
    op.drop_table('assets')
