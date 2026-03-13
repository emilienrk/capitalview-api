"""drop currency from market_assets (delete non-EUR price history)

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
Create Date: 2026-03-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "n5o6p7q8r9s0"
down_revision = "m4n5o6p7q8r9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Delete price history rows for assets that were not stored in EUR.
    # These prices are in native currency (USD, GBP, …) and cannot be trusted as EUR.
    # They will be re-fetched in EUR on the next request via backfill_price_history.
    conn.execute(
        text("""
            DELETE FROM market_price_history
            WHERE market_asset_id IN (
                SELECT id FROM market_assets
                WHERE currency IS NOT NULL AND currency != 'EUR'
            )
        """)
    )

    op.drop_column("market_assets", "currency")


def downgrade() -> None:
    op.add_column(
        "market_assets",
        sa.Column("currency", sa.String(), server_default="EUR", nullable=False),
    )
