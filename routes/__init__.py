"""Routes module."""

from .auth import router as auth_router
from .bank import router as bank_router
from .cashflow import router as cashflow_router
from .stocks import router as stocks_router
from .crypto import router as crypto_router
from .dashboard import router as dashboard_router
from .notes import router as notes_router
from .settings import router as settings_router

__all__ = [
    "auth_router",
    "bank_router",
    "cashflow_router",
    "stocks_router",
    "crypto_router",
    "dashboard_router",
    "notes_router",
    "settings_router",
]
