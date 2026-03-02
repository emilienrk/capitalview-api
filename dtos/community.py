"""Community view DTOs.

All response schemas intentionally omit amounts, quantities, and PRU.
Only symbols, asset types, and PnL percentages are exposed.
"""

from typing import List, Optional
from pydantic import BaseModel


class CommunitySettingsUpdate(BaseModel):
    """Payload for PUT /community/settings.

    * is_active: enable / disable the community profile.
    * display_name: optional public display name.
    * bio: optional short bio / description.
    * shared_stock_isins: list of stock ISINs the user wants to share.
    * shared_crypto_symbols: list of crypto symbols the user wants to share.
    """
    is_active: bool
    display_name: Optional[str] = None
    bio: Optional[str] = None
    shared_stock_isins: List[str] = []
    shared_crypto_symbols: List[str] = []


class CommunityPositionResponse(BaseModel):
    """A single shared position — only symbol, type, and PnL %."""
    symbol: str
    asset_type: str  # "CRYPTO" | "STOCK"
    pnl_percentage: Optional[float] = None  # None if market price unavailable


class CommunityProfileResponse(BaseModel):
    """Public profile returned by GET /community/profiles/{username}."""
    username: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
    positions: List[CommunityPositionResponse] = []
    global_pnl_percentage: Optional[float] = None


class CommunityProfileListItem(BaseModel):
    """Lightweight item for the profile listing endpoint."""
    username: str
    display_name: Optional[str] = None
    bio: Optional[str] = None


class CommunitySettingsResponse(BaseModel):
    """Response after updating community settings."""
    is_active: bool
    display_name: Optional[str] = None
    bio: Optional[str] = None
    shared_stock_isins: List[str] = []
    shared_crypto_symbols: List[str] = []
    positions_count: int = 0


class AvailablePosition(BaseModel):
    """A single position the user can choose to share."""
    symbol: str
    asset_type: str  # "CRYPTO" | "STOCK"


class AvailablePositionsResponse(BaseModel):
    """All shareable positions for the authenticated user."""
    stocks: List[AvailablePosition] = []
    crypto: List[AvailablePosition] = []
