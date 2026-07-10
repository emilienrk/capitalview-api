"""Unified platform-import schemas (preview/confirm for any source)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from dtos.crypto import BinanceImportGroupPreview, BinanceImportPreviewResponse


class ImportSourceInfo(BaseModel):
    """One available import source (parser)."""
    source_id: str
    label: str
    category: str  # "crypto" | "stock" | "bank"
    file_hint: str
    supports_mapping: bool = False


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


class BankImportPointPreview(BaseModel):
    """One (date, balance) point for bank history import."""
    snapshot_date: date
    value: Decimal
    is_duplicate: bool = False


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
    bank_points: list[BankImportPointPreview] | None = None


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
    bank_points: list[BankImportPointPreview] | None = None
    overwrite: bool = False  # bank only: replace existing history


class ImportConfirmResponse(BaseModel):
    imported_count: int
    skipped_duplicates: int = 0
    groups_count: int | None = None
