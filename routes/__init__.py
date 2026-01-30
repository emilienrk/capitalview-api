"""Routes module."""

from .bank import router as bank_router
from .cashflow import router as cashflow_router
from .stocks import router as stocks_router
from .crypto import router as crypto_router
from .users import router as users_router
from .notes import router as notes_router

__all__ = [
    "bank_router",
    "cashflow_router",
    "stocks_router",
    "crypto_router",
    "users_router",
    "notes_router",
]
