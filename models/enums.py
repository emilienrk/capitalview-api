"""
Enumerations for the CapitalView database models.
"""
from enum import Enum


class AssetType(str, Enum):
    """Type of asset for market data."""
    STOCK = "STOCK"
    CRYPTO = "CRYPTO"


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
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    LIVRET_A = "LIVRET_A"
    LIVRET_DEVE = "LIVRET_DEVE"
    LEP = "LEP"
    LDD = "LDD"
    PEL = "PEL"
    CEL = "CEL"

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
    """Type of crypto transaction (atomic ledger model).

    Atomic types
    ------------
    BUY          — Primary positive leg: asset acquired (cost basis in EUR).
    SPEND        — Negative leg: asset ceded in a trade or payment.
    FEE          — On-chain / exchange fee deducted in crypto.
    REWARD       — Staking / airdrop income; cost basis = 0.
    FIAT_DEPOSIT — Direct EUR entry (wire transfer, exchange deposit).
    FIAT_ANCHOR  — Virtual EUR cost anchor for a crypto deposit group; never
                   counted in balance — only used to carry PRU cost in a group.
    TRANSFER     — Neutral outbound to own wallet (no tax event).
    EXIT         — Taxable outbound (cash-out, payment, donation).
    """
    BUY          = "BUY"
    SPEND        = "SPEND"
    FEE          = "FEE"
    REWARD       = "REWARD"
    FIAT_DEPOSIT = "FIAT_DEPOSIT"
    FIAT_ANCHOR  = "FIAT_ANCHOR"
    TRANSFER     = "TRANSFER"
    EXIT         = "EXIT"
