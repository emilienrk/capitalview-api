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
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import NamedTuple

from sqlmodel import Session, select

from dtos.banking import (
    BankCategoryAssignResult,
    BankFlowCurrencyTotal,
    BankRuleWords,
    BankFlowMonth,
    BankFlowsResponse,
    BankTransactionItem,
    BankTransactionsResponse,
    BankTransferDecisionKind,
    BankTransferStatus,
    BankUncategorizedGroup,
    BankUncategorizedResponse,
    OperationNature,
    OperationType,
    RuleSource,
)
from models.bank import BankAccount
from models.banking import BankTransaction
from models.enums import BankAccountType
from services.banking import transfer_patterns as stored_patterns
from services.banking.categories import (
    Category,
    CategoryNotFoundError,
    load_categories,
    load_rules,
    save_rule,
)
from services.banking.categorize import (
    NO_CATEGORY,
    Resolution,
    Rule,
    WordFrequency,
    nature_of,
    propose_tokens,
    resolve,
    rule_tokens,
)
from services.banking.linking import readable_account_bidxs
from services.banking.operation_types import operation_type
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
from services.banking.transfer_patterns import TransferPatterns
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

DEFAULT_MONTHS = 12
MAX_MONTHS = 120


class UnknownAccountError(LookupError):
    """The account filter names no bank account of this user."""


class CategoryRequiredError(ValueError):
    """A rule must file operations under a category."""


class RuleOutsideLabelError(ValueError):
    """A rule for an operation must only require words of its label."""


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
    """What resolving categories needs, loaded once per request."""
    master_key: str
    rules: list[Rule]
    categories: dict[str, Category]
    savings: frozenset[str]


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


def _filing(session: Session, user_uuid: str, master_key: str, accounts: _Accounts) -> _Filing:
    return _Filing(
        master_key=master_key,
        rules=load_rules(session, user_uuid, master_key),
        categories=load_categories(session, user_uuid, master_key),
        savings=_savings_accounts(accounts, master_key),
    )


def _label(movement: _Movement, master_key: str) -> str | None:
    return decrypt_data(movement.row.remittance_enc, master_key) if movement.row.remittance_enc else None


def _resolution(movement: _Movement, label: str | None, filing: _Filing) -> Resolution:
    override = movement.row.category_ref_enc
    return resolve(
        label_words(label),
        decrypt_data(override, filing.master_key) if override else None,
        filing.rules,
        filing.categories,
    )


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
    for i, movement in enumerate(movements):
        row = movement.row
        changed = False
        signature = label_signature(labels[i])
        if row.label_signature_bidx is None and signature is not None:
            row.label_signature_bidx = hash_index(signature, master_key)
            changed = True
        # Every row, not only those stored before types existed: this is how a
        # change to the lexicon reaches the history.
        kind = operation_type(labels[i]).value
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
    for i, movement in enumerate(movements):
        side = stored_patterns.side_key(movement.account_bidx, movement.is_credit)
        side_rows[side] += 1
        for word in label_words(labels[i]):
            side_words[side][word] += 1
    signatures = {signature for signature in map(label_signature, labels.values()) if signature}
    word_counts: dict[str, int] = defaultdict(int)
    for signature in signatures:
        for word in signature.split():
            word_counts[word] += 1
    patterns.word_frequency = WordFrequency(
        counts=dict(word_counts),
        common_above=max(COMMON_WORD_MIN_COUNT - 1, COMMON_WORD_SHARE * len(signatures)),
    )

    patterns.common_words = {
        side: frozenset(
            w for w, n in counts.items()
            if n >= COMMON_WORD_MIN_COUNT and n > COMMON_WORD_SHARE * side_rows[side]
        )
        for side, counts in side_words.items()
    }

    pairing = _Pairing(
        master_key=master_key,
        decisions=load_decisions(session, user_uuid, master_key),
        patterns=patterns,
        savings=savings,
    )
    questions: dict[str, int] = defaultdict(int)
    for index, leg in _internal_transfer_legs(movements, pairing).items():
        if leg.status is BankTransferStatus.SUGGESTED and not movements[index].is_credit:
            questions[movements[index].period] += 1
    patterns.questions = dict(sorted(questions.items()))

    stored_patterns.write_patterns(session, user_bidx, source, patterns, master_key)
    return patterns


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
) -> tuple[Resolution, OperationNature]:
    """One operation's category and how it counts: the single reading every
    view of the operations shares."""
    movement = movements[index]
    leg = transfer_legs.get(index)
    resolution = _resolution(movement, label, filing)
    savings_legs = (
        (movement.account_bidx in filing.savings) + (movements[leg.other].account_bidx in filing.savings)
        if leg else 0
    )
    nature = nature_of(movement.is_credit, leg.status if leg else None, savings_legs, resolution.category)
    return resolution, nature


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
        resolution, nature = _filed(movements, transfer_legs, index, label, filing)
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
            # Every reader pairs first, and pairing backfills a missing type.
            operation_type=(
                OperationType(decrypt_data(row.operation_type_enc, master_key))
                if row.operation_type_enc else OperationType.UNKNOWN
            ),
            nature=nature,
            category_id=resolution.category.uuid if resolution.category else None,
            category_name=resolution.category.name if resolution.category else None,
            category_source=resolution.source,
            rule_id=resolution.rule_uuid,
        )

    return item


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

    movements, transfer_legs = _paired_movements(
        session, master_key, accounts.readable, [period], _pairing(session, user_uuid, master_key, accounts),
    )
    in_scope = set(scope)
    selected = [
        i for i, m in enumerate(movements) if m.period == period and m.account_bidx in in_scope
    ]
    totals = _aggregate(movements, transfer_legs, selected, [period])
    [month] = totals.months

    item = _item_builder(movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts))

    return BankTransactionsResponse(
        period=period,
        currency=totals.currency,
        inflow=month.inflow,
        outflow=month.outflow,
        net=month.net,
        internal_transfers_excluded=totals.transfers_count,
        internal_transfers_amount=totals.transfers_amount,
        transfer_questions=totals.questions_count,
        reversals_excluded=totals.reversals_count,
        reversals_amount=totals.reversals_amount,
        pending_count=totals.pending_count,
        pending_inflow=totals.pending_inflow,
        pending_outflow=totals.pending_outflow,
        other_currencies=totals.other_currencies,
        transactions=[item(i) for i in reversed(selected)],
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
    movements, transfer_legs = _paired_movements(
        session, master_key, accounts.readable, [period], _pairing(session, user_uuid, master_key, accounts),
    )
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
    item = _item_builder(movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts))
    return [item(i) for _, i in matches]


def assign_category(
    session: Session,
    user_uuid: str,
    master_key: str,
    transaction_id: str,
    category_id: str | None,
    apply_to_similar: bool,
    tokens: list[str] | None = None,
) -> BankCategoryAssignResult:
    """File one operation, or write the user's rule for every operation like it.

    A rule drops the operation's own override, so the rule is what files it
    from then on — correcting the rule later corrects it too.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    row = session.get(BankTransaction, transaction_id)
    if row is None or row.account_id_bidx not in accounts.readable:
        raise TransactionNotFoundError(transaction_id)
    if category_id is not None and category_id not in load_categories(session, user_uuid, master_key):
        raise CategoryNotFoundError(category_id)

    if not apply_to_similar:
        row.category_ref_enc = encrypt_data(category_id or NO_CATEGORY, master_key)
        session.add(row)
        session.commit()
        return BankCategoryAssignResult(
            transaction=_transaction_item(session, user_uuid, master_key, accounts, row),
            filed_count=1 if category_id else 0,
        )

    if category_id is None:
        raise CategoryRequiredError()
    label = decrypt_data(row.remittance_enc, master_key) if row.remittance_enc else None
    frequency = transfer_patterns(session, user_uuid, master_key, accounts).word_frequency
    words = tokens if tokens is not None else propose_tokens(label, frequency)
    if not rule_tokens(words) <= label_words(label):
        raise RuleOutsideLabelError()
    rule = save_rule(session, user_uuid, master_key, words, category_id, RuleSource.USER, frequency)
    row.category_ref_enc = None
    session.add(row)
    session.commit()

    filing = _filing(session, user_uuid, master_key, accounts)
    filed = sum(
        1 for movement in _load_movements(session, master_key, accounts.readable, None)
        if _resolution(movement, _label(movement, master_key), filing).rule_uuid == rule.uuid
    )
    return BankCategoryAssignResult(
        transaction=_transaction_item(session, user_uuid, master_key, accounts, row),
        filed_count=filed,
    )


def rule_words(session: Session, user_uuid: str, master_key: str, transaction_id: str) -> BankRuleWords:
    accounts = _user_accounts(session, user_uuid, master_key)
    row = session.get(BankTransaction, transaction_id)
    if row is None or row.account_id_bidx not in accounts.readable:
        raise TransactionNotFoundError(transaction_id)
    label = decrypt_data(row.remittance_enc, master_key) if row.remittance_enc else None
    frequency = transfer_patterns(session, user_uuid, master_key, accounts).word_frequency
    return BankRuleWords(
        words=sorted(label_words(label), key=lambda word: (frequency.of(word), word)),
        proposed=propose_tokens(label, frequency),
    )


def _transaction_item(
    session: Session, user_uuid: str, master_key: str, accounts: _Accounts, row: BankTransaction
) -> BankTransactionItem:
    """One operation exactly as its month's list shows it."""
    day = row_date(row, master_key)
    if day is None:
        movements = _load_movements(session, master_key, [row.account_id_bidx], None)
        transfer_legs: dict[int, _TransferLeg] = {}
    else:
        movements, transfer_legs = _paired_movements(
            session, master_key, accounts.readable, [f"{day:%Y-%m}"],
            _pairing(session, user_uuid, master_key, accounts),
        )
    [index] = [i for i, m in enumerate(movements) if m.row.uuid == row.uuid]
    return _item_builder(movements, transfer_legs, accounts, _filing(session, user_uuid, master_key, accounts))(index)


def uncategorized_groups(
    session: Session, user_uuid: str, master_key: str, limit: int | None = None
) -> BankUncategorizedResponse:
    """The operations nothing files yet, grouped by label signature and
    direction, heaviest first.

    An operation paired as a transfer or a cancellation is left out: its
    nature does not depend on a category. So is one the user filed as "none".
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    if not accounts.readable:
        return BankUncategorizedResponse(total_groups=0, total_operations=0, groups=[])
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts)

    grouped: dict[tuple[str, bool], list[tuple[_Movement, str]]] = defaultdict(list)
    for index, movement in enumerate(movements):
        leg = transfer_legs.get(index)
        if leg is not None and leg.status in DEDUCTED:
            continue
        label = _label(movement, master_key)
        signature = label_signature(label)
        if signature is None or _resolution(movement, label, filing).source is not None:
            continue
        grouped[(signature, movement.is_credit)].append((movement, label))

    groups = []
    for (signature, is_credit), members in grouped.items():
        # Movements come sorted by day: the last one is the most recent.
        latest, latest_label = members[-1]
        currencies = Counter(movement.currency for movement, _ in members)
        currency = max(currencies, key=lambda c: (currencies[c], c == latest.currency))
        amounts = [movement.amount for movement, _ in members if movement.currency == currency]
        groups.append(BankUncategorizedGroup(
            signature=signature,
            transaction_id=latest.row.uuid,
            label=latest_label,
            is_credit=is_credit,
            count=len(members),
            currency=currency,
            total=sum(amounts, Decimal("0")),
            median=median(amounts),
            last_date=latest.day,
            tokens=propose_tokens(latest_label, pairing.patterns.word_frequency),
        ))
    groups.sort(key=lambda g: (-g.total, -g.count, g.signature, g.is_credit))
    return BankUncategorizedResponse(
        total_groups=len(groups),
        total_operations=sum(g.count for g in groups),
        groups=groups[:limit] if limit is not None else groups,
    )


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
