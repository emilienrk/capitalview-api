"""
Recurring payment decisions as stored (services/banking/recurring_decisions.py):
what they keep, what deleting an account does to them, and how writing one
reaches the stored transfer patterns.
"""
from datetime import date
from decimal import Decimal

from sqlmodel import Session

from models.banking import BankRecurringSeries
from services.bank import delete_bank_account
from services.banking import transfer_patterns as stored_patterns
from services.banking.flows import _regulated_savings, _user_accounts, transfer_patterns
from services.banking.recurring_decisions import (
    CONFIRMED,
    REFUSED,
    Decision,
    Identity,
    delete_decision,
    load_decisions,
    save_decision,
)
from services.encryption import hash_index
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, _ops


def _decision(uuid: str, *accounts: str, status: str = CONFIRMED) -> Decision:
    return Decision(
        uuid=uuid, status=status, anchors=frozenset({f"anchor-{uuid}"}),
        identity=Identity(("edf", "clients"), accounts, "monthly", Decimal("60.00"), "DIRECT_DEBIT"),
    )


def test_a_decision_reads_back_as_written(session: Session, master_key: str):
    written = _decision("sub-1", CURRENT)
    written.includes = frozenset({"in"})
    written.excludes = frozenset({"out"})
    written.name, written.cadence, written.ended_on = "Électricité", "quarterly", date(2026, 6, 30)
    save_decision(session, USER, master_key, written)

    [read] = load_decisions(session, USER, master_key)
    assert (read.uuid, read.status, read.anchors, read.identity) == ("sub-1", CONFIRMED, written.anchors, written.identity)
    assert (read.includes, read.excludes, read.name, read.cadence, read.ended_on) == (
        frozenset({"in"}), frozenset({"out"}), "Électricité", "quarterly", date(2026, 6, 30),
    )


def test_nothing_about_an_operation_is_stored_in_clear(session: Session, master_key: str):
    decision = _decision("sub-1", CURRENT)
    decision.anchors = frozenset({hash_index("operation-uuid", master_key)})
    save_decision(session, USER, master_key, decision)
    row = session.get(BankRecurringSeries, "sub-1")
    clear = " ".join(str(value) for value in row.model_dump().values())
    for secret in ("operation-uuid", hash_index("operation-uuid", master_key), CURRENT, "edf", "60.00", CONFIRMED):
        assert secret not in clear


def test_decisions_replay_in_the_order_they_last_changed(session: Session, master_key: str):
    save_decision(session, USER, master_key, _decision("first", CURRENT))
    save_decision(session, USER, master_key, _decision("second", CURRENT, status=REFUSED))
    save_decision(session, USER, master_key, _decision("first", CURRENT))
    assert [d.uuid for d in load_decisions(session, USER, master_key)] == ["second", "first"]


def test_deleting_an_account_drops_its_decisions_and_forgets_it_in_the_others(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-02", "60.00", "DBIT", "PRLV SEPA EDF"), (LIVRET, "2026-03-02", "5.00", "CRDT", "INTERETS"))
    save_decision(session, USER, master_key, _decision("only-current", CURRENT))
    save_decision(session, USER, master_key, _decision("both", CURRENT, LIVRET))
    save_decision(session, USER, master_key, _decision("elsewhere", LIVRET))

    assert delete_bank_account(session, CURRENT, master_key)

    assert {d.uuid: d.identity.accounts for d in load_decisions(session, USER, master_key)} == {
        "both": (LIVRET,), "elsewhere": (LIVRET,),
    }


def test_writing_a_decision_outdates_the_stored_patterns(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-02", "60.00", "DBIT", "PRLV SEPA EDF"))
    transfer_patterns(session, USER, master_key)
    accounts = _user_accounts(session, USER, master_key)
    user_bidx = hash_index(USER, master_key)

    def digest() -> str:
        return stored_patterns.source_digest(
            session, user_bidx, accounts.readable, _regulated_savings(accounts, master_key), master_key,
        )

    built = digest()
    assert stored_patterns.read_patterns(session, user_bidx, built, master_key) is not None
    save_decision(session, USER, master_key, _decision("sub-1", CURRENT))
    created = digest()
    assert created != built
    assert stored_patterns.read_patterns(session, user_bidx, created, master_key) is None

    decision = _decision("sub-1", CURRENT)
    decision.name = "EDF"
    save_decision(session, USER, master_key, decision)
    renamed = digest()
    assert renamed != created

    # No decision left: the same sources as the first build.
    delete_decision(session, USER, master_key, "sub-1")
    assert digest() == built
