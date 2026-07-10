"""
Generic bank statement CSV import.

The bank model stores a balance curve (daily snapshots), not transactions,
so the CSV is converted into (date, balance) points and written through the
existing ``import_bank_account_history`` (forward-fill included).

Two modes via ``options["bank_mode"]``:
- ``"balance"`` (default): the mapped column is the balance on that date
  (the last row wins for a given date).
- ``"delta"``: the mapped column is a signed movement; balances are
  accumulated chronologically from ``options["initial_balance"]``.

Mapping: {"date": ..., "balance": ...} or {"date": ..., "amount": ...}.
"""

from decimal import Decimal

from sqlmodel import Session

from dtos.bank import BankHistoryEntry
from dtos.imports import (
    BankImportPointPreview,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from services.imports.base import ImportCategory, ImportParser
from services.imports.dedup import bank_existing_dates
from services.imports.generic_csv import (
    get_mapped,
    parse_generic_date,
    parse_generic_decimal,
    read_rows,
)
from services.imports.registry import register


def parse_bank_points(csv_content: str, options: dict) -> tuple[list[BankImportPointPreview], list[str]]:
    mapping = options.get("mapping") or {}
    mode = (options.get("bank_mode") or "balance").lower()
    date_format = options.get("date_format")
    decimal_separator = options.get("decimal_separator")

    lines, warnings = read_rows(csv_content, options)

    value_field = "balance" if mapping.get("balance") else "amount"
    parsed: list[tuple] = []
    skipped = 0

    for line in lines:
        snapshot_date = parse_generic_date(get_mapped(line, mapping, "date"), date_format)
        value = parse_generic_decimal(get_mapped(line, mapping, value_field), decimal_separator)
        if snapshot_date is None or value is None:
            skipped += 1
            continue
        parsed.append((snapshot_date.date(), value))

    if skipped:
        warnings.append(f"{skipped} ligne(s) illisible(s) ignorée(s)")

    parsed.sort(key=lambda p: p[0])

    points: dict = {}
    if mode == "delta":
        try:
            balance = Decimal(str(options.get("initial_balance", "0")))
        except Exception:
            balance = Decimal("0")
        for d, delta in parsed:
            balance += delta
            points[d] = balance  # one point per date: end-of-day balance
    else:
        for d, value in parsed:
            points[d] = value  # last row wins for a given date

    return (
        [BankImportPointPreview(snapshot_date=d, value=v) for d, v in sorted(points.items())],
        warnings,
    )


@register
class GenericBankParser(ImportParser):
    """Any bank statement CSV, converted into a balance curve."""

    source_id = "generic_bank"
    label = "CSV générique (relevé bancaire) avec mapping de colonnes"
    category = ImportCategory.BANK
    file_hint = "relevé CSV bancaire (mode solde ou mode mouvements)"
    supports_mapping = True

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        points, warnings = parse_bank_points(csv_content, options)

        duplicates = 0
        if account_id and master_key:
            existing = bank_existing_dates(session, account_id, master_key)
            for point in points:
                if point.snapshot_date in existing:
                    point.is_duplicate = True
                    duplicates += 1

        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=len(points),
            duplicates_count=duplicates,
            warnings=warnings,
            bank_points=points,
        )

    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        from models.bank import BankAccount
        from services.bank import import_bank_account_history

        account = session.get(BankAccount, account_id)
        points = payload.bank_points or []

        entries = [
            BankHistoryEntry(snapshot_date=p.snapshot_date, value=p.value)
            for p in points
        ]
        written = import_bank_account_history(
            session, account, entries, master_key, overwrite=payload.overwrite
        )
        return ImportConfirmResponse(imported_count=written)
