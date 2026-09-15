"""add label signatures and transfer patterns

Revision ID: 28fdf21d9e17
Revises: f96836a1518c
Create Date: 2026-09-15 13:12:10.065895

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '28fdf21d9e17'
down_revision: Union[str, None] = 'f96836a1518c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also reported the unrelated drift noted in f96836a1518c: left out.
    # Existing rows get their signature from the first transfer-pattern rebuild,
    # which holds the master key a migration never has.
    op.create_table('bank_transfer_patterns',
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('source_bidx', sa.TEXT(), nullable=False),
    sa.Column('content_enc', sa.TEXT(), nullable=False),
    sa.Column('built_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('user_uuid_bidx')
    )
    op.add_column('bank_transactions', sa.Column('label_signature_bidx', sa.TEXT(), nullable=True))
    op.create_index(op.f('ix_bank_transactions_label_signature_bidx'), 'bank_transactions', ['label_signature_bidx'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bank_transactions_label_signature_bidx'), table_name='bank_transactions')
    op.drop_column('bank_transactions', 'label_signature_bidx')
    op.drop_table('bank_transfer_patterns')
