"""add accounts_enc to bank_sessions

Revision ID: 9b2d7f5e3c41
Revises: 8a1c6e4f2b90
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b2d7f5e3c41"
down_revision: Union[str, None] = "8a1c6e4f2b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bank_sessions", sa.Column("accounts_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_sessions", "accounts_enc")
