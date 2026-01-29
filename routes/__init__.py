"""Routes module."""

from .portfolio import router as portfolio_router
from .bank import router as bank_router
from .cashflow import router as cashflow_router

__all__ = ["portfolio_router", "bank_router", "cashflow_router"]
