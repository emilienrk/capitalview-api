"""Services module."""

from .market import get_market_price, get_market_info
from .stock_account import (
    create_stock_account,
    get_stock_account,
    get_user_stock_accounts,
    update_stock_account,
    delete_stock_account
)
from .stock_transaction import (
    create_stock_transaction,
    get_stock_transaction,
    update_stock_transaction,
    delete_stock_transaction,
    get_account_transactions,
    get_stock_account_summary
)
from .crypto_account import (
    create_crypto_account,
    get_crypto_account,
    get_user_crypto_accounts,
    update_crypto_account,
    delete_crypto_account
)
from .crypto_transaction import (
    create_crypto_transaction,
    get_crypto_transaction,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_crypto_account_summary
)
from .bank import (
    create_bank_account,
    get_bank_account,
    get_user_bank_accounts,
    update_bank_account,
    delete_bank_account
)
from .cashflow import (
    create_cashflow,
    get_cashflow,
    get_all_user_cashflows,
    update_cashflow,
    delete_cashflow,
    get_user_inflows,
    get_user_outflows,
    get_user_cashflow_balance
)
from .note import (
    create_note,
    get_note,
    get_user_notes,
    update_note,
    delete_note
)

__all__ = [
    "get_market_price",
    "get_market_info",
    
    # Stock Account
    "create_stock_account",
    "get_stock_account",
    "get_user_stock_accounts",
    "update_stock_account",
    "delete_stock_account",
    
    # Stock Transaction
    "create_stock_transaction",
    "get_stock_transaction",
    "update_stock_transaction",
    "delete_stock_transaction",
    "get_account_transactions",
    "get_stock_account_summary",
    
    # Crypto Account
    "create_crypto_account",
    "get_crypto_account",
    "get_user_crypto_accounts",
    "update_crypto_account",
    "delete_crypto_account",
    
    # Crypto Transaction
    "create_crypto_transaction",
    "get_crypto_transaction",
    "update_crypto_transaction",
    "delete_crypto_transaction",
    "get_crypto_account_summary",
    
    # Bank
    "create_bank_account",
    "get_bank_account",
    "get_user_bank_accounts",
    "update_bank_account",
    "delete_bank_account",
    
    # Cashflow
    "create_cashflow",
    "get_cashflow",
    "get_all_user_cashflows",
    "update_cashflow",
    "delete_cashflow",
    "get_user_inflows",
    "get_user_outflows",
    "get_user_cashflow_balance",
    
    # Note
    "create_note",
    "get_note",
    "get_user_notes",
    "update_note",
    "delete_note",
]
