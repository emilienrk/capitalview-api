"""split_market_price_history

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-03-11 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l3m4n5o6p7q8"
down_revision: Union[str, None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename table market_prices → market_assets
    op.rename_table("market_prices", "market_assets")

    # 2. Rename indexes
    op.drop_index("ix_market_prices_isin", table_name="market_assets")
    op.create_index("ix_market_assets_isin", "market_assets", ["isin"], unique=True)

    op.drop_index("ix_market_prices_symbol", table_name="market_assets")
    op.create_index("ix_market_assets_symbol", "market_assets", ["symbol"], unique=False)

    # 3. Add asset_type column
    op.add_column(
        "market_assets",
        sa.Column("asset_type", sa.String(), nullable=True),
    )

    # 4. Create market_price_history table
    op.create_table(
        "market_price_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "market_asset_id",
            sa.Integer(),
            sa.ForeignKey("market_assets.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("market_asset_id", "date", name="uq_market_price_history_asset_date"),
    )

    # 5. Migrate existing price data into history table
    op.execute(
        """
        INSERT INTO market_price_history (market_asset_id, price, date, created_at, updated_at)
        SELECT id, current_price, CURRENT_DATE, now(), now()
        FROM market_assets
        WHERE current_price IS NOT NULL AND current_price > 0
        """
    )

    # 6. Drop old columns from market_assets
    op.drop_column("market_assets", "current_price")
    op.drop_column("market_assets", "last_updated")


def downgrade() -> None:
    # Re-add removed columns
    op.add_column(
        "market_assets",
        sa.Column(
            "current_price",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "market_assets",
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Migrate most recent price back
    op.execute(
        """
        UPDATE market_assets ma
        SET current_price = sub.price,
            last_updated   = sub.updated_at
        FROM (
            SELECT DISTINCT ON (market_asset_id)
                   market_asset_id, price, updated_at
            FROM market_price_history
            ORDER BY market_asset_id, date DESC
        ) sub
        WHERE ma.id = sub.market_asset_id
        """
    )

    # Drop history table
    op.drop_table("market_price_history")

    # Drop asset_type column
    op.drop_column("market_assets", "asset_type")

    # Rename table back
    op.drop_index("ix_market_assets_isin", table_name="market_assets")
    op.drop_index("ix_market_assets_symbol", table_name="market_assets")
    op.rename_table("market_assets", "market_prices")
    op.create_index("ix_market_prices_isin", "market_prices", ["isin"], unique=True)
    op.create_index("ix_market_prices_symbol", "market_prices", ["symbol"], unique=False)
