"""add api tokens

Revision ID: 3c87d1280222
Revises: 9c4f1ab73e20
Create Date: 2026-08-13 11:08:59.700758

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3c87d1280222'
down_revision: Union[str, None] = '9c4f1ab73e20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'api_tokens',
        sa.Column('uuid', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('user_uuid', sa.String(), nullable=False),
        sa.Column('name_enc', sa.TEXT(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('mk_wrapped', sa.TEXT(), nullable=False),
        sa.Column('mk_salt', sa.TEXT(), nullable=False),
        sa.Column('scopes', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('uuid'),
    )
    op.create_index(op.f('ix_api_tokens_user_uuid'), 'api_tokens', ['user_uuid'], unique=False)
    op.create_index(op.f('ix_api_tokens_token_hash'), 'api_tokens', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_api_tokens_token_hash'), table_name='api_tokens')
    op.drop_index(op.f('ix_api_tokens_user_uuid'), table_name='api_tokens')
    op.drop_table('api_tokens')
