"""
Normalisation and storage of the movements read from Enable Banking (spec §E/§F).

`normalize_transaction` is a pure reading of one raw API payload; it owns every
quirk §F measured against a real bank: `booking_date` is sometimes absent, a
foreign currency arrives without any exchange rate, amounts are decimal strings,
and `transaction_id` must never be used to identify anything.

`store_transactions` owns the three deduplication levels of §E, from the most
reliable to the most approximate:

1. intra-account, by `entry_reference` — survives a reconnection, but the key
   is composite `(account_id_bidx, entry_ref_bidx)` because a reference is not
   globally unique;
2. intra-account, by `dedup_bidx` — the fallback when a bank supplies no
   reference, and the only way to follow a pending operation whose reference
   changes when it books;
3. cross-account, by `dedup_bidx`, scoped to the **user** — 93 % of card
   operations also exist on the current account with a different reference;
   without this level, nearly all card spending is counted twice.

Cancelled and rejected operations are never stored: they invalidate a row
already ingested, and are dropped outright when they were never seen.

A foreign-currency operation is stored with its own currency, unconverted (no
rate is ever supplied); readers must check `currency_enc` before summing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session, select

from models.banking import BankAccountLink, BankTransaction
from services.encryption import decrypt_data, encrypt_data, hash_index

# Currency the rest of CapitalView reasons in. Anything else is stored as-is.
BASE_CURRENCY = "EUR"

# TransactionStatus members, referenced by NAME. The contract's
# x-enum-descriptions are misaligned with their values (documented trap), so a
# value's position in that enum means nothing — never index into it.
STATUS_BOOKED = "BOOK"
STATUS_CANCELLED = "CNCL"
STATUS_REJECTED = "RJCT"

# The two statuses that invalidate an already ingested transaction (§E).
INVALIDATING_STATUSES = frozenset({STATUS_CANCELLED, STATUS_REJECTED})

# Only a booked operation is final. Anything else may still change amount, date
# or entry reference, so its row stays claimable for correction (§E).
FINAL_STATUSES = frozenset({STATUS_BOOKED})


@dataclass(frozen=True)
class NormalizedTransaction:
    """One raw API transaction, read according to §F. Nothing here is derived
    from `transaction_id`, whose value changes between calls."""

    entry_reference: str | None
    amount: Decimal
    currency: str
    credit_debit: str
    status: str
    booking_date: date | None
    value_date: date | None
    transaction_date: date | None
    # Date the transaction is placed on, with §F's fallback applied. None when
    # the bank supplied none of the three — such a movement cannot be dated and
    # is therefore unusable for a balance curve.
    effective_date: date | None
    remittance: str | None
    # A foreign currency arrives with no exchange rate, so the amount is left
    # in its original currency rather than naively added up.
    unconverted: bool

    @property
    def period(self) -> str | None:
        """The "YYYY-MM" behind period_bidx."""
        return self.effective_date.strftime("%Y-%m") if self.effective_date else None

    @property
    def dedup_key(self) -> str | None:
        """The fingerprint behind dedup_bidx — the only reliable signal for the
        card / current-account duplication.

        It carries the currency on top of §A5's (date, amount, direction)
        triple (ruling R11): the captured data holds an unconverted CHF 12.63
        debit, which would otherwise share a fingerprint with a EUR 12.63 debit
        on the same day and silently lose one of the two. §A5 describes the
        intent — recognising the same operation seen twice — not an
        interoperability format, and cross-account duplicates always carry the
        same currency on both sides.
        """
        return self._fingerprint(self.effective_date) if self.effective_date else None

    @property
    def alternate_dedup_keys(self) -> list[str]:
        """Fingerprints this same operation may already be stored under, built
        from its other dates.

        A pending operation carries no booking_date, so it is keyed on its
        transaction_date; booking gives it a booking_date that differs from the
        transaction_date in the vast majority of the captured rows, which moves
        the retained date and therefore the fingerprint. The booked payload
        still carries the original transaction_date, so looking the other dates
        up as well is what lets level 2 recognise the pending row instead of
        leaving a ghost behind it (§E: a pending operation is never final).
        """
        seen = {self.effective_date}
        keys = []
        for day in (self.booking_date, self.transaction_date, self.value_date):
            if day is not None and day not in seen:
                seen.add(day)
                keys.append(self._fingerprint(day))
        return keys

    def _fingerprint(self, day: date) -> str:
        return "|".join(
            (day.isoformat(), _canonical_amount(self.amount), self.currency, self.credit_debit)
        )


def normalize_transaction(raw: dict[str, Any]) -> NormalizedTransaction:
    """Read one raw Enable Banking transaction payload."""
    amount_field = raw.get("transaction_amount") or {}
    try:
        amount = Decimal(str(amount_field.get("amount")))
    except InvalidOperation as exc:
        raise ValueError(f"transaction_amount.amount is not a decimal: {amount_field.get('amount')!r}") from exc

    # Direction and status are mandatory at the contract, like the amount.
    # Degrading them to "" would fold an empty direction into the fingerprint
    # and leave the row claimable forever, since "" is never a final status.
    credit_debit = raw.get("credit_debit_indicator")
    if not credit_debit:
        raise ValueError("credit_debit_indicator is required")
    status = raw.get("status")
    if not status:
        raise ValueError("status is required")

    currency = str(amount_field.get("currency") or BASE_CURRENCY)
    booking_date = _parse_date(raw.get("booking_date"))
    value_date = _parse_date(raw.get("value_date"))
    transaction_date = _parse_date(raw.get("transaction_date"))

    return NormalizedTransaction(
        entry_reference=raw.get("entry_reference") or None,
        amount=amount,
        currency=currency,
        credit_debit=str(credit_debit),
        status=str(status),
        booking_date=booking_date,
        value_date=value_date,
        transaction_date=transaction_date,
        # booking_date first (the date the bank recorded it), then the date the
        # operation happened, then the value date as a last resort.
        effective_date=booking_date or transaction_date or value_date,
        remittance=_join_remittance(raw.get("remittance_information")),
        unconverted=currency != BASE_CURRENCY,
    )


def store_transactions(
    session: Session,
    user_uuid: str,
    master_key: str,
    bank_account_uuid: str,
    transactions: Iterable[dict[str, Any]],
) -> tuple[int, int, int]:
    """Store an account's transaction feed, deduplicated. Returns
    (inserted, updated, skipped). The feed's order is never relied upon."""
    account_bidx = hash_index(bank_account_uuid, master_key)
    sibling_dedup = _sibling_dedup_indexes(session, user_uuid, master_key, account_bidx)

    existing = list(
        session.exec(
            select(BankTransaction).where(BankTransaction.account_id_bidx == account_bidx)
        ).all()
    )
    by_ref = {row.entry_ref_bidx: row for row in existing if row.entry_ref_bidx}
    by_dedup: dict[str, list[BankTransaction]] = defaultdict(list)
    for row in existing:
        by_dedup[row.dedup_bidx].append(row)
    # Rows this run already inserted or corrected. They come from the same feed
    # snapshot, so they can never be the earlier version of another transaction
    # in that same snapshot — level 2 must not claim them.
    touched: set[int] = set()

    inserted = updated = skipped = 0
    for raw in transactions:
        tx = normalize_transaction(raw)
        if tx.effective_date is None:
            skipped += 1
            continue

        ref_bidx = hash_index(tx.entry_reference, master_key) if tx.entry_reference else None
        dedup_bidx = hash_index(tx.dedup_key, master_key)

        row = by_ref.get(ref_bidx) if ref_bidx else None
        if row is None:
            row = _claimable_row(by_dedup.get(dedup_bidx, []), touched, master_key)
        if row is None:
            # Still level 2, and still confined to claimable rows: a pending row
            # is keyed on the date it had then, which booking moves.
            for alternate in tx.alternate_dedup_keys:
                candidates = by_dedup.get(hash_index(alternate, master_key), [])
                row = _claimable_row(candidates, touched, master_key)
                if row is not None:
                    break

        if row is not None:
            _drop_from_indexes(row, by_ref, by_dedup)
            if tx.status in INVALIDATING_STATUSES:
                session.delete(row)
            else:
                _apply(row, tx, ref_bidx, dedup_bidx, master_key)
                _add_to_indexes(row, by_ref, by_dedup)
                touched.add(id(row))
            updated += 1
            continue

        if tx.status in INVALIDATING_STATUSES:
            skipped += 1
            continue

        if dedup_bidx in sibling_dedup:
            skipped += 1
            continue

        row = BankTransaction(account_id_bidx=account_bidx)
        _apply(row, tx, ref_bidx, dedup_bidx, master_key)
        session.add(row)
        _add_to_indexes(row, by_ref, by_dedup)
        touched.add(id(row))
        inserted += 1

    session.commit()
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) and value else None


def _canonical_amount(amount: Decimal) -> str:
    """A single rendering per value, so "150" and "150.00" share a dedup_bidx.
    normalize() renders integers in exponent form (150 -> 1.5E+2); 'f' undoes it."""
    return format(amount.normalize(), "f")


def _join_remittance(lines: Any) -> str | None:
    """Multi-line remittance information, concatenated on a stable rule: each
    line stripped, empty lines dropped, joined by a single space."""
    if not isinstance(lines, list):
        return None
    parts = [str(line).strip() for line in lines if str(line).strip()]
    return " ".join(parts) or None


def _sibling_dedup_indexes(
    session: Session, user_uuid: str, master_key: str, account_bidx: str
) -> set[str]:
    """Dedup indexes already stored on the user's *other* linked accounts —
    level 3 is scoped to the user, not to the account."""
    linked = session.exec(
        select(BankAccountLink.bank_account_uuid_bidx).where(
            BankAccountLink.user_uuid_bidx == hash_index(user_uuid, master_key)
        )
    ).all()
    siblings = [bidx for bidx in linked if bidx != account_bidx]
    if not siblings:
        return set()
    return set(
        session.exec(
            select(BankTransaction.dedup_bidx).where(
                BankTransaction.account_id_bidx.in_(siblings)
            )
        ).all()
    )


def _claimable_row(
    candidates: list[BankTransaction], touched: set[int], master_key: str
) -> BankTransaction | None:
    """Level 2: among the rows sharing a dedup index on this account, the one
    the incoming transaction may correct — either a row the bank gave no
    reference for, or a non-final row whose reference can still change."""
    for row in candidates:
        if id(row) in touched:
            continue
        if row.entry_ref_bidx is None:
            return row
        if decrypt_data(row.status_enc, master_key) not in FINAL_STATUSES:
            return row
    return None


def _apply(
    row: BankTransaction,
    tx: NormalizedTransaction,
    ref_bidx: str | None,
    dedup_bidx: str,
    master_key: str,
) -> None:
    row.period_bidx = hash_index(tx.period, master_key)
    row.entry_ref_bidx = ref_bidx
    row.dedup_bidx = dedup_bidx
    row.amount_enc = encrypt_data(_canonical_amount(tx.amount), master_key)
    row.currency_enc = encrypt_data(tx.currency, master_key)
    row.credit_debit_enc = encrypt_data(tx.credit_debit, master_key)
    row.status_enc = encrypt_data(tx.status, master_key)
    row.booking_date_enc = _encrypt_date(tx.booking_date, master_key)
    row.value_date_enc = _encrypt_date(tx.value_date, master_key)
    row.transaction_date_enc = _encrypt_date(tx.transaction_date, master_key)
    row.remittance_enc = encrypt_data(tx.remittance, master_key) if tx.remittance else None


def _encrypt_date(value: date | None, master_key: str) -> str | None:
    return encrypt_data(value.isoformat(), master_key) if value else None


def _add_to_indexes(
    row: BankTransaction,
    by_ref: dict[str, BankTransaction],
    by_dedup: dict[str, list[BankTransaction]],
) -> None:
    if row.entry_ref_bidx:
        by_ref[row.entry_ref_bidx] = row
    by_dedup[row.dedup_bidx].append(row)


def _drop_from_indexes(
    row: BankTransaction,
    by_ref: dict[str, BankTransaction],
    by_dedup: dict[str, list[BankTransaction]],
) -> None:
    if row.entry_ref_bidx:
        by_ref.pop(row.entry_ref_bidx, None)
    siblings = by_dedup.get(row.dedup_bidx)
    if siblings:
        # Identity, not equality: SQLModel rows compare by field value.
        by_dedup[row.dedup_bidx] = [other for other in siblings if other is not row]
