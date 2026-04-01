"""rename community pick symbol to asset_key

Revision ID: r9s0t1u2v3w4
Revises: q8r9s0t1u2v3
Create Date: 2026-04-01 12:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "r9s0t1u2v3w4"
down_revision = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_user_pick", "community_picks", type_="unique")
    op.alter_column("community_picks", "symbol", new_column_name="asset_key")
    op.create_unique_constraint(
        "uq_user_pick",
        "community_picks",
        ["user_id", "asset_key", "asset_type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_user_pick", "community_picks", type_="unique")
    op.alter_column("community_picks", "asset_key", new_column_name="symbol")
    op.create_unique_constraint(
        "uq_user_pick",
        "community_picks",
        ["user_id", "symbol", "asset_type"],
    )
