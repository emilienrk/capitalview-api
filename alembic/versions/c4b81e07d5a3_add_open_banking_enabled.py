"""add open_banking_enabled to user_settings

Revision ID: c4b81e07d5a3
Revises: a7e3d9c1f204
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4b81e07d5a3"
down_revision: Union[str, None] = "a7e3d9c1f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Off for every existing user: the feature was never opt-in before, and
    # turning it on for them would surface a flow they never asked for.
    op.add_column(
        "user_settings",
        sa.Column(
            "open_banking_enabled",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "open_banking_enabled")
