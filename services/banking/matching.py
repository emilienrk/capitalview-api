"""
Confronting what the user declared with what actually moved.

`services/cashflow.py` holds declarations — "Loyer, 850 €, mensuel". This module
holds the other half: which real movements each declaration corresponds to, and
what the gap between the two is.

Nothing links the two automatically. A `Cashflow` carries a name the user wrote;
the bank writes `PRLV SEPA FONCIA`. So the app proposes and the user confirms
once, and the confirmed `match_pattern` is what makes the link durable.

The label signature carries **no bank vocabulary at all** — no list of French
prefixes, no merchant dictionary. It keeps the alphabetic tokens and drops every
alphanumeric run containing a digit, which is what dates, references and card
suffixes look like in every format seen so far. Measured on 4 240 real rows: it
merges `CARTE ATMB CB*0837` with `CARTE ATMB` and brings 1 252 raw variants down
to 815 stable groups.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlmodel import Session, select

from dtos.cashflow import CashflowComparison, MatchCandidate, RecentOccurrence
from models.banking import BankTransaction
from models.cashflow import Cashflow
from models.enums import FlowType, Frequency
from services.banking.linking import unmirrored_account_bidxs
from services.banking.transactions import FINAL_STATUSES
from services.encryption import decrypt_data, encrypt_data, hash_index

CREDIT = "CRDT"

# Tokens kept in a signature. Past this, labels are padding and reference noise;
# below it, unrelated merchants start colliding.
SIGNATURE_TOKENS = 4

# A group has to be seen this many times before it can be proposed as a match:
# a single movement is an event, not a recurrence.
MIN_OCCURRENCES = 2

# How far the observed amount may sit from the declared one before it reads as a
# drift rather than rounding. Tight on purpose: at 5 % a 850 € rent could climb
# by 42 € — 500 € a year — without a word, which is exactly what this is meant to
# catch. The absolute floor keeps a 12,50 € subscription from being flagged over
# a cent, and the comparison runs on the median, so a one-off month cannot move
# the verdict on its own.
DRIFT_RATIO = Decimal("0.02")
DRIFT_FLOOR = Decimal("1")

# Expected gap between two occurrences, in days, per declared frequency.
_CADENCE_DAYS = {
    Frequency.DAILY: 1,
    Frequency.WEEKLY: 7,
    Frequency.MONTHLY: 30,
    Frequency.YEARLY: 365,
}

# Missed by this many cadences in a row and the flow reads as gone, not late.
MISSING_CADENCE_FACTOR = 2.5

# How much spacing counts against the amount when ranking candidates. Small on
# purpose: it is a tie-breaker between groups of the same amount, not evidence.
CADENCE_WEIGHT = 0.1

# How much regularity counts. A group seen twice in six months whose two dates
# happen to sit 30 days apart looks perfectly monthly and means nothing; one seen
# five times does. Measured against the occurrences the declared frequency
# implies over the window, so it needs no vocabulary and no merchant list.
COVERAGE_WEIGHT = 0.3

# How many candidates the user is offered. Amount alone cannot separate a 59,04 €
# fuel purchase seen six times from a 60,00 € electricity bill seen five times —
# no scoring fixes that, so the choice is handed over instead of guessed at.
MAX_CANDIDATES = 5


def label_signature(text: str | None) -> str:
    """The stable part of a transaction label.

    Accents folded, case dropped, and every run containing a digit removed whole
    — `CB*0837`, `03/08/25` and `4382956` carry no identity, and keeping them
    would give the same merchant a new signature on every purchase.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFD", text.upper())
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    runs = re.findall(r"[A-Z0-9*/.]+", folded)
    tokens = [run for run in runs if run.isalpha() and len(run) >= 2]
    return " ".join(tokens[:SIGNATURE_TOKENS])


class Occurrence(NamedTuple):
    day: date
    amount: Decimal
    is_credit: bool


class SignatureGroup(NamedTuple):
    signature: str
    occurrences: list[Occurrence]

    @property
    def median_amount(self) -> Decimal:
        return Decimal(str(statistics.median([float(o.amount) for o in self.occurrences])))

    @property
    def last_day(self) -> date:
        return max(o.day for o in self.occurrences)

    @property
    def median_gap_days(self) -> int | None:
        """Typical spacing between two occurrences, or None below two of them."""
        days = sorted(o.day for o in self.occurrences)
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        return int(statistics.median(gaps)) if gaps else None


def load_signature_groups(
    session: Session, user_uuid: str, master_key: str, since: date
) -> dict[str, SignatureGroup]:
    """Every booked movement since `since`, grouped by label signature."""
    user_bidx = hash_index(user_uuid, master_key)
    # A mirrored card account is left out: its rows repeat the current
    # account's, and a signature seen twice for one movement would inflate the
    # occurrence count MIN_OCCURRENCES and the `duplicated` verdict rest on.
    account_bidxs = unmirrored_account_bidxs(session, user_bidx, master_key)
    if not account_bidxs:
        return {}

    buckets: dict[str, list[Occurrence]] = defaultdict(list)
    rows = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx.in_(account_bidxs)  # type: ignore[attr-defined]
        )
    ).all()
    for row in rows:
        if decrypt_data(row.status_enc, master_key) not in FINAL_STATUSES:
            continue
        day = _row_day(row, master_key)
        if day is None or day < since:
            continue
        signature = label_signature(
            decrypt_data(row.remittance_enc, master_key) if row.remittance_enc else None
        )
        if not signature:
            continue
        buckets[signature].append(
            Occurrence(
                day=day,
                amount=abs(Decimal(decrypt_data(row.amount_enc, master_key))),
                is_credit=decrypt_data(row.credit_debit_enc, master_key) == CREDIT,
            )
        )
    return {
        signature: SignatureGroup(signature, sorted(items))
        for signature, items in buckets.items()
    }


def _row_day(row: BankTransaction, master_key: str) -> date | None:
    for column in (row.booking_date_enc, row.transaction_date_enc, row.value_date_enc):
        if column:
            return date.fromisoformat(decrypt_data(column, master_key))
    return None


def rank_candidates(
    groups: dict[str, SignatureGroup],
    amount: Decimal,
    flow_type: FlowType,
    frequency: Frequency,
    window_days: int,
) -> list[MatchCandidate]:
    """The groups that could be this declaration, best first.

    Scored on how close the amount sits, how regular the group is, and how well
    its spacing matches the declared frequency. Always a proposal, never an
    assignment: two subscriptions at 9,99 € are indistinguishable on amount
    alone, and a fuel card seen every month is a better statistical match for
    "58,55 € monthly" than the electricity bill actually meant. Only the user
    knows which is which, so several are offered rather than one guessed at.
    """
    wants_credit = flow_type == FlowType.INFLOW
    expected_gap = _CADENCE_DAYS.get(frequency)
    expected_count = max(1, window_days // expected_gap) if expected_gap else 1

    scored: list[tuple[float, MatchCandidate]] = []
    for signature, group in groups.items():
        matching = [o for o in group.occurrences if o.is_credit == wants_credit]
        if len(matching) < MIN_OCCURRENCES:
            continue
        median = Decimal(str(statistics.median([float(o.amount) for o in matching])))
        if median <= 0 or amount <= 0:
            continue
        distance = abs(float(median - amount)) / float(amount)
        if distance > 0.5:
            continue
        coverage = min(1.0, len(matching) / expected_count)
        score = (
            distance
            + CADENCE_WEIGHT * _cadence_distance(signature, matching, expected_gap)
            + COVERAGE_WEIGHT * (1 - coverage)
        )
        scored.append((
            score,
            MatchCandidate(
                pattern=signature,
                observed_amount=median,
                occurrences=len(matching),
                last_seen=max(o.day for o in matching),
            ),
        ))
    scored.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in scored[:MAX_CANDIDATES]]


def _cadence_distance(
    signature: str, occurrences: list[Occurrence], expected_gap: int | None
) -> float:
    """How far this group's spacing sits from the declared frequency, relative."""
    if not expected_gap:
        return 0.0
    gap = SignatureGroup(signature, occurrences).median_gap_days
    return abs(gap - expected_gap) / expected_gap if gap else 0.0


def get_match_pattern(cashflow: Cashflow, master_key: str) -> str | None:
    return (
        decrypt_data(cashflow.match_pattern_enc, master_key)
        if cashflow.match_pattern_enc
        else None
    )


def set_match_pattern(cashflow: Cashflow, pattern: str | None, master_key: str) -> None:
    """An empty pattern clears the link rather than storing a blank one."""
    cleaned = (pattern or "").strip()
    cashflow.match_pattern_enc = encrypt_data(cleaned, master_key) if cleaned else None


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------

# Statuses, in the order they are decided.
UNMATCHED = "unmatched"      # never confirmed against a label
MISSING = "missing"          # confirmed, but nothing has moved for a long while
DUPLICATED = "duplicated"    # seen more often than the declared frequency allows
DRIFTED = "drifted"          # it does move, for a different amount than declared
ON_TRACK = "on_track"


def compare_cashflows(
    session: Session,
    user_uuid: str,
    master_key: str,
    months: int = 6,
    today: date | None = None,
) -> list[CashflowComparison]:
    """One verdict per active declaration, newest movements first."""
    anchor = today or date.today()
    since = _months_before(anchor, months)
    groups = load_signature_groups(session, user_uuid, master_key, since)

    user_bidx = hash_index(user_uuid, master_key)
    cashflows = session.exec(
        select(Cashflow).where(Cashflow.user_uuid_bidx == user_bidx)
    ).all()

    results = []
    for cashflow in cashflows:
        if cashflow.is_active_enc and decrypt_data(cashflow.is_active_enc, master_key) == "false":
            continue
        results.append(
            _compare_one(cashflow, groups, master_key, anchor, (anchor - since).days)
        )
    return results


def _months_before(anchor: date, months: int) -> date:
    year, month = anchor.year, anchor.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    return date(year, month, 1)


def _compare_one(
    cashflow: Cashflow,
    groups: dict[str, SignatureGroup],
    master_key: str,
    anchor: date,
    window_days: int,
) -> CashflowComparison:
    name = decrypt_data(cashflow.name_enc, master_key)
    flow_type = FlowType(decrypt_data(cashflow.flow_type_enc, master_key))
    frequency = Frequency(decrypt_data(cashflow.frequency_enc, master_key))
    declared = Decimal(decrypt_data(cashflow.amount_enc, master_key))
    pattern = get_match_pattern(cashflow, master_key)

    base = dict(
        cashflow_id=cashflow.uuid,
        name=name,
        flow_type=flow_type,
        frequency=frequency,
        declared_amount=declared,
        category=decrypt_data(cashflow.category_enc, master_key),
        match_pattern=pattern,
    )

    if pattern is None:
        return CashflowComparison(
            **base,
            status=UNMATCHED,
            candidates=rank_candidates(groups, declared, flow_type, frequency, window_days),
        )

    group = groups.get(pattern)
    wants_credit = flow_type == FlowType.INFLOW
    occurrences = (
        [o for o in group.occurrences if o.is_credit == wants_credit] if group else []
    )
    if not occurrences:
        return CashflowComparison(**base, status=MISSING)

    matched = SignatureGroup(pattern, occurrences)
    observed = matched.median_amount
    last_seen = matched.last_day
    recent = [
        RecentOccurrence(day=o.day, amount=o.amount) for o in sorted(occurrences)[-4:]
    ]

    # Gone quiet: past a couple of cadences the flow is not late any more.
    expected_gap = _CADENCE_DAYS.get(frequency)
    if expected_gap and (anchor - last_seen).days > expected_gap * MISSING_CADENCE_FACTOR:
        return CashflowComparison(
            **base, status=MISSING, observed_amount=observed,
            last_seen=last_seen, occurrences=len(occurrences), recent=recent,
        )

    # Seen twice where the declared cadence allows once. Reported, never "fixed":
    # a double debit is something to take up with the bank, not to normalise.
    if expected_gap:
        window_start = anchor.replace(day=1)
        in_window = [o for o in occurrences if o.day >= window_start]
        if expected_gap >= 28 and len(in_window) > 1:
            return CashflowComparison(
                **base, status=DUPLICATED, observed_amount=observed,
                last_seen=last_seen, occurrences=len(occurrences), recent=recent,
            )

    tolerance = max(declared * DRIFT_RATIO, DRIFT_FLOOR)
    status = DRIFTED if abs(observed - declared) > tolerance else ON_TRACK
    return CashflowComparison(
        **base, status=status, observed_amount=observed, last_seen=last_seen,
        occurrences=len(occurrences), recent=recent,
    )
