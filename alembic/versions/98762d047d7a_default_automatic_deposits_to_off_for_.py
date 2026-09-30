"""default automatic deposits to off for new accounts

Revision ID: 98762d047d7a
Revises: 3d775aabd640
Create Date: 2026-09-30 08:56:58.331861

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '98762d047d7a'
down_revision: Union[str, None] = '3d775aabd640'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts keep their value; only new ones start with the deposit type in view.
    op.alter_column("user_settings", "crypto_auto_deposits", server_default=sa.false())


def downgrade() -> None:
    op.alter_column("user_settings", "crypto_auto_deposits", server_default=sa.true())
