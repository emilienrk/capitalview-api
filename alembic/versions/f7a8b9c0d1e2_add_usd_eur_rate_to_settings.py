"""add_usd_eur_rate_to_settings

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-02-26 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column(
            'usd_eur_rate',
            sa.Numeric(precision=10, scale=6),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('user_settings', 'usd_eur_rate')
