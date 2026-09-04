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

Two parsers share that machinery: ``generic_bank`` takes the mapping from the
user, ``native_bank`` hardcodes the ``snapshot_date``/``value`` shape the app
documents and is auto-detected from its header.

``generic_bank_transactions`` is the other path entirely: it writes
``BankTransaction`` rows through ``store_transactions``, the same table the sync
fills, so an account with no Enable Banking connection (a Livret A, a passbook)
can still say what actually moved on it. It writes no balance snapshot — a
statement does not always carry a reference balance to rebuild a curve from —
so the two bank imports are complementary, not alternatives.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlmodel import Session

from dtos.bank import BankHistoryEntry
from dtos.imports import (
    BankImportPointPreview,
    BankImportTransactionPreview,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from models.currency import BASE_CURRENCY
from services.banking.transactions import STATUS_BOOKED, canonical_amount
from services.encryption import hash_index
from services.imports.base import ImportCategory, ImportParser, csv_header_line
from services.imports.dedup import bank_existing_dates, bank_existing_transaction_refs
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


class _BankHistoryParser(ImportParser):
    """Shared preview/execute for bank parsers; subclasses supply the effective options."""

    category = ImportCategory.BANK

    def effective_options(self, options: dict) -> dict:
        """Options actually handed to :func:`parse_bank_points`."""
        return options

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        points, warnings = parse_bank_points(csv_content, self.effective_options(options))

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


@register
class GenericBankParser(_BankHistoryParser):
    """Any bank statement CSV, converted into a balance curve."""

    source_id = "generic_bank"
    label = "CSV générique (relevé bancaire) avec mapping de colonnes"
    file_hint = "relevé CSV bancaire (mode solde ou mode mouvements)"
    supports_mapping = True

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected


@register
class NativeBankParser(_BankHistoryParser):
    """The CSV shape CapitalView itself documents: one balance per date."""

    source_id = "native_bank"
    label = "Format CapitalView (snapshot_date, value)"
    file_hint = "CSV à deux colonnes : snapshot_date, value"
    supports_mapping = False
    template_csv = (
        "snapshot_date,value\n"
        "2024-01-31,12500.00\n"
        "2024-02-29,13200.50\n"
        "2024-03-31,11800.00\n"
    )

    _MAPPING = {"date": "snapshot_date", "balance": "value"}

    def detect(self, csv_content: str) -> float:
        header = csv_header_line(csv_content).lower()
        return 1.0 if "snapshot_date" in header and "value" in header else 0.0

    def effective_options(self, options: dict) -> dict:
        return {**options, "mapping": self._MAPPING, "bank_mode": "balance"}


# ---------------------------------------------------------------------------
# The transactional path
# ---------------------------------------------------------------------------

# Columns of the shape the template documents, used when the user maps nothing.
DEFAULT_TRANSACTION_MAPPING = {"date": "date", "amount": "amount", "label": "label"}

CREDIT = "CRDT"
DEBIT = "DBIT"

# Prefix of the synthesised entry reference, so a row's origin stays readable
# once decrypted.
CSV_REFERENCE_PREFIX = "csv"


def _synthetic_reference(day: date, amount: Decimal, direction: str, label: str, rank: int) -> str:
    """A stable identity for a CSV row, which carries no reference of its own.

    Without one, deduplication would fall back on the (date, amount, currency,
    direction) fingerprint alone, and two genuinely distinct movements booked
    the same day for the same amount would collapse into one on re-import. The
    digest covers the row's own content and `rank` counts the identical rows
    before it, so re-importing the same file yields the same references — while
    twins keep one each.

    Ranked among its identical siblings rather than by position in the file: a
    statement exported newest-first puts new rows at the top, which would shift
    every absolute position and re-insert the whole history.
    """
    payload = "|".join((day.isoformat(), canonical_amount(amount), direction, label, str(rank)))
    return f"{CSV_REFERENCE_PREFIX}:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _with_references(
    rows: Iterable[BankImportTransactionPreview],
) -> list[tuple[BankImportTransactionPreview, str]]:
    """Each row paired with its synthesised reference, twins ranked apart."""
    seen: Counter[tuple] = Counter()
    out = []
    for row in rows:
        key = (row.day, row.amount, row.direction, row.label)
        reference = _synthetic_reference(row.day, row.amount, row.direction, row.label, seen[key])
        seen[key] += 1
        out.append((row, reference))
    return out


def parse_bank_transactions(
    csv_content: str, options: dict
) -> tuple[list[BankImportTransactionPreview], list[str]]:
    """Read a statement CSV as movements: date, signed amount, label."""
    mapping = options.get("mapping") or DEFAULT_TRANSACTION_MAPPING
    date_format = options.get("date_format")
    decimal_separator = options.get("decimal_separator")
    currency = options.get("currency") or BASE_CURRENCY

    lines, warnings = read_rows(csv_content, options)

    rows: list[BankImportTransactionPreview] = []
    skipped = 0
    for line in lines:
        day = parse_generic_date(get_mapped(line, mapping, "date"), date_format)
        amount = parse_generic_decimal(get_mapped(line, mapping, "amount"), decimal_separator)
        # A zero movement has no direction to read, and nothing moved.
        if day is None or amount is None or amount == 0:
            skipped += 1
            continue
        rows.append(
            BankImportTransactionPreview(
                day=day.date(),
                amount=abs(amount),
                direction=CREDIT if amount > 0 else DEBIT,
                label=get_mapped(line, mapping, "label"),
                currency=currency,
            )
        )

    if skipped:
        warnings.append(f"{skipped} ligne(s) illisible(s) ignorée(s)")

    rows.sort(key=lambda r: (r.day, r.amount, r.direction, r.label))
    return rows, warnings


@register
class GenericBankTransactionsParser(ImportParser):
    """A bank statement CSV read as movements, not as a balance curve.

    For the accounts no bank API reaches — a Livret A, a passbook — so their
    movements can be compared and paired like any synced account's. Written
    through `store_transactions`, so its two deduplication levels apply and
    re-importing the same file changes nothing.
    """

    source_id = "generic_bank_transactions"
    category = ImportCategory.BANK
    label = "CSV de mouvements bancaires (date, montant signé, libellé)"
    file_hint = "relevé CSV d'opérations : une ligne par mouvement, montant signé"
    supports_mapping = True
    template_csv = (
        "date,amount,label\n"
        "2024-01-15,-42.50,CARTE FNAC\n"
        "2024-01-31,1200.00,VIREMENT SALAIRE\n"
        "2024-02-03,-850.00,VIREMENT LIVRET A\n"
    )

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected: `native_bank` owns the two-column shape

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        rows, warnings = parse_bank_transactions(csv_content, self._options_for(session, options, account_id, master_key))

        duplicates = 0
        if account_id and master_key:
            existing = bank_existing_transaction_refs(session, account_id, master_key)
            for row, reference in _with_references(rows):
                if hash_index(reference, master_key) in existing:
                    row.is_duplicate = True
                    duplicates += 1

        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=len(rows),
            duplicates_count=duplicates,
            warnings=warnings,
            bank_transactions=rows,
        )

    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        from services.banking.transactions import store_transactions

        currency = self._account_currency(session, account_id, master_key)
        raws = [
            {
                "entry_reference": reference,
                "transaction_amount": {"currency": currency, "amount": str(row.amount)},
                "credit_debit_indicator": row.direction,
                "status": STATUS_BOOKED,
                "booking_date": row.day.isoformat(),
                "remittance_information": [row.label] if row.label else [],
            }
            # References are recomputed here, never taken from the client: they
            # are what makes a re-import idempotent.
            for row, reference in _with_references(payload.bank_transactions or [])
        ]
        inserted, updated, skipped = store_transactions(session, master_key, account_id, raws)
        return ImportConfirmResponse(
            imported_count=inserted,
            # Already there, under the same reference: the re-import case.
            skipped_duplicates=updated + skipped,
        )

    def _options_for(
        self, session: Session, options: dict, account_id: str | None, master_key: str | None
    ) -> dict:
        if options.get("currency") or not (account_id and master_key):
            return options
        return {**options, "currency": self._account_currency(session, account_id, master_key)}

    def _account_currency(self, session: Session, account_id: str, master_key: str) -> str:
        """A statement is denominated by the account it belongs to."""
        from models.bank import BankAccount
        from services.bank import account_currency

        account = session.get(BankAccount, account_id)
        return account_currency(account, master_key) if account else BASE_CURRENCY
