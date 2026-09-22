"""
Observed cash flows: what actually moved on the linked accounts.

The counterpart of `services/cashflow.py`, which holds what the user *declared*
would move. Everything here is derived from stored `BankTransaction` rows, so it
needs no network and no Enable Banking credentials.

Two readers share one pipeline — load, pair the internal transfers, aggregate —
so the monthly totals and the list of a month's operations can never disagree:
`compute_real_flows` sums months, `list_month_transactions` lists one of them.

Deliberately bank-agnostic. It reads only the three fields the Enable Banking
contract marks required on every transaction — amount, currency and
`credit_debit_indicator` — plus the status. It never parses a label: the
`remittance_information` format is the bank's own invention (Boursorama writes
`CARTE 03/08/25 AIRBNB * HMFYWK533K`, another writes something else), and the
structured fields that would replace it — `merchant_category_code`,
`bank_transaction_code`, `creditor` — are empty on all 4 240 real rows captured
so far. The label is only ever handed back as-is, for the user to read.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

from sqlmodel import Session, select

from dtos.banking import (
    BankContributionMatch,
    BankFlowCurrencyTotal,
    BankFlowMonth,
    BankFlowQuestion,
    BankFlowsResponse,
    BankReviewItem,
    BankReviewKind,
    BankReviewQueue,
    BankReviewYear,
    BankRecurringQuestion,
    BankRecurringTag,
    BankTransactionItem,
    BankTransactionsResponse,
    BankTransferDecisionKind,
    BankTransactionTypeResult,
    BankTransferStatus,
    BankTypeRuleItem,
    CashflowType,
    OperationType,
    TypeScope,
    TypeSource,
)
from models.bank import BankAccount
from models.banking import BankAccountLink, BankTransaction
from models.currency import BASE_CURRENCY
from models.enums import BankAccountType
from services.banking import recurring_series
from services.banking import transfer_patterns as stored_patterns
from services.banking.cashflow_types import Resolution, resolve_type
from services.banking.contributions import (
    Candidate,
    Contributions,
    Match,
    load_contributions,
    match_candidates,
)
from services.banking.linking import readable_account_bidxs
from services.banking.operation_types import operation_type
from services.banking.recurring_decisions import load_decisions as load_recurring_decisions
from services.banking.transactions import (
    CREDIT,
    FINAL_STATUSES,
    label_signature,
    label_words,
    row_date,
)
from services.banking.transfer_decisions import (
    MAX_DECISION_DAYS,
    Decisions,
    TransactionNotFoundError,
    Verdict,
    load_decisions,
)
from services.banking.transfer_patterns import FlowCarrier, TransferPatterns
from services.banking.type_rules import TypeRules, load_rules, save_rule, telling_words
from services.encryption import decrypt_data, encrypt_data, hash_index

logger = logging.getLogger(__name__)


# How far apart the two legs of one internal transfer may be dated, in banking
# days. Counted in calendar days, a Thursday debit landing on Monday or a card
# top-up booked after Easter reads as five days apart for what is one working
# day of settlement — measured on real data, those were the pairs being missed.
TRANSFER_TOLERANCE_BANKING_DAYS = 2
# Beyond this, no calendar holds enough closed days to stay inside the tolerance.
_TRANSFER_MAX_CALENDAR_DAYS = 10

# A refund lands on the account that paid, within this many days of the payment.
REFUND_MAX_DAYS = 30
# A word on more than this share of one side of an account says nothing about
# which operations belong together: "CARTE" on debits, "AVOIR" or "VIR" on credits.
COMMON_WORD_SHARE = 0.05
# Below this many occurrences a word is never common: on an account holding a
# handful of operations, the share alone would call every word common.
COMMON_WORD_MIN_COUNT = 3

# Regulated French savings accounts only ever move money to and from their
# holder's own current account: a pair touching one is a transfer by law. A
# generic SAVINGS account carries no such rule.
REGULATED_SAVINGS = frozenset({
    BankAccountType.LIVRET_A, BankAccountType.LIVRET_DEVE, BankAccountType.LEP,
    BankAccountType.LDD, BankAccountType.PEL, BankAccountType.CEL,
})

# Every account holding money set aside, regulated or not: a transfer with one
# of these on exactly one side is saving, not spending.
SAVINGS_ACCOUNTS = REGULATED_SAVINGS | {BankAccountType.SAVINGS}

# The pairs kept out of the totals. A suggested pair is only offered: until the
# user settles it, both legs count — measured, most pairs seen once were a third
# party refunding a purchase, not a transfer.
DEDUCTED = frozenset({
    BankTransferStatus.SAVINGS, BankTransferStatus.RECURRING, BankTransferStatus.LEARNED,
    BankTransferStatus.CONFIRMED, BankTransferStatus.REVERSAL, BankTransferStatus.REFUND,
})
_CANCELLATIONS = frozenset({BankTransferStatus.REVERSAL, BankTransferStatus.REFUND})

# What a flow question offers. A credit may be income, a refund (typed as an
# expense taken back), money taken back from savings or an investment, or
# nothing at all; a transfer sent, anything but income.
CREDIT_CHOICES = [
    CashflowType.INCOME, CashflowType.EXPENSE, CashflowType.SAVING, CashflowType.INVESTMENT, CashflowType.NEUTRAL,
]
DEBIT_CHOICES = [CashflowType.EXPENSE, CashflowType.SAVING, CashflowType.INVESTMENT, CashflowType.NEUTRAL]

# A label whose operations add up to less than this over the whole history asks
# nothing: its default stands. Measured on 53 months of real operations, 186
# labels asked; under 100 € sat 85 of them, 2 393 € in all — 1.4 % of what the
# questions weighed. A label that grows past it later starts asking.
FLOW_QUESTION_MIN_AMOUNT = Decimal("100")

DEFAULT_MONTHS = 12
MAX_MONTHS = 120


class UnknownAccountError(LookupError):
    """The account filter names no bank account of this user."""


class PairedOperationError(ValueError):
    """A paired operation takes its type from its pair, undone by a transfer decision."""


class LabelRequiredError(ValueError):
    """Only an operation with a label can type the operations reading like it."""


class _Movement(NamedTuple):
    row: BankTransaction
    account_bidx: str
    period: str
    day: date | None
    amount: Decimal
    currency: str
    is_credit: bool
    is_final: bool


class _TransferLeg(NamedTuple):
    other: int
    status: BankTransferStatus


class _Accounts(NamedTuple):
    """The user's bank accounts, keyed by the blind index movements carry."""
    by_bidx: dict[str, BankAccount]
    # Only those whose movements a reader may sum (see readable_account_bidxs).
    readable: list[str]


class _Pairing(NamedTuple):
    """What the pairing needs beyond the movements themselves."""
    master_key: str
    decisions: Decisions
    patterns: TransferPatterns
    savings: frozenset[str]


class _Filing(NamedTuple):
    """What reading how each operation counts needs, loaded once per request."""
    master_key: str
    savings: frozenset[str]
    rules: TypeRules
    patterns: TransferPatterns
    # Movement index -> the deposit or withdrawal the user declared facing it.
    # Resolved over the whole set of movements, since a deposit proves one of
    # them at most (services/banking/contributions.py).
    contributions: dict[int, Match]


@dataclass
class _Totals:
    currency: str
    months: list[BankFlowMonth]
    pending_count: int
    pending_inflow: Decimal
    pending_outflow: Decimal
    transfers_count: int
    transfers_amount: Decimal
    questions_count: int
    reversals_count: int
    reversals_amount: Decimal
    other_currencies: list[BankFlowCurrencyTotal]


def _months_back(anchor: date, months: int) -> list[str]:
    """The `months` "YYYY-MM" periods ending on `anchor`'s own month."""
    periods = []
    year, month = anchor.year, anchor.month
    for _ in range(months):
        periods.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(periods))


def _shift_period(period: str, months: int) -> str:
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


@lru_cache(maxsize=64)
def _target_closing_days(year: int) -> frozenset[date]:
    """The days TARGET2, the euro area's settlement system, does not settle.

    One calendar for every euro bank, unlike national public holidays."""
    easter = _easter_sunday(year)
    return frozenset({
        date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
        date(year, 5, 1), date(year, 12, 25), date(year, 12, 26),
    })


def _banking_days_between(first: date, second: date) -> int:
    """Settlement days stepped over going from the earlier date to the later one."""
    low, high = sorted((first, second))
    count = 0
    day = low
    while day < high:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in _target_closing_days(day.year):
            count += 1
    return count


def _internal_transfer_legs(
    movements: list[_Movement], pairing: _Pairing | None = None
) -> dict[int, _TransferLeg]:
    """Movements that pair up — as one transfer between the user's own accounts,
    or as an operation and its cancellation on one — each index mapped to its
    other leg's, with how sure the pair is.

    A transfer inflates both sides of the summary: it leaves one linked account
    and lands on another, so counting it makes the user look like they earn and
    spend money they merely moved. Candidates are found on the only signals
    every bank supplies — opposite direction, identical amount and currency,
    different accounts, dates close together. Those signals alone cannot tell a
    transfer from a third party refunding a purchase to the cent on the other
    account, so each pair is then settled, first rule that applies:

    1. the user bound it (services/banking/transfer_decisions.py);
    2. it touches a regulated savings account;
    3. its shape recurs across the history (services/banking/transfer_patterns.py);
    4. both labels read like pairs the user confirmed;
    5. otherwise it is only suggested, and both legs keep counting.

    Refunds on one account come between the last two: same amount, the credit
    within a month of the debit, and a word the two labels share that the
    account does not use everywhere — the merchant's name.

    One-to-one, and settled globally, closest pairs first: walking the debits in
    date order let an earlier coincidental debit claim a credit whose true debit
    was booked the next day, leaving both that debit and the true pair unmatched.

    Without `pairing`, every candidate comes back suggested: the bare structure
    the patterns are counted on.
    """
    paired: dict[int, _TransferLeg] = {}

    def claim(debit: int, credit: int, status: BankTransferStatus) -> None:
        paired[debit] = _TransferLeg(credit, status)
        paired[credit] = _TransferLeg(debit, status)

    decisions = pairing.decisions if pairing else Decisions()
    ref_of: list[str | None] = [None] * len(movements)
    if pairing and decisions:
        ref_of = [hash_index(m.row.uuid, pairing.master_key) for m in movements]
    refs = {ref: i for i, ref in enumerate(ref_of) if ref is not None}
    for (debit_ref, credit_ref), kind in decisions.bound.items():
        debit, credit = refs.get(debit_ref), refs.get(credit_ref)
        if debit is None or credit is None or debit in paired or credit in paired:
            continue
        d, c = movements[debit], movements[credit]
        # A pending movement can be rewritten when it books: a decision about
        # what it used to say no longer holds.
        same_account = d.account_bidx == c.account_bidx
        if (
            d.is_credit or not c.is_credit or d.amount != c.amount or d.currency != c.currency
            or same_account != (kind is BankTransferDecisionKind.REVERSAL)
        ):
            continue
        claim(debit, credit, BankTransferStatus.REVERSAL if same_account else BankTransferStatus.CONFIRMED)
    rejected = {
        (refs[debit_ref], refs[credit_ref])
        for debit_ref, credit_ref in decisions.rejected
        if debit_ref in refs and credit_ref in refs
    }

    words: dict[int, frozenset[str]] = {}

    def words_of(index: int) -> frozenset[str]:
        if index not in words:
            label = movements[index].row.remittance_enc
            words[index] = label_words(decrypt_data(label, pairing.master_key)) if label and pairing else frozenset()
        return words[index]

    def verdict(index: int) -> Verdict | None:
        movement = movements[index]
        return decisions.memory.verdict(
            movement.account_bidx, movement.is_credit, words_of(index), ref_of[index]
        )

    by_key: dict[tuple[str, Decimal], tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
    for index, movement in enumerate(movements):
        if movement.day is not None and index not in paired:
            by_key[(movement.currency, movement.amount)][movement.is_credit].append(index)

    settled: list[tuple[int, int, int, int, BankTransferStatus]] = []
    offered: list[tuple[int, int, int, int, BankTransferStatus]] = []
    refunds: list[tuple[int, int, int, BankTransferStatus]] = []
    for debits, credits in by_key.values():
        for debit in debits:
            d = movements[debit]
            for credit in credits:
                c = movements[credit]
                if (debit, credit) in rejected:
                    continue
                if c.account_bidx == d.account_bidx:
                    gap = (c.day - d.day).days
                    if pairing and 0 <= gap <= REFUND_MAX_DAYS and _share_a_telling_word(
                        pairing.patterns, d, c, words_of(debit), words_of(credit)
                    ):
                        refunds.append((gap, debit, credit, BankTransferStatus.REFUND))
                    continue
                calendar_gap = abs((c.day - d.day).days)
                if calendar_gap > _TRANSFER_MAX_CALENDAR_DAYS:
                    continue
                banking_gap = _banking_days_between(d.day, c.day)
                if banking_gap > TRANSFER_TOLERANCE_BANKING_DAYS:
                    continue
                status = _transfer_status(pairing, d, c, debit, credit, verdict) if pairing else BankTransferStatus.SUGGESTED
                if status is None:
                    continue
                # The calendar gap breaks ties: Friday to Sunday is zero banking
                # days, and still further apart than a same-day leg.
                entry = (banking_gap, calendar_gap, debit, credit, status)
                (offered if status is BankTransferStatus.SUGGESTED else settled).append(entry)

    for tier in (settled, refunds, offered):
        free = [entry for entry in sorted(tier) if entry[-3] not in paired and entry[-2] not in paired]
        for debit, credit, status in _closest_complete_matching(free):
            claim(debit, credit, status)
    return paired


def _closest_complete_matching(
    candidates: list[tuple],
) -> list[tuple[int, int, BankTransferStatus]]:
    """Closest pairs first, then no leg left out that could have been paired.

    Closest-first alone strands a leg whenever two debits of one amount sit a
    day either side of two credits: the first pair formed takes the credit the
    other debit needed, while swapping would have paired all four. Each debit
    left over then looks for such a swap (Kuhn's augmenting paths) — which only
    ever adds pairs, never undoes the closest-first choice where no swap helps.
    `candidates` come sorted, each ending with (debit, credit, status).
    """
    credit_of: dict[int, int] = {}
    debit_of: dict[int, int] = {}
    status: dict[tuple[int, int], BankTransferStatus] = {}
    options: dict[int, list[int]] = defaultdict(list)
    for *_, debit, credit, pair_status in candidates:
        status[(debit, credit)] = pair_status
        options[debit].append(credit)
        if debit not in credit_of and credit not in debit_of:
            credit_of[debit], debit_of[credit] = credit, debit

    def augment(debit: int, visited: set[int]) -> bool:
        for credit in options[debit]:
            if credit in visited:
                continue
            visited.add(credit)
            if credit not in debit_of or augment(debit_of[credit], visited):
                credit_of[debit], debit_of[credit] = credit, debit
                return True
        return False

    for debit in list(options):
        if debit not in credit_of:
            augment(debit, set())
    return [(debit, credit, status[(debit, credit)]) for debit, credit in credit_of.items()]


def _transfer_status(
    pairing: _Pairing, d: _Movement, c: _Movement, debit: int, credit: int, verdict
) -> BankTransferStatus | None:
    """How sure a candidate transfer is; None when a leg reads like one the user
    said was not theirs, and it is not even offered."""
    if pairing.decisions.memory:
        legs = (verdict(debit), verdict(credit))
        if Verdict.OTHER in legs:
            return None
    else:
        legs = (None, None)
    if d.account_bidx in pairing.savings or c.account_bidx in pairing.savings:
        return BankTransferStatus.SAVINGS
    if pairing.patterns.recurs(
        d.account_bidx, c.account_bidx, d.row.label_signature_bidx, c.row.label_signature_bidx
    ):
        return BankTransferStatus.RECURRING
    if legs == (Verdict.OWN, Verdict.OWN):
        return BankTransferStatus.LEARNED
    return BankTransferStatus.SUGGESTED


def _share_a_telling_word(
    patterns: TransferPatterns, d: _Movement, c: _Movement, debit_words: frozenset[str], credit_words: frozenset[str]
) -> bool:
    """Whether a payment and a credit of its amount on the same account name the
    same thing. Each label is stripped of the words the other side of the
    account uses everywhere, so a shared "CB" proves nothing and a shared
    merchant name does."""
    telling_debit = debit_words - patterns.common(d.account_bidx, is_credit=True)
    telling_credit = credit_words - patterns.common(c.account_bidx, is_credit=False)
    return bool(telling_debit & telling_credit)


def _user_accounts(session: Session, user_uuid: str, master_key: str) -> _Accounts:
    user_bidx = hash_index(user_uuid, master_key)
    by_bidx = {
        hash_index(account.uuid, master_key): account
        for account in session.exec(
            select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
        ).all()
    }
    # `BankAccountLink.bank_account_uuid_bidx` and `BankTransaction.account_id_bidx`
    # are the same blind index of the same CapitalView account uuid. Linked
    # accounts and CSV-imported ones alike.
    return _Accounts(by_bidx, readable_account_bidxs(session, user_bidx, master_key))


def _links(session: Session, user_uuid: str, master_key: str) -> dict[str, date]:
    """Each linked account's last successful sync, by its blind index."""
    return {
        link.bank_account_uuid_bidx: link.last_synced_at
        for link in session.exec(
            select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == hash_index(user_uuid, master_key))
        ).all()
    }


def _scope(accounts: _Accounts, account_id: str | None, master_key: str) -> list[str]:
    """The accounts a reader asked about, among those it may read."""
    if account_id is None:
        return accounts.readable
    bidx = hash_index(account_id, master_key)
    if bidx not in accounts.by_bidx:
        raise UnknownAccountError(account_id)
    # A manual account nobody imported anything into is the user's, but has
    # nothing to show: an empty answer, not a missing one.
    return [bidx] if bidx in accounts.readable else []


def _load_movements(
    session: Session, master_key: str, account_bidxs: list[str], periods: list[str] | None
) -> list[_Movement]:
    """The movements of `periods` (every one when None), sorted.

    Sorted before anything reads an index: the database returns rows in no
    promised order, and transfer pairing would otherwise hand back a different
    answer for the same data from one call to the next.
    """
    query = select(BankTransaction).where(
        BankTransaction.account_id_bidx.in_(account_bidxs)  # type: ignore[attr-defined]
    )
    period_of: dict[str, str] = {}
    if periods is not None:
        period_of = {hash_index(p, master_key): p for p in periods}
        query = query.where(BankTransaction.period_bidx.in_(list(period_of)))  # type: ignore[attr-defined]

    movements: list[_Movement] = []
    for row in session.exec(query).all():
        day = row_date(row, master_key)
        movements.append(_Movement(
            row=row,
            account_bidx=row.account_id_bidx,
            period=period_of[row.period_bidx] if periods is not None else (f"{day:%Y-%m}" if day else ""),
            day=day,
            amount=Decimal(decrypt_data(row.amount_enc, master_key)),
            currency=decrypt_data(row.currency_enc, master_key),
            is_credit=decrypt_data(row.credit_debit_enc, master_key) == CREDIT,
            is_final=decrypt_data(row.status_enc, master_key) in FINAL_STATUSES,
        ))
    movements.sort(
        key=lambda m: (m.day or date.min, m.account_bidx, m.amount, m.is_credit, m.row.uuid)
    )
    return movements


def _pairing(session: Session, user_uuid: str, master_key: str, accounts: _Accounts) -> _Pairing:
    return _Pairing(
        master_key=master_key,
        decisions=load_decisions(session, user_uuid, master_key),
        patterns=transfer_patterns(session, user_uuid, master_key, accounts),
        savings=_regulated_savings(accounts, master_key),
    )


def _regulated_savings(accounts: _Accounts, master_key: str) -> frozenset[str]:
    return _accounts_of_types(accounts, REGULATED_SAVINGS, master_key)


def _savings_accounts(accounts: _Accounts, master_key: str) -> frozenset[str]:
    return _accounts_of_types(accounts, SAVINGS_ACCOUNTS, master_key)


def _accounts_of_types(accounts: _Accounts, types: frozenset[BankAccountType], master_key: str) -> frozenset[str]:
    return frozenset(
        bidx for bidx, account in accounts.by_bidx.items()
        if decrypt_data(account.account_type_enc, master_key) in types
    )


def _filing(
    session: Session,
    user_uuid: str,
    master_key: str,
    accounts: _Accounts,
    patterns: TransferPatterns,
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
) -> _Filing:
    """Everything the reading needs, the movements included: the deposits facing
    them are matched one for one, which no single operation can decide alone.

    Every reader passes the movements it loaded, so they all read one operation
    the same way — the whole point of `_filed`.
    """
    return _Filing(
        master_key=master_key,
        savings=_savings_accounts(accounts, master_key),
        rules=load_rules(session, user_uuid, master_key),
        patterns=patterns,
        contributions=_contributions(
            movements, transfer_legs, master_key, load_contributions(session, user_uuid, master_key)
        ),
    )


def _contributions(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    master_key: str,
    contributions: Contributions,
) -> dict[int, Match]:
    """The declared movement facing each bank movement, where there is one.

    Offered to the matching: every operation whose question only the user could
    answer otherwise — an unpaired credit, or an unpaired transfer sent
    (`_asks_flow`). What the user or a rule already settled is offered too and
    stays typed by them: `resolve_type` reads the deduction last.
    """
    candidates = [
        Candidate(index, movement.day, movement.amount, movement.is_credit)
        for index, movement in enumerate(movements)
        if movement.is_final
        and movement.day is not None
        and movement.currency == BASE_CURRENCY
        and index not in transfer_legs
        and (movement.is_credit or _operation_type(movement, master_key) is OperationType.TRANSFER)
    ]
    return match_candidates(candidates, contributions)


def _operation_type(movement: _Movement, master_key: str) -> OperationType:
    """Read from the stored type, from the label when the rebuild has not
    reached the row yet."""
    stored = movement.row.operation_type_enc
    if stored:
        return OperationType(decrypt_data(stored, master_key))
    return operation_type(_label(movement, master_key))


def _label(movement: _Movement, master_key: str) -> str | None:
    return decrypt_data(movement.row.remittance_enc, master_key) if movement.row.remittance_enc else None


def _paired_movements(
    session: Session,
    master_key: str,
    account_bidxs: list[str],
    periods: list[str],
    pairing: _Pairing | None,
) -> tuple[list[_Movement], dict[int, _TransferLeg]]:
    """Every movement of `periods` and of the month either side, sorted, with
    the pairs settled — none when `pairing` is None.

    Loaded across *every* readable account, whatever the reader filters on
    afterwards: a transfer's other leg sits on another account by definition.
    And a month past each edge, because a transfer booked on the 30th may land
    on the 2nd — without it the first month of a window would count as spending
    what the same month reads as a transfer once it is no longer on the edge.
    """
    padded = [_shift_period(periods[0], -1), *periods, _shift_period(periods[-1], 1)]
    movements = _load_movements(session, master_key, account_bidxs, padded)
    if pairing is None:
        return movements, {}
    return movements, _internal_transfer_legs(movements, pairing)


def transfer_patterns(
    session: Session,
    user_uuid: str,
    master_key: str,
    accounts: _Accounts | None = None,
    rebuild: bool = False,
) -> TransferPatterns:
    """The user's transfer patterns, rebuilt first when the data moved since.

    The rebuild reads the whole history once: it backfills the label signature
    of rows stored before signatures existed and the operation type of every
    row the lexicon now reads differently, counts the shape of every
    candidate pair, finds the words too common on each side of an account, and
    counts the pairs left for the user to settle once all of that applies.
    """
    accounts = accounts or _user_accounts(session, user_uuid, master_key)
    user_bidx = hash_index(user_uuid, master_key)
    savings = _regulated_savings(accounts, master_key)
    source = stored_patterns.source_digest(session, user_bidx, accounts.readable, savings, master_key)
    if not rebuild:
        stored = stored_patterns.read_patterns(session, user_bidx, source, master_key)
        if stored is not None:
            return stored

    movements = _load_movements(session, master_key, accounts.readable, None)
    labels = {
        i: decrypt_data(m.row.remittance_enc, master_key) if m.row.remittance_enc else None
        for i, m in enumerate(movements)
    }
    backfilled = False
    kinds: list[OperationType] = []
    for i, movement in enumerate(movements):
        row = movement.row
        changed = False
        signature = label_signature(labels[i])
        if row.label_signature_bidx is None and signature is not None:
            row.label_signature_bidx = hash_index(signature, master_key)
            changed = True
        # Every row, not only those stored before types existed: this is how a
        # change to the lexicon reaches the history.
        kinds.append(operation_type(labels[i]))
        kind = kinds[-1].value
        if row.operation_type_enc is None or decrypt_data(row.operation_type_enc, master_key) != kind:
            row.operation_type_enc = encrypt_data(kind, master_key)
            changed = True
        if changed:
            session.add(row)
            backfilled = True
    if backfilled:
        session.commit()
        source = stored_patterns.source_digest(session, user_bidx, accounts.readable, savings, master_key)

    patterns = TransferPatterns()
    for debit, leg in _internal_transfer_legs(movements).items():
        d, c = movements[debit], movements[leg.other]
        if d.is_credit or d.row.label_signature_bidx is None or c.row.label_signature_bidx is None:
            continue
        key = stored_patterns.shape_key(
            d.account_bidx, c.account_bidx, d.row.label_signature_bidx, c.row.label_signature_bidx
        )
        patterns.shapes[key] = patterns.shapes.get(key, 0) + 1

    side_rows: dict[str, int] = defaultdict(int)
    side_words: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    side_labels: dict[str, set[frozenset[str]]] = defaultdict(set)
    for i, movement in enumerate(movements):
        side = stored_patterns.side_key(movement.account_bidx, movement.is_credit)
        side_rows[side] += 1
        for word in label_words(labels[i]):
            side_words[side][word] += 1
        telling = telling_words(labels[i])
        if telling:
            side_labels[side].add(telling)
    patterns.common_words = {
        side: frozenset(
            w for w, n in counts.items()
            if n >= COMMON_WORD_MIN_COUNT and n > COMMON_WORD_SHARE * side_rows[side]
        )
        for side, counts in side_words.items()
    }

    # Counted over distinct labels, not operations: a salary's employer comes
    # back every month under one label or a few, while "VIR" or "CARTE" sits in
    # most of them. Counted over operations, the employer would read as common
    # and a rule could never reach the next month's reference.
    patterns.label_common_words = {}
    for side, distinct in side_labels.items():
        counts: dict[str, int] = defaultdict(int)
        for words in distinct:
            for word in words:
                counts[word] += 1
        patterns.label_common_words[side] = frozenset(
            w for w, n in counts.items()
            if n >= COMMON_WORD_MIN_COUNT and n > COMMON_WORD_SHARE * len(distinct)
        )

    pairing = _Pairing(
        master_key=master_key,
        decisions=load_decisions(session, user_uuid, master_key),
        patterns=patterns,
        savings=savings,
    )
    transfer_legs = _internal_transfer_legs(movements, pairing)
    questions: dict[str, int] = defaultdict(int)
    questions_amount: dict[str, Decimal] = defaultdict(Decimal)
    for index, leg in transfer_legs.items():
        if leg.status is BankTransferStatus.SUGGESTED and not movements[index].is_credit:
            questions[movements[index].period] += 1
            questions_amount[movements[index].period] += movements[index].amount
    patterns.questions = dict(sorted(questions.items()))
    patterns.questions_amount = dict(sorted(questions_amount.items()))

    filing = _filing(session, user_uuid, master_key, accounts, patterns, movements, transfer_legs)
    resolutions = [
        _filed(movements, transfer_legs, index, labels[index], filing) for index in range(len(movements))
    ]
    asking_groups = _asking_groups(movements, transfer_legs, labels, resolutions)
    heavy = [members for members in asking_groups if _heavy(movements, members)]
    derived = recurring_series.derive(
        [
            _recurring_movement(index, movement, labels[index], transfer_legs.get(index), resolutions[index],
                                   kinds[index], master_key)
            for index, movement in enumerate(movements)
        ],
        load_recurring_decisions(session, user_uuid, master_key),
        {bidx: account.uuid for bidx, account in accounts.by_bidx.items()},
        {index for members in heavy for index in members},
        master_key,
    )
    patterns.recurring = derived.recurring
    patterns.recurring_questions = derived.questions
    # A refund of a counted recurring payment asks whatever it weighs: its answer
    # moves the recurring payment's own figure.
    forced = [
        members for members in asking_groups
        if not _heavy(movements, members) and derived.refunds.intersection(members)
    ]

    flow_questions: dict[str, int] = defaultdict(int)
    flow_open: dict[str, int] = defaultdict(int)
    flow_open_amount: dict[str, Decimal] = defaultdict(Decimal)
    for members in heavy + forced:
        # Movements come sorted by day: the last one is the most recent.
        carrier = movements[members[-1]]
        patterns.flow_carriers[carrier.row.uuid] = FlowCarrier(
            len(members), sum((movements[i].amount for i in members), Decimal("0")),
        )
        flow_questions[carrier.period] += 1
        for index in members:
            flow_open[movements[index].period] += 1
            flow_open_amount[movements[index].period] += movements[index].amount
    patterns.flow_questions = dict(sorted(flow_questions.items()))
    patterns.flow_open = dict(sorted(flow_open.items()))
    patterns.flow_open_amount = dict(sorted(flow_open_amount.items()))

    # Sorted by day: an account's first movement seen is its earliest.
    for movement in movements:
        if movement.day is None:
            continue
        first, _ = patterns.coverage.get(movement.account_bidx, (movement.day, movement.day))
        patterns.coverage[movement.account_bidx] = (first, movement.day)

    stored_patterns.write_patterns(session, user_bidx, source, patterns, master_key)
    return patterns


def _flow_groups(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    labels: dict[int, str | None],
    resolutions: list[Resolution],
    carriers: Collection[str] = frozenset(),
) -> list[list[int]]:
    """The labels only the user can type, each as its operations in date order:
    one group per account, direction and signature, heavy enough to ask — or
    carrying a question the rebuild asked all the same (`carriers`, the stored
    `TransferPatterns.flow_carriers`)."""
    return [
        members for members in _asking_groups(movements, transfer_legs, labels, resolutions)
        if _heavy(movements, members) or movements[members[-1]].row.uuid in carriers
    ]


def _asking_groups(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    labels: dict[int, str | None],
    resolutions: list[Resolution],
) -> list[list[int]]:
    groups: dict[tuple[str, bool, str], list[int]] = defaultdict(list)
    for index, movement in enumerate(movements):
        label = labels[index]
        if _asks_flow(movement, transfer_legs.get(index), label, resolutions[index]):
            groups[(movement.account_bidx, movement.is_credit, label_signature(label))].append(index)
    return list(groups.values())


def _heavy(movements: list[_Movement], members: list[int]) -> bool:
    return sum((movements[i].amount for i in members), Decimal("0")) >= FLOW_QUESTION_MIN_AMOUNT


def _recurring_movement(
    index: int,
    movement: _Movement,
    label: str | None,
    leg: _TransferLeg | None,
    resolution: Resolution,
    kind: OperationType,
    master_key: str,
) -> recurring_series.Movement:
    # The card payment's own date keeps a recurring payment's rhythm through the
    # zero to six days a bank takes to book it: read for the debits only.
    paid_on = None
    stored = movement.row.transaction_date_enc
    if stored and movement.is_final and not movement.is_credit:
        paid_on = date.fromisoformat(decrypt_data(stored, master_key))
    return recurring_series.Movement(
        index=index, uuid=movement.row.uuid, account=movement.account_bidx, period=movement.period,
        day=movement.day, paid_on=paid_on, amount=movement.amount, currency=movement.currency,
        is_credit=movement.is_credit, is_final=movement.is_final, label=label,
        leg=leg.status if leg else None, type=resolution.type, method=kind,
    )


def _aggregate(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    selected: list[int],
    periods: list[str],
) -> _Totals:
    """The monthly totals of the `selected` movements."""
    # A pair counts once, and in full, as soon as one of its legs is selected:
    # filtered on one account, only one leg ever is.
    deducted = {i: leg for i, leg in transfer_legs.items() if leg.status in DEDUCTED}
    pairs = {min(i, deducted[i].other) for i in selected if i in deducted}
    reversals = {i for i in pairs if deducted[i].status in _CANCELLATIONS}
    transfers = pairs - reversals
    questions = {
        min(i, transfer_legs[i].other) for i in selected
        if i in transfer_legs and transfer_legs[i].status is BankTransferStatus.SUGGESTED
    }
    kept = [i for i in selected if i not in deducted]

    # The currency the headline totals speak. Picking the most frequent one keeps
    # a stray foreign-currency movement from silently joining a euro total —
    # amounts arrive unconverted, with no exchange rate attached.
    counts: dict[str, int] = defaultdict(int)
    for index in kept:
        counts[movements[index].currency] += 1
    main_currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    per_month = {p: {"in": Decimal("0"), "out": Decimal("0"), "nin": 0, "nout": 0} for p in periods}
    pending_in = pending_out = Decimal("0")
    pending_count = 0
    others: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"in": Decimal("0"), "out": Decimal("0")}
    )

    for index in kept:
        movement = movements[index]
        if movement.currency != main_currency:
            others[movement.currency]["in" if movement.is_credit else "out"] += movement.amount
            continue
        if not movement.is_final:
            pending_count += 1
            if movement.is_credit:
                pending_in += movement.amount
            else:
                pending_out += movement.amount
            continue
        bucket = per_month[movement.period]
        if movement.is_credit:
            bucket["in"] += movement.amount
            bucket["nin"] += 1
        else:
            bucket["out"] += movement.amount
            bucket["nout"] += 1

    return _Totals(
        currency=main_currency,
        months=[
            BankFlowMonth(
                period=p,
                inflow=per_month[p]["in"],
                outflow=per_month[p]["out"],
                net=per_month[p]["in"] - per_month[p]["out"],
                inflow_count=int(per_month[p]["nin"]),
                outflow_count=int(per_month[p]["nout"]),
            )
            for p in periods
        ],
        pending_count=pending_count,
        pending_inflow=pending_in,
        pending_outflow=pending_out,
        transfers_count=len(transfers),
        transfers_amount=sum((movements[i].amount for i in transfers), Decimal("0")),
        questions_count=len(questions),
        reversals_count=len(reversals),
        reversals_amount=sum((movements[i].amount for i in reversals), Decimal("0")),
        other_currencies=[
            BankFlowCurrencyTotal(currency=c, inflow=v["in"], outflow=v["out"])
            for c, v in sorted(others.items())
        ],
    )


def compute_real_flows(
    session: Session,
    user_uuid: str,
    master_key: str,
    months: int = DEFAULT_MONTHS,
    exclude_internal_transfers: bool = True,
    today: date | None = None,
    account_id: str | None = None,
) -> BankFlowsResponse:
    """Aggregate what actually moved, month by month, over the last `months`.

    `account_id` narrows the totals to one account; transfers are still paired
    against all of them.
    """
    months = max(1, min(months, MAX_MONTHS))
    anchor = today or date.today()
    periods = _months_back(anchor, months)

    accounts = _user_accounts(session, user_uuid, master_key)
    scope = _scope(accounts, account_id, master_key)
    if not scope:
        return _empty(periods)

    pairing = _pairing(session, user_uuid, master_key, accounts) if exclude_internal_transfers else None
    movements, transfer_legs = _paired_movements(
        session, master_key, accounts.readable, periods, pairing,
    )
    window, in_scope = set(periods), set(scope)
    selected = [
        i for i, m in enumerate(movements) if m.period in window and m.account_bidx in in_scope
    ]
    totals = _aggregate(movements, transfer_legs, selected, periods)

    total_in = sum((m.inflow for m in totals.months), Decimal("0"))
    total_out = sum((m.outflow for m in totals.months), Decimal("0"))
    # Averaged over the months that actually carry data, not over the window:
    # dividing a three-month history by twelve reads as a 75 % drop in income.
    covered = sum(1 for m in totals.months if m.inflow_count or m.outflow_count) or 1

    return BankFlowsResponse(
        currency=totals.currency,
        months=totals.months,
        inflow=total_in,
        outflow=total_out,
        net=total_in - total_out,
        monthly_inflow=total_in / covered,
        monthly_outflow=total_out / covered,
        covered_months=covered,
        account_count=len(scope),
        # Named, not just counted: a total across several accounts is only
        # trustworthy once the reader can see which ones it is made of — and
        # which one is missing when the figures look too big.
        account_names=sorted(
            decrypt_data(accounts.by_bidx[bidx].name_enc, master_key) for bidx in scope
        ),
        internal_transfers_excluded=totals.transfers_count,
        internal_transfers_amount=totals.transfers_amount,
        reversals_excluded=totals.reversals_count,
        reversals_amount=totals.reversals_amount,
        pending_count=totals.pending_count,
        pending_inflow=totals.pending_inflow,
        pending_outflow=totals.pending_outflow,
        other_currencies=totals.other_currencies,
    )


def _filed(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    index: int,
    label: str | None,
    filing: _Filing,
) -> Resolution:
    """How one operation counts: the single reading every view of the
    operations shares."""
    movement = movements[index]
    leg = transfer_legs.get(index)
    savings_legs = (
        (movement.account_bidx in filing.savings) + (movements[leg.other].account_bidx in filing.savings)
        if leg else 0
    )
    override = movement.row.type_override_enc
    rule = filing.rules.reach(
        movement.account_bidx, movement.is_credit, label,
        filing.patterns.label_common(movement.account_bidx, movement.is_credit),
    )
    match = filing.contributions.get(index)
    return resolve_type(
        movement.is_credit,
        leg.status if leg else None,
        savings_legs,
        CashflowType(decrypt_data(override, filing.master_key)) if override else None,
        (rule.uuid, rule.type) if rule else None,
        contributed=match is not None and match.exact,
    )


def _asks_flow(movement: _Movement, leg: _TransferLeg | None, label: str | None, resolution: Resolution) -> bool:
    """Whether only the user can say how this operation counts: nothing pairs
    or types it, and it is a credit or a transfer sent.

    The operation type decides whether to ask, never an amount: a label format
    the lexicon misses only asks one question fewer, and the default applies.
    A suggested pair keeps its own question until the user settles it.
    """
    if not movement.is_final or leg is not None or resolution.source is not TypeSource.DEFAULT:
        return False
    if label_signature(label) is None:
        return False
    return movement.is_credit or operation_type(label) is OperationType.TRANSFER


def _item_builder(
    movements: list[_Movement],
    transfer_legs: dict[int, _TransferLeg],
    accounts: _Accounts,
    filing: _Filing,
):
    master_key = filing.master_key
    names = {
        bidx: decrypt_data(account.name_enc, master_key)
        for bidx, account in accounts.by_bidx.items()
    }

    def item(index: int) -> BankTransactionItem:
        movement = movements[index]
        leg = transfer_legs.get(index)
        counterpart = movements[leg.other] if leg else None
        row = movement.row
        label = _label(movement, master_key)
        resolution = _filed(movements, transfer_legs, index, label, filing)
        settles = filing.patterns.flow_carriers.get(row.uuid)
        asks = settles is not None and _asks_flow(movement, leg, label, resolution)
        stored, member = filing.patterns.recurring_of(row.uuid) or (None, None)
        refunds_recurring = (
            stored is not None and stored.counted and member.role == stored_patterns.REFUND
        )
        return BankTransactionItem(
            id=row.uuid,
            account_id=accounts.by_bidx[movement.account_bidx].uuid,
            account_name=names[movement.account_bidx],
            operation_date=movement.day,
            amount=movement.amount,
            currency=movement.currency,
            is_credit=movement.is_credit,
            is_pending=not movement.is_final,
            label=label,
            transfer_account_id=(
                accounts.by_bidx[counterpart.account_bidx].uuid if counterpart else None
            ),
            transfer_account_name=names[counterpart.account_bidx] if counterpart else None,
            transfer_id=counterpart.row.uuid if counterpart else None,
            transfer_status=leg.status if leg else None,
            operation_type=_operation_type(movement, master_key),
            cashflow_type=resolution.type,
            type_source=resolution.source,
            type_rule_id=resolution.rule_id,
            flow_question=BankFlowQuestion(
                choices=CREDIT_CHOICES if movement.is_credit else DEBIT_CHOICES,
                operation_count=settles.count,
                amount=settles.amount,
                suggested=CashflowType.EXPENSE if refunds_recurring else None,
                recurring_name=stored.name if refunds_recurring else None,
            ) if asks else None,
            contribution=_contribution_item(filing.contributions.get(index)),
            recurring=BankRecurringTag(
                id=stored.decision, key=stored.key, name=stored.name,
                cadence=stored.cadence, role=member.role, state=stored.state,
            ) if stored is not None and stored.counted else None,
            recurring_question=(
                _recurring_question(stored)
                if stored is not None and stored.question and stored.carrier == row.uuid else None
            ),
        )

    return item


def _recurring_question(stored: stored_patterns.StoredRecurring) -> BankRecurringQuestion:
    return BankRecurringQuestion(
        cadence=stored.cadence,
        amount=stored.amount,
        variable=stored.variable,
        occurrence_count=recurring_series.occurrence_count(stored),
        since=stored.first,
        annual_estimate=recurring_series.annual_estimate(stored),
        renamed_from=[before for _, before, _ in stored.renamed],
    )


def _contribution_item(match: Match | None) -> BankContributionMatch | None:
    """The declared movement to show: the evidence that typed the operation, or
    a nearby deposit the user may answer the question with."""
    if match is None:
        return None
    return BankContributionMatch(
        account_name=match.contribution.account_name,
        day=match.contribution.day,
        amount=match.contribution.amount,
        is_deposit=match.contribution.is_deposit,
        exact=match.exact,
    )


def list_month_transactions(
    session: Session,
    user_uuid: str,
    master_key: str,
    period: str,
    account_id: str | None = None,
) -> BankTransactionsResponse:
    """Every operation of one "YYYY-MM" month, newest first, with the month's
    totals computed exactly as `compute_real_flows` computes that month.

    Nothing is dropped from the list: an internal transfer, a pending operation
    or a foreign-currency one is flagged rather than hidden, so the list always
    adds up to what the bank app shows.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    scope = _scope(accounts, account_id, master_key)
    if not scope:
        return _empty_month(period)

    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements, transfer_legs = _paired_movements(session, master_key, accounts.readable, [period], pairing)
    in_scope = set(scope)
    selected = [
        i for i, m in enumerate(movements) if m.period == period and m.account_bidx in in_scope
    ]
    totals = _aggregate(movements, transfer_legs, selected, [period])
    [month] = totals.months

    item = _item_builder(
        movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs),
    )
    transactions = [item(i) for i in reversed(selected)]

    return BankTransactionsResponse(
        period=period,
        currency=totals.currency,
        inflow=month.inflow,
        outflow=month.outflow,
        net=month.net,
        internal_transfers_excluded=totals.transfers_count,
        internal_transfers_amount=totals.transfers_amount,
        transfer_questions=totals.questions_count + sum(
            1 for tx in transactions if tx.flow_question or tx.recurring_question
        ),
        reversals_excluded=totals.reversals_count,
        reversals_amount=totals.reversals_amount,
        pending_count=totals.pending_count,
        pending_inflow=totals.pending_inflow,
        pending_outflow=totals.pending_outflow,
        other_currencies=totals.other_currencies,
        transactions=transactions,
    )


def review_queue(
    session: Session, user_uuid: str, master_key: str, year: int | None = None
) -> BankReviewQueue:
    """Every question left to the user across the history, the one an answer
    moves most money with first.

    Read over the whole history in one pass, as the questions were counted:
    building each carrier from its own month would load a month per question.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    item = _item_builder(
        movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs),
    )

    questions: list[BankReviewItem] = []
    recurring = {s.carrier: s for s in pairing.patterns.recurring if s.question}
    for index, movement in enumerate(movements):
        leg = transfer_legs.get(index)
        carrier = pairing.patterns.flow_carriers.get(movement.row.uuid)
        stored = recurring.get(movement.row.uuid)
        if stored is not None:
            questions.append(BankReviewItem(
                kind=BankReviewKind.RECURRING, transaction=item(index),
                amount=recurring_series.annual_estimate(stored),
                operation_count=recurring_series.occurrence_count(stored),
            ))
        elif carrier is not None:
            built = item(index)
            if built.flow_question:
                questions.append(BankReviewItem(
                    kind=BankReviewKind.FLOW, transaction=built, amount=carrier.amount, operation_count=carrier.count,
                ))
        elif leg is not None and leg.status is BankTransferStatus.SUGGESTED and not movement.is_credit:
            questions.append(BankReviewItem(
                kind=BankReviewKind.TRANSFER, transaction=item(index), amount=movement.amount, operation_count=2,
            ))

    # A recurring payment's answer moves no total: counted in, never added up.
    def moves(question: BankReviewItem) -> Decimal:
        return Decimal("0") if question.kind is BankReviewKind.RECURRING else question.amount

    years: dict[int, BankReviewYear] = {}
    for question in questions:
        day = question.transaction.operation_date
        if day is None:
            continue
        entry = years.setdefault(day.year, BankReviewYear(year=day.year, amount=Decimal("0"), count=0))
        entry.amount += moves(question)
        entry.count += 1
    if year is not None:
        questions = [q for q in questions if q.transaction.operation_date and q.transaction.operation_date.year == year]
    questions.sort(key=lambda q: (-q.amount, -(q.transaction.operation_date or date.min).toordinal()))
    return BankReviewQueue(
        total_amount=sum((moves(q) for q in questions), Decimal("0")),
        total_count=len(questions),
        recurring_count=sum(1 for q in questions if q.kind is BankReviewKind.RECURRING),
        years=sorted(years.values(), key=lambda entry: -entry.year),
        questions=questions,
    )


def list_transfer_counterparts(
    session: Session,
    user_uuid: str,
    master_key: str,
    transaction_id: str,
) -> list[BankTransactionItem]:
    """The movements a user may bind to this one by hand, nearest first.

    Opposite direction, same amount and currency, on any readable account — the
    same account for a cancellation — within MAX_DECISION_DAYS. Each comes as
    the list shows it, current pairing included, so a candidate already claimed
    by another pair says so.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    row = session.get(BankTransaction, transaction_id)
    if row is None or row.account_id_bidx not in accounts.readable:
        raise TransactionNotFoundError(transaction_id)
    day = row_date(row, master_key)
    if day is None:
        return []

    period = f"{day.year:04d}-{day.month:02d}"
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements, transfer_legs = _paired_movements(session, master_key, accounts.readable, [period], pairing)
    [origin] = [m for m in movements if m.row.uuid == transaction_id]
    matches = sorted(
        (
            (abs((m.day - day).days), i)
            for i, m in enumerate(movements)
            if m.row.uuid != transaction_id
            and m.day is not None
            and m.is_credit != origin.is_credit
            and m.amount == origin.amount
            and m.currency == origin.currency
            and abs((m.day - day).days) <= MAX_DECISION_DAYS
        ),
    )
    item = _item_builder(
        movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs),
    )
    return [item(i) for _, i in matches]


def list_flow_group(
    session: Session,
    user_uuid: str,
    master_key: str,
    transaction_id: str,
) -> list[BankTransactionItem]:
    """The operations one answer to a flow question would type, newest first.

    Exactly the operations its count is made of (`_flow_groups`), so what the
    list shows and what the question claims can never disagree. Read over the
    whole history, as the questions are counted: the group spans every month.
    Empty when the operation carries no question — an answered label types
    nothing more.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    _readable_row(session, accounts, transaction_id)
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    labels = {index: _label(movement, master_key) for index, movement in enumerate(movements)}
    resolutions = [
        _filed(movements, transfer_legs, index, labels[index], filing) for index in range(len(movements))
    ]

    groups = _flow_groups(movements, transfer_legs, labels, resolutions, pairing.patterns.flow_carriers)
    members = next(
        (group for group in groups if any(movements[i].row.uuid == transaction_id for i in group)),
        [],
    )
    item = _item_builder(movements, transfer_legs, accounts, filing)
    return [item(i) for i in reversed(members)]


def set_transaction_type(
    session: Session,
    user_uuid: str,
    master_key: str,
    transaction_id: str,
    kind: CashflowType,
    scope: TypeScope,
) -> BankTransactionTypeResult:
    """Type one operation, or write the rule of its label for every operation
    reading like it on its account and direction.

    A rule drops the operation's own override, so the rule is what types it
    from then on — correcting the rule later corrects it too.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    row = _readable_row(session, accounts, transaction_id)
    current = _transaction_item(session, user_uuid, master_key, accounts, row)
    if current.transfer_status not in (None, BankTransferStatus.SUGGESTED):
        raise PairedOperationError(transaction_id)

    if scope is TypeScope.OPERATION:
        row.type_override_enc = encrypt_data(kind.value, master_key)
        session.add(row)
        session.commit()
        return BankTransactionTypeResult(
            transaction=_transaction_item(session, user_uuid, master_key, accounts, row), covered_count=1,
        )

    signature = label_signature(current.label)
    if signature is None:
        raise LabelRequiredError(transaction_id)
    row.type_override_enc = None
    session.add(row)
    rule = save_rule(
        session, user_uuid, master_key, accounts.by_bidx[row.account_id_bidx].uuid, current.is_credit, current.label, kind,
    )
    covered = sum(
        1 for _, _, resolution in _typed_history(session, user_uuid, master_key, accounts)
        if resolution.rule_id == rule.uuid
    )
    return BankTransactionTypeResult(
        transaction=_transaction_item(session, user_uuid, master_key, accounts, row), covered_count=covered,
    )


def clear_transaction_type(
    session: Session, user_uuid: str, master_key: str, transaction_id: str
) -> BankTransactionItem:
    """Drop what the user forced on this one operation."""
    accounts = _user_accounts(session, user_uuid, master_key)
    row = _readable_row(session, accounts, transaction_id)
    row.type_override_enc = None
    session.add(row)
    session.commit()
    return _transaction_item(session, user_uuid, master_key, accounts, row)


def list_type_rules(session: Session, user_uuid: str, master_key: str) -> list[BankTypeRuleItem]:
    """Every rule of the user, with the operations it types across the history."""
    accounts = _user_accounts(session, user_uuid, master_key)
    rules = load_rules(session, user_uuid, master_key)
    counts: dict[str, int] = defaultdict(int)
    labels: dict[str, str | None] = {}
    for _, label, resolution in _typed_history(session, user_uuid, master_key, accounts):
        if resolution.rule_id is not None:
            counts[resolution.rule_id] += 1
            labels[resolution.rule_id] = label
    items = [
        BankTypeRuleItem(
            id=rule.uuid,
            account_id=accounts.by_bidx[rule.account_bidx].uuid,
            account_name=decrypt_data(accounts.by_bidx[rule.account_bidx].name_enc, master_key),
            is_credit=rule.is_credit,
            signature=rule.signature,
            label=labels.get(rule.uuid),
            type=rule.type,
            operation_count=counts[rule.uuid],
            created_at=rule.created_at,
        )
        # A rule of a deleted account types nothing and names no account.
        for rule in rules.exact.values() if rule.account_bidx in accounts.by_bidx
    ]
    return sorted(items, key=lambda item: (-item.operation_count, item.signature))


def _readable_row(session: Session, accounts: _Accounts, transaction_id: str) -> BankTransaction:
    row = session.get(BankTransaction, transaction_id)
    if row is None or row.account_id_bidx not in accounts.readable:
        raise TransactionNotFoundError(transaction_id)
    return row


def _typed_history(session: Session, user_uuid: str, master_key: str, accounts: _Accounts):
    """Every stored operation with its label and type, paired across the
    whole history."""
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    for index, movement in enumerate(movements):
        label = _label(movement, master_key)
        yield movement, label, _filed(movements, transfer_legs, index, label, filing)


def _transaction_item(
    session: Session, user_uuid: str, master_key: str, accounts: _Accounts, row: BankTransaction
) -> BankTransactionItem:
    """One operation exactly as its month's list shows it."""
    day = row_date(row, master_key)
    pairing = _pairing(session, user_uuid, master_key, accounts)
    if day is None:
        movements = _load_movements(session, master_key, [row.account_id_bidx], None)
        transfer_legs: dict[int, _TransferLeg] = {}
    else:
        movements, transfer_legs = _paired_movements(
            session, master_key, accounts.readable, [f"{day:%Y-%m}"], pairing,
        )
    [index] = [i for i, m in enumerate(movements) if m.row.uuid == row.uuid]
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    return _item_builder(movements, transfer_legs, accounts, filing)(index)


def _empty(periods: list[str]) -> BankFlowsResponse:
    return BankFlowsResponse(
        currency="EUR",
        months=[
            BankFlowMonth(period=p, inflow=Decimal("0"), outflow=Decimal("0"), net=Decimal("0"))
            for p in periods
        ],
        inflow=Decimal("0"),
        outflow=Decimal("0"),
        net=Decimal("0"),
        monthly_inflow=Decimal("0"),
        monthly_outflow=Decimal("0"),
        covered_months=0,
        account_count=0,
        account_names=[],
        internal_transfers_excluded=0,
        internal_transfers_amount=Decimal("0"),
        pending_count=0,
        pending_inflow=Decimal("0"),
        pending_outflow=Decimal("0"),
        other_currencies=[],
    )


def _empty_month(period: str) -> BankTransactionsResponse:
    return BankTransactionsResponse(
        period=period,
        currency="EUR",
        inflow=Decimal("0"),
        outflow=Decimal("0"),
        net=Decimal("0"),
        internal_transfers_excluded=0,
        internal_transfers_amount=Decimal("0"),
        pending_count=0,
        pending_inflow=Decimal("0"),
        pending_outflow=Decimal("0"),
        other_currencies=[],
        transactions=[],
    )
