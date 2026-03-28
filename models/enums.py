"""
Enumerations for the CapitalView database models.
"""
from enum import Enum


class AccountCategory(str, Enum):
    """Type of account tracked in account_history snapshots."""
    STOCK = "STOCK"
    CRYPTO = "CRYPTO"
    BANK = "BANK"
    ASSET = "ASSET"

class AssetType(str, Enum):
    """Type of asset for market data."""
    STOCK = "STOCK"
    CRYPTO = "CRYPTO"
    COMMODITY = "COMMODITY"
    BOND = "BOND"
    FIAT = "FIAT"

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
    WITHDRAW = "WITHDRAW"

    @classmethod
    def credit_types(cls) -> frozenset:
        return frozenset({cls.BUY, cls.DEPOSIT, cls.DIVIDEND})

    @classmethod
    def debit_types(cls) -> frozenset:
        return frozenset({cls.SELL, cls.WITHDRAW})

class CryptoAccountType(str, Enum):
    """Type factorisé de compte d'investissement crypto."""
    EXCHANGE = "EXCHANGE"
    WALLET = "WALLET"
    DEFI = "DEFI"

class CryptoTransactionType(str, Enum):
    """Type of crypto transaction (atomic ledger model).

    Atomic types
    ------------
    BUY          — Primary positive leg: asset acquired (cost basis in EUR).
    SPEND        — Negative leg: asset ceded in a trade or payment.
    FEE          — On-chain / exchange fee deducted in crypto.
    REWARD       — Staking / airdrop income; cost basis = 0.
    DEPOSIT      — Direct EUR entry (wire transfer, exchange deposit).
    ANCHOR       — Virtual EUR cost anchor for a crypto deposit group; never
                   counted in balance — only used to carry PRU cost in a group.
    TRANSFER     — Neutral outbound to own wallet (no tax event).
    WITHDRAW     — Taxable outbound (cash-out, payment, donation).
    """
    BUY          = "BUY"
    SPEND        = "SPEND"
    FEE          = "FEE"
    REWARD       = "REWARD"
    DEPOSIT      = "DEPOSIT"
    ANCHOR       = "ANCHOR"
    TRANSFER     = "TRANSFER"
    WITHDRAW     = "WITHDRAW"

    @classmethod
    def credit_types(cls) -> frozenset:
        return frozenset({cls.BUY, cls.REWARD, cls.DEPOSIT})

    @classmethod
    def debit_types(cls) -> frozenset:
        return frozenset({cls.SPEND, cls.TRANSFER, cls.WITHDRAW, cls.FEE})


class CryptoCompositeTransactionType(str, Enum):
    """User-facing composite actions accepted by the composite crypto endpoints.

    These actions are decomposed server-side into one or more
    ``CryptoTransactionType`` atomic rows.
    """

    BUY = "BUY"
    REWARD = "REWARD"
    FIAT_DEPOSIT = "FIAT_DEPOSIT"
    CRYPTO_DEPOSIT = "CRYPTO_DEPOSIT"
    TRANSFER = "TRANSFER"
    FIAT_WITHDRAW = "FIAT_WITHDRAW"
    SELL_TO_FIAT = "SELL_TO_FIAT"
    FEE = "FEE"
    NON_TAXABLE_EXIT = "NON_TAXABLE_EXIT"

    @classmethod
    def normalize(cls, value: "CryptoCompositeTransactionType") -> "CryptoCompositeTransactionType":
        """Return a canonical composite action."""
        return value
