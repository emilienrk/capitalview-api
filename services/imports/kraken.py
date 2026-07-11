"""
Kraken ledger CSV import (``ledgers.csv``).

Format:
  "txid","refid","time","type","subtype","aclass","asset","amount","fee","balance"

Rows sharing the same ``refid`` form one group (e.g. the two legs of a
trade), which is more precise than time-window grouping. Kraken asset
codes (ZEUR, XXBT…) are normalized to standard symbols; staking suffixes
(.S/.F/.M/.B) are stripped. Non-zero ``fee`` values produce an extra
FEE row in the group.
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import Session

from dtos.crypto import BinanceImportPreviewResponse
from models.enums import CryptoTransactionType
from services.imports._crypto_common import (
    CryptoImportParser,
    MappedRow,
    build_crypto_preview,
)
from services.imports.base import csv_header_line
from services.imports.registry import register

# Kraken-specific asset codes → standard symbols
_ASSET_MAP = {
    "ZEUR": "EUR", "ZUSD": "USD", "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY",
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XXDG": "DOGE", "XDG": "DOGE",
    "XXRP": "XRP", "XXLM": "XLM", "XLTC": "LTC", "XZEC": "ZEC", "XETC": "ETC",
    "XMLN": "MLN", "XREP": "REP", "ETH2": "ETH",
}

_STAKING_SUFFIXES = (".S", ".F", ".M", ".B", ".P")


def normalize_kraken_asset(raw: str) -> str:
    """ZEUR→EUR, XXBT→BTC, DOT.S→DOT, ETH2.S→ETH…"""
    asset = raw.strip().upper()
    for suffix in _STAKING_SUFFIXES:
        if asset.endswith(suffix):
            asset = asset[: -len(suffix)]
            break
    if asset in _ASSET_MAP:
        return _ASSET_MAP[asset]
    # Legacy 4-letter codes with X/Z prefix (XTZ, XRP… are real symbols, so
    # only strip when the prefixed form is unknown AND 4 chars long)
    if len(asset) == 4 and asset[0] in ("X", "Z"):
        stripped = asset[1:]
        if stripped in ("BTC", "ETH", "LTC", "XRP", "XLM", "ZEC", "ETC", "REP", "MLN", "DOGE"):
            return stripped
    return asset


@dataclass
class _KrakenRow:
    refid: str
    time: datetime
    type: str
    subtype: str
    asset: str          # normalized
    raw_asset: str
    amount: Decimal
    fee: Decimal


def _parse_time(value: str) -> datetime | None:
    value = value.strip().replace("T", " ")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_csv(content: str) -> list[_KrakenRow]:
    if content.startswith("\ufeff"):
        content = content[1:]

    reader = csv.DictReader(io.StringIO(content))
    rows: list[_KrakenRow] = []

    for line in reader:
        lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in line.items()}
        time_val = _parse_time(lowered.get("time", ""))
        if time_val is None:
            continue
        try:
            amount = Decimal(lowered.get("amount") or "0")
            fee = Decimal(lowered.get("fee") or "0")
        except InvalidOperation:
            continue
        if amount == 0 and fee == 0:
            continue

        raw_asset = lowered.get("asset", "")
        rows.append(_KrakenRow(
            refid=lowered.get("refid", ""),
            time=time_val,
            type=lowered.get("type", "").lower(),
            subtype=lowered.get("subtype", "").lower(),
            asset=normalize_kraken_asset(raw_asset),
            raw_asset=raw_asset,
            amount=amount,
            fee=abs(fee),
        ))

    return rows


def _map_row(row: _KrakenRow) -> MappedRow | None:
    """Map one ledger row to an atomic transaction (None = intentionally skipped)."""
    is_eur = row.asset == "EUR"
    amount = abs(row.amount)
    positive = row.amount > 0

    # Internal spot<->staking moves are not portfolio events
    if row.type == "transfer" and ("staking" in row.subtype or "spot" in row.subtype):
        return None

    if row.type == "deposit":
        tx_type = CryptoTransactionType.DEPOSIT if is_eur else CryptoTransactionType.BUY
    elif row.type == "withdrawal":
        tx_type = CryptoTransactionType.WITHDRAW if is_eur else CryptoTransactionType.TRANSFER
    elif row.type in ("staking", "earn", "reward", "dividend"):
        tx_type = CryptoTransactionType.REWARD
    elif row.type in ("trade", "spend", "receive", "margin", "settled", "adjustment", "transfer"):
        if positive:
            tx_type = CryptoTransactionType.DEPOSIT if is_eur else CryptoTransactionType.BUY
        else:
            tx_type = CryptoTransactionType.SPEND
    else:
        tx_type = CryptoTransactionType.BUY if positive else CryptoTransactionType.SPEND

    if amount == 0:
        return None

    price = Decimal("1") if is_eur else Decimal("0")
    if tx_type in (CryptoTransactionType.BUY, CryptoTransactionType.TRANSFER) and not is_eur:
        price = Decimal("0")

    return MappedRow(
        operation=f"{row.type}{f' ({row.subtype})' if row.subtype else ''}",
        coin=row.asset,
        change=row.amount,
        tx_type=tx_type,
        asset_key=row.asset,
        amount=amount,
        price=price,
    )


def _build_buckets(rows: list[_KrakenRow]) -> list[tuple[datetime, list[MappedRow]]]:
    """Group ledger rows by refid (falling back to one group per row)."""
    ordered: dict[str, list[_KrakenRow]] = {}
    for i, row in enumerate(sorted(rows, key=lambda r: r.time)):
        key = row.refid or f"__solo_{i}"
        ordered.setdefault(key, []).append(row)

    buckets: list[tuple[datetime, list[MappedRow]]] = []
    for group_rows in ordered.values():
        mapped: list[MappedRow] = []
        for row in group_rows:
            m = _map_row(row)
            if m is not None:
                mapped.append(m)
            if row.fee > 0:
                mapped.append(MappedRow(
                    operation="fee",
                    coin=row.asset,
                    change=-row.fee,
                    tx_type=CryptoTransactionType.FEE,
                    asset_key=row.asset,
                    amount=row.fee,
                    price=Decimal("1") if row.asset == "EUR" else Decimal("0"),
                ))
        if mapped:
            buckets.append((group_rows[0].time.replace(microsecond=0), mapped))

    buckets.sort(key=lambda b: b[0])
    return buckets


def generate_preview(
    csv_content: str,
    session: Session | None = None,
    existing_fps: set | None = None,
) -> BinanceImportPreviewResponse:
    rows = _parse_csv(csv_content)
    if not rows:
        return BinanceImportPreviewResponse(
            total_groups=0, total_rows=0, groups_needing_eur=0, groups=[],
        )
    return build_crypto_preview(_build_buckets(rows), session=session, existing_fps=existing_fps)


@register
class KrakenParser(CryptoImportParser):
    """Kraken ledger CSV export (ledgers.csv)."""

    source_id = "kraken"
    label = "Kraken (export ledgers.csv)"
    file_hint = "export CSV « Ledgers » Kraken"

    _HEADERS = {"txid", "refid", "time", "type", "aclass", "asset", "amount", "fee", "balance"}

    def detect(self, csv_content: str) -> float:
        header = csv_header_line(csv_content).lower()
        cols = {c.strip().strip('"') for c in header.split(",")}
        hits = len(self._HEADERS & cols)
        return hits / len(self._HEADERS) if hits >= 6 else 0.0

    def generate(self, csv_content, session=None, existing_fps=None):
        return generate_preview(csv_content, session=session, existing_fps=existing_fps)
