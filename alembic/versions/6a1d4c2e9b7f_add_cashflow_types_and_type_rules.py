"""add cashflow types and type rules

Revision ID: 6a1d4c2e9b7f
Revises: 28fdf21d9e17
Create Date: 2026-09-16 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a1d4c2e9b7f'
down_revision: Union[str, None] = '28fdf21d9e17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get their operation type from the first transfer-pattern
    # rebuild, which holds the master key a migration never has.
    op.add_column('bank_transactions', sa.Column('operation_type_enc', sa.TEXT(), nullable=True))
    op.add_column('bank_transactions', sa.Column('type_override_enc', sa.TEXT(), nullable=True))
    op.create_table('bank_type_rules',
    sa.Column('uuid', sa.TEXT(), nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('rule_bidx', sa.TEXT(), nullable=False),
    sa.Column('signature_enc', sa.TEXT(), nullable=False),
    sa.Column('account_ref_enc', sa.TEXT(), nullable=False),
    sa.Column('credit_enc', sa.TEXT(), nullable=False),
    sa.Column('words_enc', sa.TEXT(), nullable=False),
    sa.Column('type_enc', sa.TEXT(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('uuid'),
    sa.UniqueConstraint('user_uuid_bidx', 'rule_bidx', name='uq_bank_type_rules_rule')
    )
    op.create_index(op.f('ix_bank_type_rules_user_uuid_bidx'), 'bank_type_rules', ['user_uuid_bidx'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bank_type_rules_user_uuid_bidx'), table_name='bank_type_rules')
    op.drop_table('bank_type_rules')
    op.drop_column('bank_transactions', 'type_override_enc')
    op.drop_column('bank_transactions', 'operation_type_enc')
