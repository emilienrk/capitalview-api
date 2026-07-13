"""add account security (wrapped master key, recovery, totp)

Revision ID: aa1b2c3d4e5f
Revises: c3c8860cb414
Create Date: 2026-07-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa1b2c3d4e5f'
down_revision: Union[str, None] = 'c3c8860cb414'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('mk_wrapped_password', sa.TEXT(), nullable=True))
    op.add_column('users', sa.Column('mk_salt_password', sa.TEXT(), nullable=True))
    op.add_column('users', sa.Column('mk_wrapped_recovery', sa.TEXT(), nullable=True))
    op.add_column('users', sa.Column('mk_salt_recovery', sa.TEXT(), nullable=True))
    op.add_column('users', sa.Column('totp_secret_enc', sa.TEXT(), nullable=True))
    op.add_column('users', sa.Column(
        'totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.add_column('users', sa.Column('totp_last_used_step', sa.BigInteger(), nullable=True))

    op.create_table(
        'totp_backup_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_uuid', sa.String(), nullable=False),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_totp_backup_codes_user_uuid'), 'totp_backup_codes', ['user_uuid'], unique=False
    )
    op.create_index(
        op.f('ix_totp_backup_codes_code_hash'), 'totp_backup_codes', ['code_hash'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_totp_backup_codes_code_hash'), table_name='totp_backup_codes')
    op.drop_index(op.f('ix_totp_backup_codes_user_uuid'), table_name='totp_backup_codes')
    op.drop_table('totp_backup_codes')

    op.drop_column('users', 'totp_last_used_step')
    op.drop_column('users', 'totp_enabled')
    op.drop_column('users', 'totp_secret_enc')
    op.drop_column('users', 'mk_salt_recovery')
    op.drop_column('users', 'mk_wrapped_recovery')
    op.drop_column('users', 'mk_salt_password')
    op.drop_column('users', 'mk_wrapped_password')
