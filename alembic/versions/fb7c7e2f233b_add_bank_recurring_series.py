"""add bank recurring series

Revision ID: fb7c7e2f233b
Revises: 7b2e5f9c3a41
Create Date: 2026-09-20 17:13:26.508591

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb7c7e2f233b'
down_revision: Union[str, None] = '7b2e5f9c3a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('bank_recurring_series',
    sa.Column('uuid', sa.TEXT(), nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('status_enc', sa.TEXT(), nullable=False),
    sa.Column('anchors_enc', sa.TEXT(), nullable=False),
    sa.Column('includes_enc', sa.TEXT(), nullable=True),
    sa.Column('excludes_enc', sa.TEXT(), nullable=True),
    sa.Column('identity_enc', sa.TEXT(), nullable=False),
    sa.Column('name_enc', sa.TEXT(), nullable=True),
    sa.Column('cadence_enc', sa.TEXT(), nullable=True),
    sa.Column('ended_on_enc', sa.TEXT(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('uuid')
    )
    op.create_index(op.f('ix_bank_recurring_series_user_uuid_bidx'), 'bank_recurring_series', ['user_uuid_bidx'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bank_recurring_series_user_uuid_bidx'), table_name='bank_recurring_series')
    op.drop_table('bank_recurring_series')
