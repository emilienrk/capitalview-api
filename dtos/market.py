"""Market-facing schemas (price series and the user's trades plotted on them)."""

import datetime
from decimal import Decimal

from pydantic import BaseModel

from models.enums import AssetType


class AssetPricePoint(BaseModel):
    """One daily close for an asset, already converted to EUR."""

    date: datetime.date
    price: Decimal


class AssetTimelineEvent(BaseModel):
    """One of the user's own trades, positioned on the price curve.

    ``price`` is where the marker belongs on the Y axis: the executed unit
    price for BUY/SELL, and the market price of the day for INCOME (a dividend
    or a staking reward is not a price of its own, so it rides the curve).
    """

    date: datetime.date
    type: str
    """BUY, SELL or INCOME — the ledger types that read as a decision."""
    quantity: Decimal
    price: Decimal | None
    total: Decimal
    """Signed cash impact in EUR: negative when money went out."""
    cost_basis_after: Decimal | None
    """Average unit cost held right after this trade, in EUR."""


class AssetPriceTimelineResponse(BaseModel):
    """An asset's price history since the user first bought it, with their trades."""

    asset_key: str
    symbol: str | None
    name: str | None
    asset_type: AssetType | None
    currency: str = "EUR"
    """Everything in this payload is expressed in this currency."""
    points: list[AssetPricePoint]
    events: list[AssetTimelineEvent]
    average_buy_price: Decimal | None
    """Current unit cost basis in EUR, fees included. None once fully sold."""
    quantity_held: Decimal
    current_price: Decimal | None
