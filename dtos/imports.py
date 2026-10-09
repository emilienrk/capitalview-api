"""Unified platform-import schemas (preview/confirm for any source)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from dtos.crypto import BinanceImportGroupPreview, BinanceImportPreviewResponse
from models.currency import BASE_CURRENCY


class ImportSourceInfo(BaseModel):
    """One available import source (parser)."""
    source_id: str
    label: str
    category: str  # "crypto" | "stock" | "bank"
    file_hint: str
    supports_mapping: bool = False
    # Columns the parser assumes when the user maps nothing: a file already
    # carrying them needs no mapping step at all.
    default_mapping: dict[str, str] | None = None
    template_csv: str | None = None
    # Bank sources: whether the file may land on a bank-linked account, before
    # the bank's own history.
    fills_before_bank_history: bool = False


class ImportSourcesResponse(BaseModel):
    sources: list[ImportSourceInfo]


class DetectRequest(BaseModel):
    csv_content: str


class DetectMatch(BaseModel):
    source_id: str
    score: float  # 0..1 header-based confidence


class DetectResponse(BaseModel):
    matches: list[DetectMatch]  # sorted by descending score


class ImportPreviewRequest(BaseModel):
    """Preview request. ``account_id`` (optional) enables duplicate detection
    against the target account. ``options`` is parser-specific (e.g. column
    mapping for the generic CSV parsers)."""
    csv_content: str
    account_id: str | None = None
    options: dict = Field(default_factory=dict)


class StockImportRowPreview(BaseModel):
    """One parsed stock transaction row."""
    row_index: int
    executed_at: str  # ISO datetime
    type: str  # StockTransactionType value
    asset_key: str | None = None
    isin: str | None = None
    name: str | None = None
    amount: float
    price_per_unit: float
    fees: float = 0.0
    needs_asset_key: bool = False
    is_duplicate: bool = False
    error: str | None = None
    notes: str | None = None


class BankImportTransactionPreview(BaseModel):
    """One movement read from a bank statement CSV.

    The direction is read from the sign in the file, and `amount` carries the
    magnitude — the shape `normalize_transaction` expects, so an imported
    movement is indistinguishable from a synced one once stored.
    """
    day: date
    amount: Decimal
    direction: str  # "CRDT" | "DBIT"
    label: str = ""
    currency: str = BASE_CURRENCY
    is_duplicate: bool = False
    # What the import does with it, on an account no bank feeds: "new",
    # "duplicate", "replaces_manual" or "ambiguous"
    # (services.banking.transactions.classify_transactions).
    status: str = "new"
    # Left out of the import by the user (an ambiguous row, usually).
    excluded: bool = False


class BankImportReplacedEntry(BaseModel):
    """An adjustment or a forecast the file's operations replace."""
    day: date
    amount: Decimal  # signed
    origin: str  # "adjustment" | "forecast"
    label: str | None = None


class BankImportCurvePreview(BaseModel):
    """The balance curve a movements file draws before a linked account's
    history, anchored so that it meets the bank's."""
    start_date: date
    end_date: date
    opening_balance: Decimal  # balance before the first movement (the anchor)
    closing_balance: Decimal
    days: int
    # First day the curve goes below zero, if any: the usual sign of older
    # operations missing — a real account rarely goes negative for months.
    first_negative_date: date | None = None


class ImportPreviewResponse(BaseModel):
    """Common envelope; exactly one category payload is set."""
    source_id: str
    category: str
    total_rows: int
    duplicates_count: int = 0
    error_rows: int = 0
    warnings: list[str] = Field(default_factory=list)
    crypto: BinanceImportPreviewResponse | None = None
    stock_rows: list[StockImportRowPreview] | None = None
    bank_transactions: list[BankImportTransactionPreview] | None = None
    bank_curve: BankImportCurvePreview | None = None
    # Bank-linked account only: the first day the bank's own history covers.
    # The file is imported up to the day before; what the bank already holds is
    # counted in `covered_by_bank_count` and left out of `bank_transactions`.
    bank_history_from: date | None = None
    covered_by_bank_count: int = 0
    # Account no bank feeds only (docs/bank-ledger.md): what the file replaces,
    # and the balance at its last operation once imported.
    bank_replaced: list[BankImportReplacedEntry] | None = None
    bank_balance_after: Decimal | None = None
    bank_balance_after_date: date | None = None


class ImportConfirmRequest(BaseModel):
    """Confirm request: the (possibly user-adjusted) preview payload.

    Duplicate flags are informative only — fingerprints are recomputed
    server-side when ``skip_duplicates`` is true.
    """
    account_id: str
    skip_duplicates: bool = True
    options: dict = Field(default_factory=dict)
    crypto_groups: list[BinanceImportGroupPreview] | None = None
    stock_rows: list[StockImportRowPreview] | None = None
    bank_transactions: list[BankImportTransactionPreview] | None = None


class ImportConfirmResponse(BaseModel):
    imported_count: int
    skipped_duplicates: int = 0
    groups_count: int | None = None
    covered_by_bank_count: int = 0
    # Adjustments and forecasts the import replaced.
    replaced_count: int = 0
