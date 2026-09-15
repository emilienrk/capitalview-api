"""
The user's transfer decisions (services/banking/transfer_decisions.py) and what
the pairing makes of them.

Labels matter here, unlike in test_banking_transfer_pairing.py: they are what a
decision teaches. They are shaped like the real ones the cases came from, with
the names replaced.
"""
from datetime import date

import pytest
from sqlmodel import Session, select

from dtos.banking import BankTransferDecisionKind as Kind
from dtos.banking import BankTransferStatus as Status
from models.banking import BankTransaction, BankTransferDecision
from services.bank import delete_bank_account
from services.banking.flows import (
    compute_real_flows,
    list_month_transactions,
    list_transfer_counterparts,
)
from services.banking.transfer_decisions import (
    DecisionError,
    TransactionNotFoundError,
    record_decision,
)
from services.encryption import decrypt_data
from tests.services.test_banking_flows import USER, _link, _raw, _store

CURRENT, NEOBANK, SAVINGS = "current", "neobank", "savings"


def _ops(session: Session, master_key: str, *operations: tuple[str, str, str, str, str]) -> None:
    """(account, day, amount, direction, label)"""
    for account in {op[0] for op in operations}:
        _link(session, master_key, account)
    for n, (account, day, amount, direction, label) in enumerate(operations):
        _store(session, master_key, account, _raw(amount, direction, day, ref=f"{label}-{day}-{n}", label=label))


def _id(session: Session, master_key: str, label: str) -> str:
    [row] = [
        row for row in session.exec(select(BankTransaction)).all()
        if row.remittance_enc and decrypt_data(row.remittance_enc, master_key) == label
    ]
    return row.uuid


def _decide(session: Session, master_key: str, first: str, second: str, kind: Kind) -> None:
    record_decision(session, USER, master_key, _id(session, master_key, first), _id(session, master_key, second), kind)


def _month(session: Session, master_key: str, period: str):
    return list_month_transactions(session, USER, master_key, period)


def _status(month, label: str) -> Status | None:
    [item] = [tx for tx in month.transactions if tx.label == label]
    return item.transfer_status


class TestReview:
    def test_a_pair_seen_once_is_only_suggested_and_both_legs_count(
        self, session: Session, master_key: str
    ):
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
        )
        month = _month(session, master_key, "2023-04")
        assert month.internal_transfers_excluded == 0
        assert month.inflow == month.outflow == 85
        assert month.transfer_questions == 1
        assert _status(month, "Virement de : Jean Tiers") is Status.SUGGESTED

    def test_a_rejected_pair_counts_on_both_sides(self, session: Session, master_key: str):
        """A refund to the cent from a third party is income, and the purchase spending."""
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
        )
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.NOT_TRANSFER)

        month = _month(session, master_key, "2023-04")
        assert month.internal_transfers_excluded == 0
        assert month.inflow == month.outflow == 85

    def test_a_rejection_frees_the_legs_for_their_true_pair(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2024-09-18", "10.00", "DBIT", "VIR INST REMBOURSEMENT ALICE"),
            (NEOBANK, "2024-09-18", "10.00", "CRDT", "Recharge sur Apple Pay via *6969"),
            (CURRENT, "2024-09-19", "10.00", "DBIT", "CARTE 18/09/24 NEOBANK**7500 CB*08"),
        )
        _decide(session, master_key, "VIR INST REMBOURSEMENT ALICE", "Recharge sur Apple Pay via *6969", Kind.NOT_TRANSFER)

        month = _month(session, master_key, "2024-09")
        assert _status(month, "CARTE 18/09/24 NEOBANK**7500 CB*08") is Status.SUGGESTED
        assert _status(month, "VIR INST REMBOURSEMENT ALICE") is None


class TestLearning:
    def test_a_refund_from_a_rejected_third_party_is_never_paired_again(
        self, session: Session, master_key: str
    ):
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
            (CURRENT, "2023-08-10", "35.04", "DBIT", "CARTE 08/08/23 GRAND FRAIS 4 CB*88"),
            (NEOBANK, "2023-08-08", "35.04", "CRDT", "VIREMENT DE : JEAN TIERS"),
        )
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.NOT_TRANSFER)

        month = _month(session, master_key, "2023-08")
        assert month.internal_transfers_excluded == 0
        assert month.inflow == month.outflow

    def test_a_top_up_reading_like_a_confirmed_one_is_trusted(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (NEOBANK, "2023-12-14", "18.58", "CRDT", "Recharge sur Apple Pay via *0733"),
            (CURRENT, "2023-12-18", "18.58", "DBIT", "CARTE 14/12/23 NEOBANK**6169 CB*88"),
            (NEOBANK, "2024-12-16", "10.00", "CRDT", "Recharge sur Apple Pay via *6969"),
            (CURRENT, "2024-12-17", "10.00", "DBIT", "CARTE 16/12/24 NEOBANK**7500 CB*08"),
        )
        _decide(session, master_key, "CARTE 14/12/23 NEOBANK**6169 CB*88", "Recharge sur Apple Pay via *0733", Kind.TRANSFER)

        assert _status(_month(session, master_key, "2023-12"), "Recharge sur Apple Pay via *0733") is Status.CONFIRMED
        month = _month(session, master_key, "2024-12")
        assert _status(month, "Recharge sur Apple Pay via *6969") is Status.LEARNED
        assert month.transfer_questions == 0

    def test_a_learned_pair_wins_a_tie_against_a_coincidence(self, session: Session, master_key: str):
        """Two debits of the top-up's amount, one banking day either side of it:
        the gaps cannot choose, the labels can."""
        _ops(
            session, master_key,
            (NEOBANK, "2026-06-25", "20.00", "CRDT", "Apple Pay Top-Up by *2793"),
            (CURRENT, "2026-06-26", "20.00", "DBIT", "TDF EMIS VIA CB 25/06/26 NEOBANK CB*08"),
            (CURRENT, "2026-07-15", "30.00", "DBIT", "PRLV SEPA Salle de sport"),
            (NEOBANK, "2026-07-16", "30.00", "CRDT", "APPLE PAY TOP-UP BY *2793"),
            (CURRENT, "2026-07-17", "30.00", "DBIT", "TDF EMIS VIA CB 16/07/26 NEOBANK CB*08"),
        )
        _decide(session, master_key, "TDF EMIS VIA CB 25/06/26 NEOBANK CB*08", "Apple Pay Top-Up by *2793", Kind.TRANSFER)

        month = _month(session, master_key, "2026-07")
        assert _status(month, "TDF EMIS VIA CB 16/07/26 NEOBANK CB*08") is Status.LEARNED
        assert _status(month, "PRLV SEPA Salle de sport") is None

    def test_withdrawing_a_decision_withdraws_what_it_taught(self, session: Session, master_key: str):
        """The rejection, replaced, no longer keeps the next refund apart."""
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
            (CURRENT, "2023-08-10", "35.04", "DBIT", "CARTE 08/08/23 GRAND FRAIS 4 CB*88"),
            (NEOBANK, "2023-08-08", "35.04", "CRDT", "VIREMENT DE : JEAN TIERS"),
        )
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.NOT_TRANSFER)
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.TRANSFER)

        assert _status(_month(session, master_key, "2023-08"), "VIREMENT DE : JEAN TIERS") is Status.SUGGESTED


class TestBinding:
    def test_a_confirmed_transfer_pairs_beyond_the_tolerance(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Moi"),
            (CURRENT, "2026-03-23", "50.00", "CRDT", "VIR SEPA Moi"),
        )
        assert _month(session, master_key, "2026-03").internal_transfers_excluded == 0

        _decide(session, master_key, "To Moi", "VIR SEPA Moi", Kind.TRANSFER)
        month = _month(session, master_key, "2026-03")
        assert month.internal_transfers_excluded == 1
        assert month.inflow == month.outflow == 0

    def test_a_cancellation_on_one_account_leaves_the_totals(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2024-06-03", "2000.00", "DBIT", "VIR INST MOI"),
            (CURRENT, "2024-06-03", "2000.00", "CRDT", "REJ VIR INST MOI"),
        )
        _decide(session, master_key, "REJ VIR INST MOI", "VIR INST MOI", Kind.REVERSAL)

        month = _month(session, master_key, "2024-06")
        assert month.inflow == month.outflow == 0
        assert month.internal_transfers_excluded == 0
        assert (month.reversals_excluded, month.reversals_amount) == (1, 2000)
        flows = compute_real_flows(session, USER, master_key, months=1, today=date(2024, 6, 30))
        assert (flows.outflow, flows.reversals_excluded) == (0, 1)
        assert _status(month, "VIR INST MOI") is Status.REVERSAL

        _decide(session, master_key, "VIR INST MOI", "REJ VIR INST MOI", Kind.NOT_TRANSFER)
        assert _month(session, master_key, "2024-06").reversals_excluded == 0


    def test_binding_a_leg_again_drops_its_previous_pair(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-05-26", "250.00", "DBIT", "VIR Virement depuis Compte courant"),
            (SAVINGS, "2025-05-26", "250.00", "CRDT", "VIR VIREMENT DEPUIS COMPTE COURANT"),
            (NEOBANK, "2025-05-27", "250.00", "CRDT", "Virement de : Jean Tiers"),
        )
        _decide(session, master_key, "VIR Virement depuis Compte courant", "Virement de : Jean Tiers", Kind.TRANSFER)
        _decide(session, master_key, "VIR Virement depuis Compte courant", "VIR VIREMENT DEPUIS COMPTE COURANT", Kind.TRANSFER)

        month = _month(session, master_key, "2025-05")
        assert _status(month, "VIR VIREMENT DEPUIS COMPTE COURANT") is Status.CONFIRMED
        assert _status(month, "Virement de : Jean Tiers") is None
        assert len(session.exec(select(BankTransferDecision)).all()) == 1


class TestValidation:
    def _pair(self, session: Session, master_key: str, *operations) -> tuple[str, str]:
        _ops(session, master_key, *operations)
        return _id(session, master_key, operations[0][4]), _id(session, master_key, operations[1][4])

    @pytest.mark.parametrize(
        "operations, kind",
        [
            (((CURRENT, "2025-01-10", "5.00", "DBIT", "a"), (NEOBANK, "2025-01-10", "5.00", "DBIT", "b")), Kind.TRANSFER),
            (((CURRENT, "2025-01-10", "5.00", "DBIT", "a"), (NEOBANK, "2025-01-10", "6.00", "CRDT", "b")), Kind.TRANSFER),
            (((CURRENT, "2025-01-10", "5.00", "DBIT", "a"), (CURRENT, "2025-01-10", "5.00", "CRDT", "b")), Kind.TRANSFER),
            (((CURRENT, "2025-01-10", "5.00", "DBIT", "a"), (NEOBANK, "2025-01-10", "5.00", "CRDT", "b")), Kind.REVERSAL),
            (((CURRENT, "2025-01-01", "5.00", "DBIT", "a"), (NEOBANK, "2025-02-15", "5.00", "CRDT", "b")), Kind.TRANSFER),
        ],
        ids=["same-direction", "other-amount", "transfer-on-one-account", "cancellation-across-accounts", "too-far-apart"],
    )
    def test_an_impossible_decision_is_refused(self, session: Session, master_key: str, operations, kind):
        first, second = self._pair(session, master_key, *operations)
        with pytest.raises(DecisionError):
            record_decision(session, USER, master_key, first, second, kind)

    def test_an_unknown_operation_is_not_found(self, session: Session, master_key: str):
        first, _ = self._pair(
            session, master_key,
            (CURRENT, "2025-01-10", "5.00", "DBIT", "a"), (NEOBANK, "2025-01-10", "5.00", "CRDT", "b"),
        )
        with pytest.raises(TransactionNotFoundError):
            record_decision(session, USER, master_key, first, "not-a-row", Kind.TRANSFER)


class TestCounterparts:
    def test_the_candidates_share_the_amount_and_come_nearest_first(
        self, session: Session, master_key: str
    ):
        _ops(
            session, master_key,
            (CURRENT, "2024-06-03", "2000.00", "DBIT", "VIR INST MOI"),
            (CURRENT, "2024-06-03", "2000.00", "CRDT", "REJ VIR INST MOI"),
            (NEOBANK, "2024-06-10", "2000.00", "CRDT", "Paiement de Moi"),
            (NEOBANK, "2024-06-04", "1999.00", "CRDT", "Autre montant"),
            (SAVINGS, "2024-06-05", "2000.00", "DBIT", "Même sens"),
        )
        candidates = list_transfer_counterparts(session, USER, master_key, _id(session, master_key, "VIR INST MOI"))
        assert [c.label for c in candidates] == ["REJ VIR INST MOI", "Paiement de Moi"]

    def test_an_operation_of_nobody_readable_is_not_found(self, session: Session, master_key: str):
        with pytest.raises(TransactionNotFoundError):
            list_transfer_counterparts(session, USER, master_key, "not-a-row")


class TestDeletion:
    def test_deleting_an_account_forgets_its_decisions(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
        )
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.NOT_TRANSFER)

        delete_bank_account(session, NEOBANK, master_key)
        assert session.exec(select(BankTransferDecision)).all() == []

    def test_unlinking_with_the_movements_forgets_their_decisions(self, session: Session, master_key: str):
        from services.banking.linking import unlink_account

        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
        )
        _decide(session, master_key, "CARTE 17/04/23 DECATHLON 4 CB*88", "Virement de : Jean Tiers", Kind.NOT_TRANSFER)

        unlink_account(session, USER, master_key, NEOBANK, delete_transactions=False)
        assert len(session.exec(select(BankTransferDecision)).all()) == 1
        _link(session, master_key, NEOBANK)
        unlink_account(session, USER, master_key, NEOBANK, delete_transactions=True)
        assert session.exec(select(BankTransferDecision)).all() == []
