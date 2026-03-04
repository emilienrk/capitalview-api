"""Community view DTOs.

All response schemas intentionally omit amounts, quantities, and PRU.
Only symbols, asset types, and PnL percentages are exposed.

Privacy model:
- Private profiles appear only when searching for the exact username.
- Investments are visible only to mutual followers.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class CommunitySettingsUpdate(BaseModel):
    """Payload for PUT /community/settings."""
    is_active: bool
    is_private: bool = True
    display_name: Optional[str] = None
    bio: Optional[str] = None
    shared_stock_isins: List[str] = []
    shared_crypto_symbols: List[str] = []


class CommunityPositionResponse(BaseModel):
    """A single shared position — only symbol, name, type, and PnL %."""
    symbol: str
    name: Optional[str] = None  # Human-readable name (e.g. "Apple Inc." instead of ISIN)
    asset_type: str  # "CRYPTO" | "STOCK"
    pnl_percentage: Optional[float] = None  # None if market price unavailable


class CommunityProfileResponse(BaseModel):
    """Public profile returned by GET /community/profiles/{username}."""
    username: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
    is_private: bool = True
    is_following: bool = False
    is_followed_by: bool = False
    is_mutual: bool = False
    positions: List[CommunityPositionResponse] = []
    picks: List["PickResponse"] = []
    global_pnl_percentage: Optional[float] = None
    followers_count: int = 0
    following_count: int = 0
    created_at: Optional[datetime] = None  # Account creation date


class CommunityProfileListItem(BaseModel):
    """Lightweight item for the profile listing endpoint."""
    username: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
    is_private: bool = True
    is_following: bool = False
    is_followed_by: bool = False
    is_mutual: bool = False


class CommunitySettingsResponse(BaseModel):
    """Response after updating community settings."""
    is_active: bool
    is_private: bool = True
    display_name: Optional[str] = None
    bio: Optional[str] = None
    shared_stock_isins: List[str] = []
    shared_crypto_symbols: List[str] = []
    positions_count: int = 0


class CommunitySearchResult(BaseModel):
    """Result from user search."""
    username: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
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
    name: Optional[str] = None  # Human-readable name (ticker name for stocks)


class AvailablePositionsResponse(BaseModel):
    """All shareable positions for the authenticated user."""
    stocks: List[AvailablePosition] = []
    crypto: List[AvailablePosition] = []


# ── Picks (likes) ────────────────────────────────────────────


class PickCreate(BaseModel):
    """Payload for POST /community/picks."""
    symbol: str
    asset_type: str  # "CRYPTO" | "STOCK"
    comment: Optional[str] = None
    target_price: Optional[float] = None


class PickUpdate(BaseModel):
    """Payload for PUT /community/picks/{pick_id}."""
    comment: Optional[str] = None
    target_price: Optional[float] = None


class PickResponse(BaseModel):
    """A single pick returned in responses."""
    id: int
    username: str
    symbol: str
    asset_type: str
    comment: Optional[str] = None
    target_price: Optional[float] = None
    created_at: str
    updated_at: str
