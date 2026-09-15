"""
Internal transfer pairing against cases lifted from real linked accounts.

Only what the pairing reads was kept — date, amount, direction and account; the
labels are gone. Each case names the operations with a short token, stored as
the label so a test can tell rows apart: the pairing itself never reads it.

Accounts: `current` and `savings` sit at one bank, `neobank` at another, `ldd`
is a second savings account at the first bank.
"""
from collections import Counter
from dataclasses import dataclass

import pytest
from sqlmodel import Session

from services.banking.flows import _paired_movements, _pairing, _user_accounts
from services.encryption import decrypt_data
from tests.services.test_banking_flows import USER, _link, _raw, _store

CURRENT, NEOBANK, SAVINGS, LDD = "current", "neobank", "savings", "ldd"


@dataclass(frozen=True)
class Case:
    id: str
    # (token, account, day, amount, "DBIT" | "CRDT")
    operations: tuple[tuple[str, str, str, str, str], ...]
    # (debit token, credit token)
    transfers: frozenset[tuple[str, str]]


CASES = [
    # --- Pairs the date-order greedy pass got wrong ---
    Case(
        "an-earlier-debit-no-longer-steals-the-credit",
        (
            ("pocket", NEOBANK, "2023-04-12", "1.00", "DBIT"),
            ("out", NEOBANK, "2023-04-13", "1.00", "DBIT"),
            ("in", CURRENT, "2023-04-13", "1.00", "CRDT"),
        ),
        frozenset({("out", "in")}),
    ),
    Case(
        "the-same-day-debit-wins-between-two-neighbours",
        (
            ("before", NEOBANK, "2023-01-04", "1.00", "DBIT"),
            ("out", NEOBANK, "2023-01-06", "1.00", "DBIT"),
            ("in", CURRENT, "2023-01-06", "1.00", "CRDT"),
            ("after", NEOBANK, "2023-01-07", "1.00", "DBIT"),
        ),
        frozenset({("out", "in")}),
    ),
    Case(
        "a-card-top-up-booked-the-next-day-beats-an-older-debit",
        (
            ("older", CURRENT, "2024-09-16", "10.00", "DBIT"),
            ("top-up", NEOBANK, "2024-09-18", "10.00", "CRDT"),
            ("card", CURRENT, "2024-09-19", "10.00", "DBIT"),
        ),
        frozenset({("card", "top-up")}),
    ),
    Case(
        "crossed-legs-around-a-weekend-pair-with-their-own",
        (
            ("in-sat", NEOBANK, "2023-08-19", "20.00", "CRDT"),
            ("out-mon", CURRENT, "2023-08-21", "20.00", "DBIT"),
            ("out-tue", CURRENT, "2023-08-22", "20.00", "DBIT"),
            ("in-tue", NEOBANK, "2023-08-22", "20.00", "CRDT"),
        ),
        frozenset({("out-mon", "in-sat"), ("out-tue", "in-tue")}),
    ),
    # --- Pairs a calendar-day tolerance missed ---
    Case(
        "thursday-to-monday",
        (
            ("out", NEOBANK, "2022-10-13", "50.00", "DBIT"),
            ("in", CURRENT, "2022-10-17", "50.00", "CRDT"),
        ),
        frozenset({("out", "in")}),
    ),
    Case(
        "over-good-friday-and-easter-monday",
        (
            ("top-up", NEOBANK, "2025-04-17", "12.50", "CRDT"),
            ("card", CURRENT, "2025-04-22", "12.50", "DBIT"),
        ),
        frozenset({("card", "top-up")}),
    ),
    Case(
        "over-labour-day-and-a-month-edge",
        (
            ("top-up", NEOBANK, "2026-04-30", "150.00", "CRDT"),
            ("card", CURRENT, "2026-05-04", "150.00", "DBIT"),
        ),
        frozenset({("card", "top-up")}),
    ),
    Case(
        "two-top-ups-of-one-amount-over-easter",
        (
            ("top-up-fri", NEOBANK, "2026-04-03", "150.00", "CRDT"),
            ("top-up-sat", NEOBANK, "2026-04-04", "150.00", "CRDT"),
            ("card-1", CURRENT, "2026-04-07", "150.00", "DBIT"),
            ("card-2", CURRENT, "2026-04-07", "150.00", "DBIT"),
        ),
        frozenset({("card-1", "top-up-fri"), ("card-2", "top-up-sat")}),
    ),
    Case(
        "two-top-ups-a-day-apart-both-pair-when-closest-first-would-strand-one",
        (
            ("card-wed", CURRENT, "2026-07-15", "30.00", "DBIT"),
            ("top-up-mon", NEOBANK, "2026-07-13", "30.00", "CRDT"),
            ("top-up-thu", NEOBANK, "2026-07-16", "30.00", "CRDT"),
            ("card-fri", CURRENT, "2026-07-17", "30.00", "DBIT"),
        ),
        frozenset({("card-wed", "top-up-mon"), ("card-fri", "top-up-thu")}),
    ),
    # --- Pairs that were right and must stay so ---
    Case(
        "to-and-from-savings-on-the-day",
        (
            ("to-savings", CURRENT, "2025-08-18", "1000.00", "DBIT"),
            ("into-savings", SAVINGS, "2025-08-18", "1000.00", "CRDT"),
            ("from-savings", SAVINGS, "2025-08-18", "500.00", "DBIT"),
            ("back-in-current", CURRENT, "2025-08-18", "500.00", "CRDT"),
        ),
        frozenset({("to-savings", "into-savings"), ("from-savings", "back-in-current")}),
    ),
    Case(
        "two-identical-transfers-on-one-day-are-two-pairs",
        (
            ("out-1", CURRENT, "2024-10-29", "500.00", "DBIT"),
            ("out-2", CURRENT, "2024-10-29", "500.00", "DBIT"),
            ("in-1", LDD, "2024-10-29", "500.00", "CRDT"),
            ("in-2", LDD, "2024-10-29", "500.00", "CRDT"),
        ),
        frozenset({("out-1", "in-1"), ("out-2", "in-2")}),
    ),
    Case(
        "saturday-to-monday",
        (
            ("out", NEOBANK, "2025-01-04", "90.90", "DBIT"),
            ("in", CURRENT, "2025-01-06", "90.90", "CRDT"),
        ),
        frozenset({("out", "in")}),
    ),
    Case(
        "a-top-up-credited-before-the-card-debit-books",
        (
            ("top-up", NEOBANK, "2024-04-12", "30.00", "CRDT"),
            ("card", CURRENT, "2024-04-15", "30.00", "DBIT"),
        ),
        frozenset({("card", "top-up")}),
    ),
    Case(
        "a-week-apart-is-two-real-movements",
        (
            ("out", NEOBANK, "2026-03-16", "50.00", "DBIT"),
            ("in", CURRENT, "2026-03-23", "50.00", "CRDT"),
        ),
        frozenset(),
    ),
    Case(
        "three-banking-days-apart-is-two-real-movements",
        (
            ("out", CURRENT, "2025-07-21", "36.24", "DBIT"),
            ("in", NEOBANK, "2025-07-24", "36.24", "CRDT"),
        ),
        frozenset(),
    ),
    Case(
        "a-rejected-instant-transfer-on-one-account-is-no-transfer",
        (
            ("sent", CURRENT, "2024-06-03", "2000.00", "DBIT"),
            ("rejected", CURRENT, "2024-06-03", "2000.00", "CRDT"),
        ),
        frozenset(),
    ),
]


def _stored_pairs(session: Session, master_key: str, case: Case) -> Counter:
    for account in {op[1] for op in case.operations}:
        _link(session, master_key, account)
    for token, account, day, amount, direction in case.operations:
        _store(session, master_key, account, _raw(amount, direction, day, ref=token, label=token))

    periods = sorted({day[:7] for _, _, day, _, _ in case.operations})
    accounts = _user_accounts(session, USER, master_key)
    movements, legs = _paired_movements(
        session, master_key, accounts.readable, periods, _pairing(session, USER, master_key, accounts)
    )
    return Counter(
        (
            decrypt_data(movements[index].row.remittance_enc, master_key),
            decrypt_data(movements[leg.other].row.remittance_enc, master_key),
        )
        for index, leg in legs.items()
        if not movements[index].is_credit
    )


def _shapes(case: Case, pairs) -> Counter:
    """Pairs as what the pairing can see. Two operations alike in account, day,
    amount and direction are interchangeable: which of them takes which leg is
    arbitrary, and changes no total."""
    shape = {token: rest for token, *rest in case.operations}
    return Counter((tuple(shape[debit]), tuple(shape[credit])) for debit, credit in pairs)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_the_pairing_matches_what_really_moved(session: Session, master_key: str, case: Case):
    stored = _stored_pairs(session, master_key, case)
    assert _shapes(case, stored.elements()) == _shapes(case, case.transfers)
