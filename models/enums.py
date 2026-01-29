"""
Enumerations for the CapitalView database models.
"""
from enum import Enum


class FlowType(str, Enum):
    """Type of cashflow."""
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"


class Frequency(str, Enum):
    """Frequency of a cashflow."""
    ONCE = "ONCE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"

class BankAccountType(str, Enum):
    """Type of bank account."""
    MAIN_ACCOUNT = "MAIN_ACCOUNT"
    LIVRET_A = "LIVRET_A"
    LIVRET_DEVE = "LIVRET_DEVE"

class StockAccountType(str, Enum):
    """Type of stock investment account."""
    PEA = "PEA"
    CTO = "CTO"
    PEA_PME = "PEA_PME"


class StockTransactionType(str, Enum):
    """Type of stock transaction."""
    BUY = "BUY"
    SELL = "SELL"
    DEPOSIT = "DEPOSIT"
    DIVIDEND = "DIVIDEND"


class CryptoTransactionType(str, Enum):
    """Type of crypto transaction."""
    BUY = "BUY"
    SELL = "SELL"
    SWAP = "SWAP"
    STAKING = "STAKING"
