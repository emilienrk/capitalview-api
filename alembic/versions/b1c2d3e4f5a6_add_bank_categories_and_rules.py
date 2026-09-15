"""add bank categories and rules

Revision ID: b1c2d3e4f5a6
Revises: 28fdf21d9e17
Create Date: 2026-09-15 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = '28fdf21d9e17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get their operation type from the first transfer-pattern
    # rebuild, which holds the master key a migration never has.
    op.create_table('bank_categories',
    sa.Column('uuid', sa.TEXT(), nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('name_enc', sa.TEXT(), nullable=False),
    sa.Column('name_bidx', sa.TEXT(), nullable=False),
    sa.Column('nature_enc', sa.TEXT(), nullable=False),
    sa.Column('origin_enc', sa.TEXT(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('uuid'),
    sa.UniqueConstraint('user_uuid_bidx', 'name_bidx', name='uq_bank_categories_name')
    )
    op.create_index(op.f('ix_bank_categories_user_uuid_bidx'), 'bank_categories', ['user_uuid_bidx'], unique=False)
    op.create_table('bank_category_rules',
    sa.Column('uuid', sa.TEXT(), nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), nullable=False),
    sa.Column('tokens_enc', sa.TEXT(), nullable=False),
    sa.Column('tokens_bidx', sa.TEXT(), nullable=False),
    sa.Column('category_ref_enc', sa.TEXT(), nullable=False),
    sa.Column('source_enc', sa.TEXT(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('uuid'),
    sa.UniqueConstraint('user_uuid_bidx', 'tokens_bidx', name='uq_bank_category_rules_tokens')
    )
    op.create_index(op.f('ix_bank_category_rules_user_uuid_bidx'), 'bank_category_rules', ['user_uuid_bidx'], unique=False)
    op.add_column('bank_transactions', sa.Column('operation_type_enc', sa.TEXT(), nullable=True))
    op.add_column('bank_transactions', sa.Column('category_ref_enc', sa.TEXT(), nullable=True))
    op.add_column('user_settings', sa.Column('ai_categorization_enabled', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('user_settings', 'ai_categorization_enabled')
    op.drop_column('bank_transactions', 'category_ref_enc')
    op.drop_column('bank_transactions', 'operation_type_enc')
    op.drop_index(op.f('ix_bank_category_rules_user_uuid_bidx'), table_name='bank_category_rules')
    op.drop_table('bank_category_rules')
    op.drop_index(op.f('ix_bank_categories_user_uuid_bidx'), table_name='bank_categories')
    op.drop_table('bank_categories')
