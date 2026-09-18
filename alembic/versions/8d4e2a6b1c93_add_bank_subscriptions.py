"""add bank subscriptions

Revision ID: 8d4e2a6b1c93
Revises: 7b2e5f9c3a41
Create Date: 2026-09-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d4e2a6b1c93'
down_revision: Union[str, None] = '7b2e5f9c3a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('bank_subscriptions',
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
    op.create_index(op.f('ix_bank_subscriptions_user_uuid_bidx'), 'bank_subscriptions', ['user_uuid_bidx'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bank_subscriptions_user_uuid_bidx'), table_name='bank_subscriptions')
    op.drop_table('bank_subscriptions')
