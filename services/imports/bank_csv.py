"""
Generic bank statement CSV import.

The bank model stores a balance curve (daily snapshots), not transactions,
so the CSV is converted into (date, balance) points and written through the
existing ``import_bank_account_history`` (forward-fill included).

The mapped column is the balance on that date; the last row wins for a given
date. A file of *movements* belongs to ``generic_bank_transactions`` below,
which accumulates them into the same curve and keeps the operations too.

Mapping: {"date": ..., "balance": ...}.

``generic_bank`` owns that machinery: it reads the ``snapshot_date``/``value``
shape the app documents without being told, and only needs a mapping when the
columns are someone else's. ``native_bank`` is the alias it grew out of.

``generic_bank_transactions`` is the other path entirely: it writes
``BankTransaction`` rows through ``store_transactions``, the same table the sync
fills, so an account with no Enable Banking connection (a Livret A, a passbook)
can still say what actually moved on it — and it rebuilds the balance curve
those movements describe, so such an account ends up with both halves a synced
one gets. A statement carries no balance of its own, so the curve is anchored on
``options["initial_balance"]``, the balance held before the file's first line
(zero unless said otherwise). Importing balances stays the degraded mode: it
draws a curve through the few dates a balance was recorded on, and nothing
between them.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

from dtos.bank import BankHistoryEntry
from dtos.imports import (
    BankImportCurvePreview,
    BankImportPointPreview,
    BankImportTransactionPreview,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
)
from models.currency import BASE_CURRENCY
from services.banking.transactions import CREDIT, DEBIT, STATUS_BOOKED, canonical_amount
from services.encryption import encrypt_data, hash_index
from services.imports.base import ImportCategory, ImportParser, header_has
from services.imports.dedup import bank_existing_dates, bank_existing_transaction_refs
from services.imports.generic_csv import (
    get_mapped,
    parse_generic_date,
    parse_generic_decimal,
    read_rows,
)
from services.imports.registry import register


def opening_balance(options: dict) -> Decimal:
    """The balance the curve starts from, before the file's first movement.

    Zero when unset: a passbook opened with the file is the honest default, and
    the preview shows what the anchor produces so a wrong one is visible.
    """
    try:
        return Decimal(str(options.get("initial_balance") or "0"))
    except Exception:
        return Decimal("0")


def parse_bank_points(csv_content: str, options: dict) -> tuple[list[BankImportPointPreview], list[str]]:
    mapping = options.get("mapping") or {}
    date_format = options.get("date_format")
    decimal_separator = options.get("decimal_separator")

    lines, warnings = read_rows(csv_content, options)

    parsed: list[tuple] = []
    skipped = 0

    for line in lines:
        snapshot_date = parse_generic_date(get_mapped(line, mapping, "date"), date_format)
        value = parse_generic_decimal(get_mapped(line, mapping, "balance"), decimal_separator)
        if snapshot_date is None or value is None:
            skipped += 1
            continue
        parsed.append((snapshot_date.date(), value))

    if skipped:
        warnings.append(f"{skipped} ligne(s) illisible(s) ignorée(s)")

    parsed.sort(key=lambda p: p[0])

    points: dict = {}
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
    """Any bank statement CSV, converted into a balance curve.

    Falls back on the shape CapitalView documents, and recognises it, so a
    well-formed file goes straight to the preview and the mapping is only
    asked for when the columns are actually someone else's.
    """

    source_id = "generic_bank"
    label = "Soldes — relevé bancaire (vos colonnes)"
    file_hint = "Trace la courbe du compte. Un solde par date, ou des mouvements cumulés depuis un solde de départ."
    supports_mapping = True
    default_mapping = {"date": "snapshot_date", "balance": "value"}
    template_csv = (
        "snapshot_date,value\n"
        "2024-01-31,12500.00\n"
        "2024-02-29,13200.50\n"
        "2024-03-31,11800.00\n"
    )

    def detect(self, csv_content: str) -> float:
        return 1.0 if header_has(csv_content, "snapshot_date", "value") else 0.0

    def effective_options(self, options: dict) -> dict:
        if options.get("mapping"):
            return options
        # No mapping given: read the documented shape.
        return {**options, "mapping": self.default_mapping}


@register
class NativeBankParser(GenericBankParser):
    """Alias kept for the files and saved imports that still name it.

    ``generic_bank`` reads this shape unaided now; nothing offers this source
    any more.
    """

    source_id = "native_bank"
    label = "Soldes — format CapitalView"
    file_hint = "Trace la courbe du compte. Deux colonnes : snapshot_date, value."
    supports_mapping = False
    listed = False

    def detect(self, csv_content: str) -> float:
        return 0.0  # `generic_bank` owns the shape now; two 1.0 would be a coin toss


# ---------------------------------------------------------------------------
# The transactional path
# ---------------------------------------------------------------------------

# Columns of the shape the template documents, used when the user maps nothing.
DEFAULT_TRANSACTION_MAPPING = {"date": "date", "amount": "amount", "label": "label"}


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


def _signed(row: BankImportTransactionPreview) -> Decimal:
    return row.amount if row.direction == CREDIT else -row.amount


def transactions_to_curve(
    rows: Iterable[BankImportTransactionPreview], opening: Decimal, until: date | None = None
) -> list[BankHistoryEntry]:
    """The daily balance curve the movements describe, walked forward.

    `opening` is the balance *before* the first movement, so the first day
    already carries its own. Days with no movement repeat the previous balance:
    a curve with holes reads as an account that fell to zero and came back.

    Nothing is produced before the first movement — the file says nothing about
    those days, and a zero there would draw a rise the account never had.
    `until` carries the last balance on up to that day, so a file that stops
    short of the bank's history still meets it.
    """
    by_day: dict[date, Decimal] = {}
    for row in rows:
        by_day[row.day] = by_day.get(row.day, Decimal("0")) + _signed(row)
    if not by_day:
        return []

    entries: list[BankHistoryEntry] = []
    balance = opening
    day, last = min(by_day), max(max(by_day), until or date.min)
    while day <= last:
        balance += by_day.get(day, Decimal("0"))
        entries.append(BankHistoryEntry(snapshot_date=day, value=balance))
        day += timedelta(days=1)
    return entries


def curve_preview(entries: list[BankHistoryEntry], opening: Decimal) -> BankImportCurvePreview | None:
    """What the curve will look like, for the user to check the anchor against."""
    if not entries:
        return None
    negative = next((e.snapshot_date for e in entries if e.value < 0), None)
    return BankImportCurvePreview(
        start_date=entries[0].snapshot_date,
        end_date=entries[-1].snapshot_date,
        opening_balance=opening,
        closing_balance=entries[-1].value,
        days=len(entries),
        first_negative_date=negative,
    )


# ---------------------------------------------------------------------------
# Accounts a bank already feeds
# ---------------------------------------------------------------------------

# A card payment is booked days after it was made: a statement dates it on the
# day it started, the bank's feed on the day it booked. Around the day the
# bank's history opens, the same operation can fall on both sides of it.
BOUNDARY_TOLERANCE = timedelta(days=5)


@dataclass(frozen=True)
class BankCoverage:
    """How much of a linked account's history the bank already holds.

    `starts` is the first day covered — None when the bank has served nothing
    yet, and then no day is known to be free of its rows. `boundary` lists the
    bank's own movements of the first days, as (earliest date, signed amount),
    for the statement rows just before `starts` to be recognised against.
    """
    starts: date | None
    boundary: tuple[tuple[date, Decimal], ...] = ()


def bank_coverage(
    session: Session, account_id: str, master_key: str, own_refs: frozenset[str] = frozenset()
) -> BankCoverage | None:
    """What the bank holds for the account, or None when no bank feeds it.

    The start is the earliest of how far the seeding pass reached and of the
    oldest movement stored from elsewhere — an Enable Banking export can reach
    further back than the seeding did. `own_refs`, the blind indexes of the
    file's own references, keeps a previous import of that same file from
    passing for the bank's history.
    """
    from sqlmodel import select

    from models.banking import BankAccountLink, BankTransaction
    from services.banking.transactions import row_date
    from services.encryption import decrypt_data

    account_bidx = hash_index(account_id, master_key)
    link = session.exec(
        select(BankAccountLink).where(BankAccountLink.bank_account_uuid_bidx == account_bidx)
    ).first()
    if link is None:
        return None

    candidates = []
    if link.history_served_from_enc:
        candidates.append(date.fromisoformat(decrypt_data(link.history_served_from_enc, master_key)))

    currency = _account_currency(session, account_id, master_key)
    dated: list[tuple[date, BankTransaction]] = []
    for row in session.exec(
        select(BankTransaction).where(BankTransaction.account_id_bidx == account_bidx)
    ).all():
        if row.entry_ref_bidx in own_refs:
            continue
        day = row_date(row, master_key)
        if day is not None:
            dated.append((day, row))
    if dated:
        candidates.append(min(day for day, _ in dated))
    if not candidates:
        return BankCoverage(starts=None)

    starts = min(candidates)
    boundary = []
    for day, row in dated:
        if day > starts + BOUNDARY_TOLERANCE or decrypt_data(row.currency_enc, master_key) != currency:
            continue
        dates = [
            date.fromisoformat(decrypt_data(column, master_key))
            for column in (row.booking_date_enc, row.transaction_date_enc, row.value_date_enc)
            if column
        ]
        amount = Decimal(decrypt_data(row.amount_enc, master_key))
        signed = amount if decrypt_data(row.credit_debit_enc, master_key) == CREDIT else -amount
        boundary.append((min(dates), signed))
    return BankCoverage(starts=starts, boundary=tuple(boundary))


def split_covered(
    rows: list[tuple[BankImportTransactionPreview, str]], coverage: BankCoverage | None
) -> tuple[list[tuple[BankImportTransactionPreview, str]], int]:
    """The rows the bank does not hold, and how many it does.

    Everything from `starts` on is the bank's. Just before it, a row is the
    bank's when one of its first movements has the same signed amount within
    the tolerance — each movement claiming the closest row only, so twins on
    the statement are not both swallowed by one operation.
    """
    if coverage is None:
        return rows, 0
    if coverage.starts is None:
        return [], len(rows)

    before = [(row, reference) for row, reference in rows if row.day < coverage.starts]
    claimed: set[int] = set()
    for day, signed in coverage.boundary:
        candidates = [
            i for i, (row, _) in enumerate(before)
            if i not in claimed and _signed(row) == signed and abs(day - row.day) <= BOUNDARY_TOLERANCE
        ]
        if candidates:
            claimed.add(min(candidates, key=lambda i: abs(day - before[i][0].day)))
    kept = [pair for i, pair in enumerate(before) if i not in claimed]
    return kept, len(rows) - len(kept)


def _account_currency(session: Session, account_id: str, master_key: str) -> str:
    """A statement is denominated by the account it belongs to."""
    from models.bank import BankAccount
    from services.bank import account_currency

    account = session.get(BankAccount, account_id)
    return account_currency(account, master_key) if account else BASE_CURRENCY




@register
class GenericBankTransactionsParser(ImportParser):
    """A bank statement CSV read as movements, not as a balance curve.

    For the accounts no bank API reaches — a Livret A, a passbook. It writes
    both halves a synced account gets: the movements themselves, through
    `store_transactions` (whose two deduplication levels make a re-import a
    no-op), and the balance curve they describe, anchored on the balance held
    before the first one. A statement carries no balance of its own, so that
    anchor is asked for — zero unless said otherwise.

    On a bank-linked account it fills what the bank never served, and only
    that: the rows the bank already holds are left out, and the curve is
    anchored so that it meets the bank's on the day its history opens.
    """

    source_id = "generic_bank_transactions"
    category = ImportCategory.BANK
    label = "Opérations — relevé bancaire"
    file_hint = "Remplit l'historique des opérations et « Ce qui a réellement bougé ». Une ligne par mouvement, montant signé."
    supports_mapping = True
    fills_before_bank_history = True
    default_mapping = DEFAULT_TRANSACTION_MAPPING
    template_csv = (
        "date,amount,label\n"
        "2024-01-15,-42.50,CARTE FNAC\n"
        "2024-01-31,1200.00,VIREMENT SALAIRE\n"
        "2024-02-03,-850.00,VIREMENT LIVRET A\n"
    )

    def detect(self, csv_content: str) -> float:
        return 1.0 if header_has(csv_content, *DEFAULT_TRANSACTION_MAPPING.values()) else 0.0

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

        coverage = None
        covered = 0
        if account_id and master_key:
            kept, coverage, covered = self._not_covered(session, account_id, master_key, rows)
            existing = bank_existing_transaction_refs(session, account_id, master_key)
            for row, reference in kept:
                if hash_index(reference, master_key) in existing:
                    row.is_duplicate = True
            rows = [row for row, _ in kept]
        duplicates = sum(1 for row in rows if row.is_duplicate)

        opening = self._opening_for(session, options, rows, account_id, master_key, coverage)
        curve = curve_preview(transactions_to_curve(rows, opening, _curve_end(coverage)), opening)
        if curve and curve.first_negative_date:
            warnings.append(
                f"Avec ce solde de départ, le compte passe sous zéro le "
                f"{curve.first_negative_date.strftime('%d/%m/%Y')} : c'est sans doute "
                f"le solde d'avant la première opération qu'il faut corriger."
            )

        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=len(rows),
            duplicates_count=duplicates,
            warnings=warnings,
            bank_transactions=rows,
            bank_curve=curve,
            bank_history_from=coverage.starts if coverage else None,
            covered_by_bank_count=covered,
        )

    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        from services.banking.transactions import store_transactions

        currency = _account_currency(session, account_id, master_key)
        # Filtered again here, never trusted from the preview: what the bank
        # holds may have grown since, and a client can send anything.
        kept, coverage, covered = self._not_covered(
            session, account_id, master_key, payload.bank_transactions or []
        )
        raws = [
            {
                "entry_reference": reference,
                "transaction_amount": {"currency": currency, "amount": str(row.amount)},
                "credit_debit_indicator": row.direction,
                "status": STATUS_BOOKED,
                "booking_date": row.day.isoformat(),
                "remittance_information": [row.label] if row.label else [],
            }
            for row, reference in kept
        ]
        inserted, updated, skipped = store_transactions(session, master_key, account_id, raws)
        self._write_curve(session, account_id, [row for row, _ in kept], payload.options, master_key, coverage)
        return ImportConfirmResponse(
            imported_count=inserted,
            # Already there, under the same reference: the re-import case.
            skipped_duplicates=updated + skipped,
            covered_by_bank_count=covered,
        )

    def _not_covered(
        self,
        session: Session,
        account_id: str,
        master_key: str,
        rows: list[BankImportTransactionPreview],
    ) -> tuple[list[tuple[BankImportTransactionPreview, str]], BankCoverage | None, int]:
        """The rows paired with their references — recomputed, since they are
        what makes a re-import idempotent — minus what a linked bank holds."""
        with_refs = _with_references(rows)
        own_refs = frozenset(hash_index(reference, master_key) for _, reference in with_refs)
        coverage = bank_coverage(session, account_id, master_key, own_refs)
        kept, covered = split_covered(with_refs, coverage)
        return kept, coverage, covered

    def _write_curve(
        self,
        session: Session,
        account_id: str,
        rows: list[BankImportTransactionPreview],
        options: dict,
        master_key: str,
        coverage: BankCoverage | None,
    ) -> None:
        """Write the balance curve the movements describe, and the balance they end on.

        Only the window the file covers is touched — `replace_history_window`,
        the same call the bank sync uses — so a manual snapshot outside it
        survives, and the file takes precedence inside it.
        """
        from models.bank import BankAccount
        from services.bank import (
            get_bank_account_history,
            replace_history_window,
        )

        opening = self._opening_for(session, options, rows, account_id, master_key, coverage)
        entries = transactions_to_curve(rows, opening, _curve_end(coverage))
        account = session.get(BankAccount, account_id)
        if not entries or account is None:
            return

        # Read before writing: the account's last known day tells whether this
        # file is the most recent word on the balance or an older one.
        last_known = get_bank_account_history(session, account_id, master_key)
        latest_known_date = last_known[-1].snapshot_date if last_known else None

        replace_history_window(
            session, account, entries, master_key, entries[0].snapshot_date, entries[-1].snapshot_date
        )

        # An old statement rebuilds its own stretch of the curve but says nothing
        # about today: overwriting the balance with it would walk the account
        # back in time. On a linked account the balance is the bank's, always.
        if coverage is None and (latest_known_date is None or entries[-1].snapshot_date >= latest_known_date):
            account.balance_enc = encrypt_data(str(entries[-1].value), master_key)
            # The movements already contain what the linked cashflows would add:
            # stamping today stops them being applied on top.
            account.balance_updated_at = date.today()
            session.add(account)
            session.commit()

    def _opening_for(
        self,
        session: Session,
        options: dict,
        rows: list[BankImportTransactionPreview],
        account_id: str | None,
        master_key: str | None,
        coverage: BankCoverage | None = None,
    ) -> Decimal:
        """The balance the curve starts from — asked for, met with the bank's
        curve, or picked up where the account's own history left off.

        Importing month after month, the anchor is never zero after the first
        file: it is the balance the account already stood at the day before this
        one opens. Re-typing it every time is how a curve gets a false step.
        """
        if options.get("initial_balance") is not None or not rows or not (account_id and master_key):
            return opening_balance(options)

        if coverage is not None and coverage.starts is not None:
            joined = _bank_balance_before(session, account_id, coverage.starts, master_key)
            if joined is not None:
                return joined - sum((_signed(row) for row in rows), Decimal("0"))

        from services.bank import last_known_balance_before

        return last_known_balance_before(session, account_id, rows[0].day, master_key)

    def _options_for(
        self, session: Session, options: dict, account_id: str | None, master_key: str | None
    ) -> dict:
        if options.get("currency") or not (account_id and master_key):
            return options
        return {**options, "currency": _account_currency(session, account_id, master_key)}


def _curve_end(coverage: BankCoverage | None) -> date | None:
    """The day the file's curve must run to: the eve of the bank's history."""
    if coverage is None or coverage.starts is None:
        return None
    return coverage.starts - timedelta(days=1)


def _bank_balance_before(session: Session, account_id: str, starts: date, master_key: str) -> Decimal | None:
    """The balance the bank's curve implies for the eve of `starts`.

    Its value on `starts` less that day's booked movements — the same walk back
    the sync draws its curve with, one day further. None when the bank drew
    nothing on that day, or when the stored curve is converted: it is kept in
    the base currency, the movements in the account's own.
    """
    from models.bank import BankAccount
    from services.bank import get_bank_account_history
    from services.banking.sync import booked_movements

    account = session.get(BankAccount, account_id)
    currency = _account_currency(session, account_id, master_key)
    if account is None or currency != BASE_CURRENCY:
        return None
    history = get_bank_account_history(session, account_id, master_key, starts, starts)
    if not history:
        return None
    movements = booked_movements(session, account, master_key, starts, starts, currency)
    return history[0].total_value - movements.get(starts, Decimal("0"))
