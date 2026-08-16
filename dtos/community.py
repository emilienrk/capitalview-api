"""Community view DTOs.

All response schemas intentionally omit amounts and quantities. What a shared
line exposes is its symbol, its PnL %, the entry price (PRU) and the first buy
date — enough to judge a call, never enough to infer position size.

Privacy model:
- Private profiles appear only when searching for the exact username.
- Investments are visible only to mutual followers.
"""

from datetime import date, datetime
from pydantic import BaseModel


class CommunitySettingsUpdate(BaseModel):
    """Payload for PUT /community/settings."""
    is_active: bool
    is_private: bool = True
    display_name: str | None = None
    bio: str | None = None
    shared_stock_asset_keys: list[str] = []
    shared_crypto_asset_keys: list[str] = []


class CommunityPositionResponse(BaseModel):
    """A single shared position — only asset_key, name, type, and PnL %."""
    asset_key: str
    name: str | None = None  # Human-readable name (e.g. "Apple Inc." instead of ISIN)
    asset_type: str  # "CRYPTO" | "STOCK"
    pnl_percentage: float | None = None  # None if market price unavailable
    pru: float | None = None  # Average entry price — never the quantity held
    first_bought_at: date | None = None  # Date of the earliest buy on this line


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


class ActivityItem(BaseModel):
    """One event in the activity feed of the people a user follows."""
    type: str  # "pick" | "target_reached"
    username: str
    display_name: str | None = None
    asset_key: str
    asset_type: str
    comment: str | None = None
    target_price: float | None = None
    performance_pct: float | None = None
    occurred_at: datetime


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
    shared_stock_asset_keys: list[str] = []
    shared_crypto_asset_keys: list[str] = []
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
    asset_key: str
    asset_type: str  # "CRYPTO" | "STOCK"
    name: str | None = None  # Human-readable name (ticker name for stocks)


class AvailablePositionsResponse(BaseModel):
    """All shareable positions for the authenticated user."""
    stocks: list[AvailablePosition] = []
    crypto: list[AvailablePosition] = []


# ── Picks (likes) ────────────────────────────────────────────


class PickCreate(BaseModel):
    """Payload for POST /community/picks."""
    asset_key: str
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
    asset_key: str
    asset_type: str
    comment: str | None = None
    target_price: float | None = None
    created_at: str
    updated_at: str
    # How the call actually played out. All nullable: an asset with no market
    # data must render as "unknown", never as a loss.
    price_at_pick: float | None = None
    current_price: float | None = None
    performance_pct: float | None = None
    target_reached: bool | None = None  # None = no target was set
