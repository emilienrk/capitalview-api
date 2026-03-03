"""add_follows_and_privacy

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-03-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j1k2l3m4n5o6"
down_revision: Union[str, None] = "i0j1k2l3m4n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_private column to community_profiles (defaults to True)
    op.add_column(
        "community_profiles",
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    # Create community_follows table
    op.create_table(
        "community_follows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "follower_id",
            sa.String(),
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "following_id",
            sa.String(),
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("follower_id", "following_id", name="uq_follow_pair"),
    )


def downgrade() -> None:
    op.drop_table("community_follows")
    op.drop_column("community_profiles", "is_private")
