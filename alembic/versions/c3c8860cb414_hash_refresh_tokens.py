"""hash_refresh_tokens

Revision ID: c3c8860cb414
Revises: 7c9db47683df
Create Date: 2026-07-07 00:00:00.000000

Refresh tokens were stored in plaintext (RefreshToken.token). This renames the
column to token_hash; existing rows keep their (now-plaintext-in-a-hash-column)
values, which will simply never match a hashed lookup again — the practical
effect is a one-time forced logout of active refresh sessions.
See AUDIT_SECURITE.md ("Refresh tokens en clair en base").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'c3c8860cb414'
down_revision: Union[str, None] = '7c9db47683df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('refresh_tokens', 'token', new_column_name='token_hash')


def downgrade() -> None:
    op.alter_column('refresh_tokens', 'token_hash', new_column_name='token')
