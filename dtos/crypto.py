from datetime import datetime, date
from decimal import Decimal

from pydantic import BaseModel, Field

from models.enums import CryptoCompositeTransactionType, CryptoTransactionType


class CryptoAccountCreate(BaseModel):
    name: str
    platform: str | None = None
    public_address: str | None = None
    opened_at: date | None = None


class CryptoAccountUpdate(BaseModel):
    name: str | None = None
    platform: str | None = None
    public_address: str | None = None
    opened_at: date | None = None


class CryptoAccountBasicResponse(BaseModel):
    id: str
    name: str
    platform: str | None = None
    public_address: str | None = None
    opened_at: date | None = None
    created_at: datetime
    updated_at: datetime


class CryptoTransactionCreate(BaseModel):
    """
    Atomic operation. Composite actions share group_uuid.
    price_per_unit is always EUR. REWARD price=0. ANCHOR price=1.
    """
    account_id: str
    symbol: str
    name: str | None = None
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None


class CryptoTransactionUpdate(BaseModel):
    symbol: str | None = None
    name: str | None = None
    type: CryptoTransactionType | None = None
    amount: Decimal | None = Field(None, gt=0)
    price_per_unit: Decimal | None = Field(None, ge=0)
    executed_at: datetime | None = None
    tx_hash: str | None = None
    notes: str | None = None


class CryptoTransactionBulkCreate(BaseModel):
    symbol: str
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None
    group_uuid: str | None = None


class CryptoBulkImportRequest(BaseModel):
    account_id: str
    transactions: list[CryptoTransactionBulkCreate]


class CryptoBulkImportResponse(BaseModel):
    imported_count: int
    transactions: list["CryptoTransactionBasicResponse"]


class CryptoTransactionBasicResponse(BaseModel):
    id: str
    account_id: str
    group_uuid: str | None = None
    symbol: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None


class CryptoCompositeTransactionResponse(BaseModel):
    """
    Wrapper returned by POST /transactions/composite and
    POST /transactions/cross-account-transfer.

    ``warning`` is set when one or more debited symbols end up with a
    negative balance after the operation (non-blocking — the transaction
    was still persisted).
    """
    rows: list[CryptoTransactionBasicResponse]
    warning: str | None = None


FIAT_SYMBOLS: frozenset[str] = frozenset(
    {"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "CNY", "NZD", "SEK", "NOK", "DKK"}
)


class CrossAccountTransferCreate(BaseModel):
    """
    Cross-account crypto transfer.
    Creates a TRANSFER outbound row in the source account and a BUY (price=0)
    inbound row in the destination account, linked by a shared group_uuid.
    Optional fee row (in crypto) is added to the source account.
    """
    from_account_id: str
    to_account_id: str
    symbol: str
    name: str | None = None
    amount: Decimal = Field(gt=0)
    fee_symbol: str | None = None
    fee_amount: Decimal | None = Field(default=None, ge=0)
    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None


class CryptoCompositeTransactionCreate(BaseModel):
    """
    Composite transaction DTO — decomposed server-side into atomic rows.

    EUR anchor
    ----------
    The user provides `eur_amount` = total trade value in EUR.
    All atomic EUR prices are computed by the service:
      - Crypto rows (BUY, SPEND, FEE): price_per_unit = 0 in DB.
    - Fiat rows (ANCHOR, DEPOSIT, WITHDRAW): price_per_unit ≥ 1.
      - ANCHOR.amount = eur_amount (+ fee if not included).

    Fee model (fee_included)
    ------------------------
    • True: fee is informational. FEE row price = 0.
    • False: fee inflates total cost. ANCHOR carries base + fee.
    """
    account_id: str
    type: CryptoCompositeTransactionType
    symbol: str
    name: str | None = None
    amount: Decimal = Field(gt=0)

    quote_symbol: str | None = None
    quote_amount: Decimal | None = Field(default=None, ge=0)

    eur_amount: Decimal | None = Field(default=None, ge=0)

    fee_included: bool = True
    fee_percentage: Decimal | None = Field(default=None, ge=0)
    fee_eur: Decimal | None = Field(default=None, ge=0)
    fee_symbol: str | None = None
    fee_amount: Decimal | None = Field(default=None, ge=0)

    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None


# ── Bulk Composite Import DTOs ───────────────────────────────

class CryptoCompositeBulkItem(BaseModel):
    """
    One composite operation for bulk CSV import.
    account_id is injected server-side from the request envelope.
    """
    type: CryptoCompositeTransactionType
    symbol: str
    name: str | None = None
    amount: Decimal = Field(gt=0)
    quote_symbol: str | None = None
    quote_amount: Decimal | None = Field(default=None, ge=0)
    eur_amount: Decimal | None = Field(default=None, ge=0)
    fee_included: bool = True
    fee_symbol: str | None = None
    fee_amount: Decimal | None = Field(default=None, ge=0)
    executed_at: datetime
    tx_hash: str | None = None
    notes: str | None = None


class CryptoBulkCompositeImportRequest(BaseModel):
    """Bulk import of composite operations from a CSV file."""
    account_id: str
    transactions: list[CryptoCompositeBulkItem]


class CryptoBulkCompositeImportResponse(BaseModel):
    imported_count: int   # total atomic rows created
    groups_count: int     # number of composite operations processed


# ── Binance Import DTOs ──────────────────────────────────────

class BinanceImportRowPreview(BaseModel):
    """One CSV row mapped to an atomic transaction."""
    operation: str
    coin: str
    change: float
    mapped_type: str
    mapped_symbol: str
    mapped_amount: float
    mapped_price: float


class BinanceImportGroupPreview(BaseModel):
    """One group (same timestamp) of CSV rows."""
    group_index: int
    timestamp: str
    rows: list[BinanceImportRowPreview]
    summary: str
    has_eur: bool
    auto_eur_amount: float | None = None
    needs_eur_input: bool
    hint_usdc_amount: float | None = None
    eur_amount: float | None = None


class BinanceImportPreviewRequest(BaseModel):
    csv_content: str


class BinanceImportPreviewResponse(BaseModel):
    total_groups: int
    total_rows: int
    groups_needing_eur: int
    groups: list[BinanceImportGroupPreview]


class BinanceImportConfirmRequest(BaseModel):
    account_id: str
    groups: list[BinanceImportGroupPreview]


class BinanceImportConfirmResponse(BaseModel):
    imported_count: int
    groups_count: int