"""when the user forced a type on an operation

Revision ID: 4ca577dfa649
Revises: c8d9e0f1a2b3
Create Date: 2026-10-09 12:36:37.205680

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ca577dfa649'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bank_transactions', sa.Column('type_override_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('bank_transactions', 'type_override_at')
