"""Services module."""

from .market import get_market_price, get_market_info
from .stocks import (
    calculate_stock_transaction,
    aggregate_stock_positions,
    get_stock_account_summary
)
from .crypto import (
    calculate_crypto_transaction,
    aggregate_crypto_positions,
    get_crypto_account_summary
)
from .bank import get_bank_account_response, get_user_bank_accounts
from .cashflow import (
    get_user_inflows,
    get_user_outflows,
    get_user_cashflow_balance
)

__all__ = [
    "get_market_price",
    "get_market_info",
    "calculate_stock_transaction",
    "aggregate_stock_positions",
    "get_stock_account_summary",
    "calculate_crypto_transaction",
    "aggregate_crypto_positions",
    "get_crypto_account_summary",
    "get_bank_account_response",
    "get_user_bank_accounts",
    "get_user_inflows",
    "get_user_outflows",
    "get_user_cashflow_balance"
]