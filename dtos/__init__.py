"""Schemas for API responses - Re-exports for backward compatibility."""

# Bank schemas
from .bank import (
    BankAccountCreate,
    BankAccountResponse,
    BankAccountUpdate,
    BankSummaryResponse,
)

# Cashflow schemas
from .cashflow import (
    CashflowBalanceResponse,
    CashflowCategoryResponse,
    CashflowCreate,
    CashflowResponse,
    CashflowSummaryResponse,
    CashflowUpdate,
)

# Transaction schemas (shared)
from .transaction import (
    AccountSummaryResponse,
    PortfolioResponse,
    PositionResponse,
    TransactionResponse,
)

# Stock schemas
from .stock import (
    StockAccountBasicResponse,
    StockAccountCreate,
    StockAccountUpdate,
    StockBulkImportRequest,
    StockBulkImportResponse,
    StockTransactionBasicResponse,
    StockTransactionBulkCreate,
    StockTransactionCreate,
    StockTransactionUpdate,
    AssetSearchResult,
    AssetInfoResponse,
)

# Crypto schemas
from .crypto import (
    CryptoAccountBasicResponse,
    CryptoAccountCreate,
    CryptoAccountUpdate,
    CryptoBulkImportRequest,
    CryptoBulkImportResponse,
    CryptoTransactionBasicResponse,
    CryptoTransactionBulkCreate,
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
)

# Note schemas
from .note import (
    NoteCreate,
    NoteResponse,
    NoteUpdate,
)


__all__ = [
    # Bank
    "BankAccountCreate",
    "BankAccountResponse",
    "BankAccountUpdate",
    "BankSummaryResponse",
    # Cashflow
    "CashflowBalanceResponse",
    "CashflowCategoryResponse",
    "CashflowCreate",
    "CashflowResponse",
    "CashflowSummaryResponse",
    "CashflowUpdate",
    # Transaction
    "AccountSummaryResponse",
    "PortfolioResponse",
    "PositionResponse",
    "TransactionResponse",
    # Stock
    "StockAccountBasicResponse",
    "StockAccountCreate",
    "StockAccountUpdate",
    "StockBulkImportRequest",
    "StockBulkImportResponse",
    "StockTransactionBasicResponse",
    "StockTransactionBulkCreate",
    "StockTransactionCreate",
    "StockTransactionUpdate",
    "AssetSearchResult",
    "AssetInfoResponse",
    # Crypto
    "CryptoAccountBasicResponse",
    "CryptoAccountCreate",
    "CryptoAccountUpdate",
    "CryptoBulkImportRequest",
    "CryptoBulkImportResponse",
    "CryptoTransactionBasicResponse",
    "CryptoTransactionBulkCreate",
    "CryptoTransactionCreate",
    "CryptoTransactionUpdate",
    # Note
    "NoteCreate",
    "NoteResponse",
    "NoteUpdate",
]

