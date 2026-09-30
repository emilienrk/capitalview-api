"""replace negative euros option with automatic deposits

Revision ID: 3d775aabd640
Revises: 95ea6cbb2c2a
Create Date: 2026-09-30 08:41:08.398029

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d775aabd640'
down_revision: Union[str, None] = '95ea6cbb2c2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same display for everyone: hiding the negative euros is what automatic deposits do.
    op.add_column(
        "user_settings",
        sa.Column("crypto_auto_deposits", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE user_settings SET crypto_auto_deposits = NOT crypto_show_negative_positions")
    op.drop_column("user_settings", "crypto_show_negative_positions")


def downgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("crypto_show_negative_positions", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE user_settings SET crypto_show_negative_positions = NOT crypto_auto_deposits")
    op.drop_column("user_settings", "crypto_auto_deposits")
