"""amount past which a debit nothing faces asks the user

Revision ID: c8d9e0f1a2b3
Revises: a7c3e9f1b2d4
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, None] = "a7c3e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("bank_question_threshold", sa.Numeric(12, 2), nullable=False, server_default="500"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "bank_question_threshold")
