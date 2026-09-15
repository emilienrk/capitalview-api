"""add bank_transfer_decisions

Revision ID: f96836a1518c
Revises: a8b9c0d1e2f3
Create Date: 2026-09-15 11:31:29.073801

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f96836a1518c'
down_revision: Union[str, None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also reported unrelated drift (refresh_tokens index, uuid column
    # types, totp_last_used_step): left out, it is not this change's to settle.
    op.create_table('bank_transfer_decisions',
    sa.Column('uuid', sa.TEXT(), nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('kind_enc', sa.TEXT(), nullable=False),
    sa.Column('debit_ref_bidx', sa.TEXT(), nullable=False),
    sa.Column('credit_ref_bidx', sa.TEXT(), nullable=False),
    sa.Column('debit_account_bidx', sa.TEXT(), nullable=False),
    sa.Column('credit_account_bidx', sa.TEXT(), nullable=False),
    sa.Column('debit_tokens_enc', sa.TEXT(), nullable=False),
    sa.Column('credit_tokens_enc', sa.TEXT(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('uuid'),
    sa.UniqueConstraint('user_uuid_bidx', 'debit_ref_bidx', 'credit_ref_bidx', name='uq_bank_transfer_decisions_pair')
    )
    op.create_index(op.f('ix_bank_transfer_decisions_user_uuid_bidx'), 'bank_transfer_decisions', ['user_uuid_bidx'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bank_transfer_decisions_user_uuid_bidx'), table_name='bank_transfer_decisions')
    op.drop_table('bank_transfer_decisions')
