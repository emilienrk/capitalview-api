"""add rate_limit_hits

Revision ID: t1u2v3w4x5y6
Revises: s0t1u2v3w4x5
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa


revision = "t1u2v3w4x5y6"
down_revision = "s0t1u2v3w4x5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_hits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket", sa.String(64), nullable=False),
        sa.Column("hit_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rate_limit_hits_bucket_hit_at", "rate_limit_hits", ["bucket", "hit_at"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_hits_bucket_hit_at", table_name="rate_limit_hits")
    op.drop_table("rate_limit_hits")
