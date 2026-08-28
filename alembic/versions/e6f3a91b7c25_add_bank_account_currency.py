"""add currency to bank_accounts

Revision ID: e6f3a91b7c25
Revises: d5e2c81f9a34
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f3a91b7c25"
down_revision: Union[str, None] = "d5e2c81f9a34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Encrypted like every other user-visible attribute of an account, and
    # nullable rather than back-filled: the value would have to be written with
    # each user's Master Key, which a migration does not have. NULL reads as
    # EUR, which is what every existing row is.
    op.add_column("bank_accounts", sa.Column("currency_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_accounts", "currency_enc")
