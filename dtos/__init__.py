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
    CryptoCompositeTransactionCreate,
    CryptoTransactionBasicResponse,
    CryptoTransactionBulkCreate,
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
    CrossAccountTransferCreate,
    BinanceImportRowPreview,
    BinanceImportGroupPreview,
    BinanceImportPreviewRequest,
    BinanceImportPreviewResponse,
    BinanceImportConfirmRequest,
    BinanceImportConfirmResponse,
)

# Note schemas
from .note import (
    NoteCreate,
    NoteReorder,
    NoteResponse,
    NoteUpdate,
)

# Settings schemas
from .settings import (
    UserSettingsUpdate,
    UserSettingsResponse,
)

# Asset schemas
from .asset import (
    AssetCreate,
    AssetUpdate,
    AssetSell,
    AssetResponse,
    AssetValuationCreate,
    AssetValuationResponse,
    AssetCategorySummary,
    AssetSummaryResponse,
)

# Dashboard statistics
from .dashboard import (
    DashboardStatisticsResponse,
    InvestmentDistribution,
    WealthBreakdown,
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
    # Binance import
    "BinanceImportRowPreview",
    "BinanceImportGroupPreview",
    "BinanceImportPreviewRequest",
    "BinanceImportPreviewResponse",
    "BinanceImportConfirmRequest",
    "BinanceImportConfirmResponse",
    # Note
    "NoteCreate",
    "NoteReorder",
    "NoteResponse",
    "NoteUpdate",
    # Settings
    "UserSettingsUpdate",
    "UserSettingsResponse",
    # Asset
    "AssetCreate",
    "AssetUpdate",
    "AssetSell",
    "AssetResponse",
    "AssetValuationCreate",
    "AssetValuationResponse",
    "AssetCategorySummary",
    "AssetSummaryResponse",
    # Dashboard statistics
    "DashboardStatisticsResponse",
    "InvestmentDistribution",
    "WealthBreakdown",
]

