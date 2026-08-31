"""
CapitalView Database Models.

This module exports all SQLModel models for the application.
Import models from here to ensure all relationships are properly loaded.
"""
from .enums import (
    BankAccountType,
    CryptoTransactionType,
    FlowType,
    Frequency,
    StockAccountType,
    StockTransactionType,
    AccountCategory,
)
from .user import User, UserSettings, UserAIProvider, TotpBackupCode
from .api_token import ApiToken
from .cashflow import Cashflow
from .bank import BankAccount
from .banking import (
    UserBankConnection,
    BankAuthorization,
    BankSession,
    BankAccountLink,
    BankTransaction,
)
from .stock import StockAccount, StockTransaction
from .crypto import CryptoAccount, CryptoTransaction
from .market import MarketAsset, MarketPriceHistory, MarketPrice
from .note import Note
from .card import Card
from .asset import Asset, AssetValuation
from .community import CommunityProfile, CommunityPosition, CommunityFollow, CommunityPick
from .account_history import AccountHistory
from .notification import Notification, NotificationType
from .job_run import JobRun, JobStatus

__all__ = [
    # Enums
    "BankAccountType",
    "FlowType",
    "Frequency",
    "StockAccountType",
    "StockTransactionType",
    "CryptoTransactionType",
    "AccountCategory",
    # Models
    "User",
    "UserSettings",
    "UserAIProvider",
    "TotpBackupCode",
    "ApiToken",
    "Cashflow",
    "BankAccount",
    "UserBankConnection",
    "BankAuthorization",
    "BankSession",
    "BankAccountLink",
    "BankTransaction",
    "StockAccount",
    "StockTransaction",
    "CryptoAccount",
    "CryptoTransaction",
    "MarketAsset",
    "MarketPriceHistory",
    "MarketPrice",
    "Note",
    "Card",
    "Asset",
    "AssetValuation",
    "CommunityProfile",
    "CommunityPosition",
    "CommunityFollow",
    "CommunityPick",
    "AccountHistory",
    "Notification",
    "JobRun",
    "JobStatus",
    "NotificationType",
]
