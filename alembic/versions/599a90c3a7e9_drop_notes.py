"""drop notes

The notes module is removed: its table and its settings toggle go with it.

Revision ID: 599a90c3a7e9
Revises: 3cf3a057d9ff
Create Date: 2026-11-18 15:02:24.894560

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '599a90c3a7e9'
down_revision: Union[str, None] = '3cf3a057d9ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_notes_user_uuid_bidx'), table_name='notes')
    op.drop_table('notes')
    op.drop_column('user_settings', 'notes_module_enabled')


def downgrade() -> None:
    op.add_column('user_settings', sa.Column('notes_module_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
    op.create_table('notes',
    sa.Column('uuid', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('user_uuid_bidx', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('name_enc', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description_enc', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('uuid', name=op.f('notes_pkey'))
    )
    op.create_index(op.f('ix_notes_user_uuid_bidx'), 'notes', ['user_uuid_bidx'], unique=False)
