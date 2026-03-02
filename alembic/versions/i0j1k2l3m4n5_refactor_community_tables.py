"""refactor_community_tables

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-03-02 20:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'i0j1k2l3m4n5'
down_revision: Union[str, None] = 'g8h9i0j1k2l3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Recreate community_profiles with user_id as PK, display_name, bio
    op.create_table(
        'community_profiles',
        sa.Column('user_id', sa.String(), sa.ForeignKey('users.uuid', ondelete='CASCADE'), primary_key=True, nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('display_name', sa.String(100), nullable=True),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_community_profiles_user_id', 'community_profiles', ['user_id'])

    # Recreate community_positions with profile_user_id FK
    op.create_table(
        'community_positions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('profile_user_id', sa.String(), sa.ForeignKey('community_profiles.user_id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('asset_type', sa.String(), nullable=False),
        sa.Column('symbol_encrypted', sa.Text(), nullable=False),
        sa.Column('pru_encrypted', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.create_table(
        'community_profiles',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(), sa.ForeignKey('users.uuid', ondelete='CASCADE'), nullable=False, unique=True, index=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'community_positions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('profile_id', sa.Integer(), sa.ForeignKey('community_profiles.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('asset_type', sa.String(), nullable=False),
        sa.Column('symbol_encrypted', sa.Text(), nullable=False),
        sa.Column('pru_encrypted', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
