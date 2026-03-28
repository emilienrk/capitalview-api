"""Community view DTOs.

All response schemas intentionally omit amounts, quantities, and PRU.
Only symbols, asset types, and PnL percentages are exposed.

Privacy model:
- Private profiles appear only when searching for the exact username.
- Investments are visible only to mutual followers.
"""

from datetime import datetime
from pydantic import BaseModel


class CommunitySettingsUpdate(BaseModel):
    """Payload for PUT /community/settings."""
    is_active: bool
    is_private: bool = True
    display_name: str | None = None
    bio: str | None = None
    shared_stock_isins: list[str] = []
    shared_crypto_symbols: list[str] = []


class CommunityPositionResponse(BaseModel):
    """A single shared position — only symbol, name, type, and PnL %."""
    symbol: str
    name: str | None = None  # Human-readable name (e.g. "Apple Inc." instead of ISIN)
    asset_type: str  # "CRYPTO" | "STOCK"
    pnl_percentage: str | None = None  # None if market price unavailable


class CommunityProfileResponse(BaseModel):
    """Public profile returned by GET /community/profiles/{username}."""
    username: str
    display_name: str | None = None
    bio: str | None = None
    is_private: bool = True
    is_following: bool = False
    is_followed_by: bool = False
    is_mutual: bool = False
    positions: list[CommunityPositionResponse] = []
    picks: list["PickResponse"] = []
    global_pnl_percentage: float | None = None
    followers_count: int = 0
    following_count: int = 0
    created_at: datetime | None = None  # Account creation date


class CommunityProfileListItem(BaseModel):
    """Lightweight item for the profile listing endpoint."""
    username: str
    display_name: str | None = None
    bio: str | None = None
    is_private: bool = True
    is_following: bool = False
    is_followed_by: bool = False
    is_mutual: bool = False


class CommunitySettingsResponse(BaseModel):
    """Response after updating community settings."""
    is_active: bool
    is_private: bool = True
    display_name: str | None = None
    bio: str | None = None
    shared_stock_isins: list[str] = []
    shared_crypto_symbols: list[str] = []
    positions_count: int = 0


class CommunitySearchResult(BaseModel):
    """Result from user search."""
    username: str
    display_name: str | None = None
    bio: str | None = None
    is_private: bool = True
    is_following: bool = False
    is_followed_by: bool = False
    is_mutual: bool = False


class FollowResponse(BaseModel):
    """Response after follow/unfollow action."""
    is_following: bool
    is_mutual: bool


class FollowStatsResponse(BaseModel):
    """Follower/following counts for a user."""
    followers_count: int = 0
    following_count: int = 0


class AvailablePosition(BaseModel):
    """A single position the user can choose to share."""
    symbol: str
    asset_type: str  # "CRYPTO" | "STOCK"
    name: str | None = None  # Human-readable name (ticker name for stocks)


class AvailablePositionsResponse(BaseModel):
    """All shareable positions for the authenticated user."""
    stocks: list[AvailablePosition] = []
    crypto: list[AvailablePosition] = []


# ── Picks (likes) ────────────────────────────────────────────


class PickCreate(BaseModel):
    """Payload for POST /community/picks."""
    symbol: str
    asset_type: str  # "CRYPTO" | "STOCK"
    comment: str | None = None
    target_price: float | None = None


class PickUpdate(BaseModel):
    """Payload for PUT /community/picks/{pick_id}."""
    comment: str | None = None
    target_price: float | None = None


class PickResponse(BaseModel):
    """A single pick returned in responses."""
    id: int
    username: str
    symbol: str
    asset_type: str
    comment: str | None = None
    target_price: float | None = None
    created_at: str
    updated_at: str
