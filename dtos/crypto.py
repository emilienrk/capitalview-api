from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.enums import CryptoTransactionType


class CryptoAccountCreate(BaseModel):
    name: str
    platform: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountBasicResponse(BaseModel):
    id: str
    name: str
    platform: Optional[str] = None
    public_address: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CryptoTransactionCreate(BaseModel):
    """
    Atomic operation. Composite actions share group_uuid.
    price_per_unit is always EUR. REWARD price=0. FIAT_ANCHOR price=1.
    """
    account_id: str
    symbol: str
    name: Optional[str] = None
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoTransactionUpdate(BaseModel):
    symbol: Optional[str] = None
    name: Optional[str] = None
    type: Optional[CryptoTransactionType] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    price_per_unit: Optional[Decimal] = Field(None, ge=0)
    executed_at: Optional[datetime] = None
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoTransactionBulkCreate(BaseModel):
    symbol: str
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoBulkImportRequest(BaseModel):
    account_id: str
    transactions: list[CryptoTransactionBulkCreate]


class CryptoBulkImportResponse(BaseModel):
    imported_count: int
    transactions: list["CryptoTransactionBasicResponse"]


class CryptoTransactionBasicResponse(BaseModel):
    id: str
    account_id: str
    group_uuid: Optional[str] = None
    symbol: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


FIAT_SYMBOLS: frozenset[str] = frozenset(
    {"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "CNY", "NZD", "SEK", "NOK", "DKK"}
)


class CryptoCompositeTransactionCreate(BaseModel):
    """
    Composite transaction DTO — decomposed server-side into atomic rows.

    EUR anchor
    ----------
    The user provides `eur_amount` = total trade value in EUR.
    All atomic EUR prices are computed by the service:
      - Crypto rows (BUY, SPEND, FEE): price_per_unit = 0 in DB.
      - Fiat rows (FIAT_ANCHOR, FIAT_DEPOSIT, EXIT): price_per_unit ≥ 1.
      - FIAT_ANCHOR.amount = eur_amount (+ fee if not included).

    Fee model (fee_included)
    ------------------------
    • True: fee is informational. FEE row price = 0.
    • False: fee inflates total cost. FIAT_ANCHOR carries base + fee.
    """
    account_id: str
    type: Literal["BUY", "REWARD", "FIAT_DEPOSIT", "FIAT_WITHDRAW", "CRYPTO_DEPOSIT", "TRANSFER", "EXIT", "GAS_FEE", "NON_TAXABLE_EXIT"]
    symbol: str
    name: Optional[str] = None
    amount: Decimal = Field(gt=0)

    quote_symbol: Optional[str] = None
    quote_amount: Optional[Decimal] = Field(default=None, ge=0)

    eur_amount: Optional[Decimal] = Field(default=None, ge=0)

    fee_included: bool = True
    fee_percentage: Optional[Decimal] = Field(default=None, ge=0)
    fee_eur: Optional[Decimal] = Field(default=None, ge=0)
    fee_symbol: Optional[str] = None
    fee_amount: Optional[Decimal] = Field(default=None, ge=0)

    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None