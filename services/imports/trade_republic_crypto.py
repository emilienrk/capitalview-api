"""
Trade Republic CSV import (crypto side).

Reads the app's transaction export (see ``trade_republic``) and keeps only its
crypto lines; the stock import of the same file sends their cash here and back.
Each line is one euro trade, written as the composite form writes one:

- bought: ``BUY`` coin + ``SPEND EUR`` (amount + fee, so the fee is in the cost);
- sold: ``SPEND`` coin + ``DEPOSIT EUR`` (amount − fee, the net proceeds).

The euros come from the stock account and go back to it, which writes a
withdrawal and a deposit facing each trade. The same movements are written
here, a ``DEPOSIT EUR`` a second before a purchase and a ``WITHDRAW EUR`` a
second after a sale, so this account's cash stays at zero as it does on
Trade Republic.

The direction is read from the sign of ``shares``, not from ``type``, so a
crypto savings plan or any other label still lands on the right side.
"""

from datetime import timedelta
from decimal import Decimal

from dtos.crypto import BinanceImportPreviewResponse
from models.enums import CryptoTransactionType
from services.imports._crypto_common import (
    CryptoImportParser,
    MappedRow,
    build_crypto_preview,
)
from services.imports.generic_csv import parse_generic_date
from services.imports.registry import register
from services.imports.trade_republic import cell, decimal_cell, is_export, read_export

_FUNDING_GAP = timedelta(seconds=1)


def generate_preview(
    csv_content: str,
    session=None,
    existing_fps: set | None = None,
) -> BinanceImportPreviewResponse:
    buckets = []
    for line in read_export(csv_content):
        if cell(line, "asset_class").upper() != "CRYPTO":
            continue
        executed_at = parse_generic_date(cell(line, "datetime") or cell(line, "date"))
        coin = cell(line, "symbol").upper()
        shares = decimal_cell(line, "shares")
        if executed_at is None or not coin or not shares:
            continue
        operation = cell(line, "type") or "unknown"
        quantity = abs(shares)
        fees = abs(decimal_cell(line, "fee")) + abs(decimal_cell(line, "tax"))
        cash = abs(decimal_cell(line, "amount"))

        at = executed_at.replace(microsecond=0)
        rows: list[MappedRow] = []
        if shares > 0:
            rows.append(MappedRow(operation, coin, quantity, CryptoTransactionType.BUY,
                                  coin, quantity, Decimal("0")))
            if cash:
                spent = cash + fees
                rows.append(MappedRow(operation, "EUR", -spent, CryptoTransactionType.SPEND,
                                      "EUR", spent, Decimal("1")))
                buckets.append((at - _FUNDING_GAP, [MappedRow(
                    operation, "EUR", spent, CryptoTransactionType.DEPOSIT, "EUR", spent, Decimal("1"),
                )]))
        else:
            rows.append(MappedRow(operation, coin, -quantity, CryptoTransactionType.SPEND,
                                  coin, quantity, Decimal("0")))
            if cash:
                received = cash - fees
                rows.append(MappedRow(operation, "EUR", received, CryptoTransactionType.DEPOSIT,
                                      "EUR", received, Decimal("1")))
                buckets.append((at + _FUNDING_GAP, [MappedRow(
                    operation, "EUR", -received, CryptoTransactionType.WITHDRAW, "EUR", received, Decimal("1"),
                )]))
        buckets.append((at, rows))

    buckets.sort(key=lambda b: b[0])
    return build_crypto_preview(buckets, session=session, existing_fps=existing_fps)


@register
class TradeRepublicCryptoParser(CryptoImportParser):
    """Trade Republic transaction export, crypto lines only."""

    source_id = "trade_republic_crypto"
    label = "Trade Republic (export CSV)"
    file_hint = (
        "export « Transactions » de l'app Trade Republic — seules les cryptos sont "
        "reprises, les actions s'importent depuis la page Bourse"
    )

    def detect(self, csv_content: str) -> float:
        return 0.95 if is_export(csv_content) else 0.0

    def generate(self, csv_content, session=None, existing_fps=None):
        return generate_preview(csv_content, session=session, existing_fps=existing_fps)
