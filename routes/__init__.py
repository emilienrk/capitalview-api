"""Routes module."""

from .auth import router as auth_router
from .api_tokens import router as api_tokens_router
from .bank import router as bank_router
from .banking import router as banking_router
from .cashflow import router as cashflow_router
from .stocks import router as stocks_router
from .crypto import router as crypto_router
from .dashboard import router as dashboard_router
from .notes import router as notes_router
from .settings import router as settings_router
from .notifications import router as notifications_router
from .asset import router as asset_router
from .community import router as community_router
from .market import router as market_router
from .projection import router as projection_router
from .imports import router as imports_router
from .analytics import router as analytics_router

__all__ = [
    "auth_router",
    "api_tokens_router",
    "bank_router",
    "banking_router",
    "cashflow_router",
    "stocks_router",
    "crypto_router",
    "dashboard_router",
    "notes_router",
    "settings_router",
    "notifications_router",
    "asset_router",
    "community_router",
    "market_router",
    "projection_router",
    "analytics_router",
]
