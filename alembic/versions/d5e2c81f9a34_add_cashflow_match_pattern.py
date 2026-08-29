"""add match_pattern to cashflows

Revision ID: d5e2c81f9a34
Revises: c4b81e07d5a3
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e2c81f9a34"
down_revision: Union[str, None] = "c4b81e07d5a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL for every existing row: a match is only ever created by the user
    # confirming one, never inferred behind their back.
    op.add_column("cashflows", sa.Column("match_pattern_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("cashflows", "match_pattern_enc")
