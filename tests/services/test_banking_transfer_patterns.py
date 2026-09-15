"""
The tiers that settle a pair without asking (flows._internal_transfer_legs) and
the stored patterns they read (services/banking/transfer_patterns.py).

Labels are shaped like the real ones the cases came from, names replaced.
"""
from sqlmodel import Session, select

from dtos.banking import BankTransferStatus as Status
from models.bank import BankAccount
from models.banking import BankTransaction, BankTransferPatterns
from services.banking.flows import list_month_transactions, transfer_patterns
from services.encryption import encrypt_data
from tests.services.test_banking_flows import USER, _link, _raw, _store

CURRENT, NEOBANK, LIVRET = "current", "neobank", "savings"  # "savings" is a Livret A


def _ops(session: Session, master_key: str, *operations: tuple[str, str, str, str, str]) -> None:
    """(account, day, amount, direction, label)"""
    for account in {op[0] for op in operations}:
        _link(session, master_key, account)
    for n, (account, day, amount, direction, label) in enumerate(operations):
        _store(session, master_key, account, _raw(amount, direction, day, ref=f"{account}-{day}-{n}", label=label))


def _month(session: Session, master_key: str, period: str):
    return list_month_transactions(session, USER, master_key, period)


def _statuses(month) -> dict[str, Status | None]:
    return {tx.label: tx.transfer_status for tx in month.transactions}


def _top_up(month: str, day: str, amount: str) -> tuple[tuple[str, str, str, str, str], ...]:
    """A top-up of the neobank account, paid by card from the current account."""
    return (
        (NEOBANK, f"2025-{month}-{day}", amount, "CRDT", f"Recharge sur Apple Pay via *{month}{day}"),
        (CURRENT, f"2025-{month}-{int(day) + 1:02d}", amount, "DBIT", f"CARTE {day}/{month}/25 NEOBANK**{day}{month} CB*08"),
    )


class TestSavings:
    def test_a_pair_touching_a_livret_is_deducted_at_first_sight(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-08-18", "1000.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2025-08-18", "1000.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        month = _month(session, master_key, "2025-08")
        assert month.internal_transfers_excluded == 1
        assert month.inflow == month.outflow == 0
        assert set(_statuses(month).values()) == {Status.SAVINGS}


class TestRecurrence:
    def test_a_shape_seen_three_times_is_deducted_without_asking(self, session: Session, master_key: str):
        _ops(session, master_key, *_top_up("03", "05", "20.00"), *_top_up("04", "10", "35.50"), *_top_up("05", "14", "12.00"))

        month = _month(session, master_key, "2025-05")
        assert month.internal_transfers_excluded == 1
        assert month.transfer_questions == 0
        assert set(_statuses(month).values()) == {Status.RECURRING}

    def test_a_shape_seen_twice_is_still_a_question(self, session: Session, master_key: str):
        """A third party refunding two purchases at one merchant pairs twice alike."""
        _ops(session, master_key, *_top_up("03", "05", "20.00"), *_top_up("04", "10", "35.50"))

        month = _month(session, master_key, "2025-04")
        assert month.internal_transfers_excluded == 0
        assert month.transfer_questions == 1

    def test_the_third_occurrence_settles_the_earlier_ones_too(self, session: Session, master_key: str):
        _ops(session, master_key, *_top_up("03", "05", "20.00"), *_top_up("04", "10", "35.50"))
        assert _month(session, master_key, "2025-03").internal_transfers_excluded == 0

        _store(session, master_key, NEOBANK, _raw("12.00", "CRDT", "2025-05-14", ref="n3", label="Recharge sur Apple Pay via *0514"))
        _store(session, master_key, CURRENT, _raw("12.00", "DBIT", "2025-05-15", ref="c3", label="CARTE 14/05/25 NEOBANK**1405 CB*08"))
        assert _month(session, master_key, "2025-03").internal_transfers_excluded == 1

    def test_refunds_from_one_third_party_never_recur_as_a_shape(self, session: Session, master_key: str):
        """The same person refunds, but a different merchant each time: no shape repeats."""
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
            (CURRENT, "2023-05-16", "136.86", "DBIT", "CARTE 15/05/23 MARIN MINASIAN 2 CB*88"),
            (NEOBANK, "2023-05-15", "136.86", "CRDT", "Virement de : Jean Tiers"),
            (CURRENT, "2023-08-10", "35.04", "DBIT", "CARTE 08/08/23 GRAND FRAIS 4 CB*88"),
            (NEOBANK, "2023-08-08", "35.04", "CRDT", "Virement de : Jean Tiers"),
        )
        for period in ("2023-04", "2023-05", "2023-08"):
            month = _month(session, master_key, period)
            assert month.internal_transfers_excluded == 0
            assert month.transfer_questions == 1


class TestRefunds:
    def test_a_refund_naming_the_same_merchant_cancels_the_payment(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-04-07", "260.00", "DBIT", "CARTE 06/04/26 VELONECY CB*08"),
            (CURRENT, "2026-04-09", "260.00", "CRDT", "AVOIR 07/04/26 VELONECY CB*08"),
        )
        month = _month(session, master_key, "2026-04")
        assert month.inflow == month.outflow == 0
        assert (month.reversals_excluded, month.reversals_amount) == (1, 260)
        assert set(_statuses(month).values()) == {Status.REFUND}

    def test_words_every_payment_carries_prove_nothing(self, session: Session, master_key: str):
        """"CARTE" and "CB" sit on most debits of the account: sharing them is no refund."""
        payments = [
            (CURRENT, f"2026-03-{day:02d}", f"{day}.00", "DBIT", f"CARTE {day:02d}/03/26 COMMERCE{day} CB*08")
            for day in range(1, 21)
        ]
        _ops(
            session, master_key, *payments,
            (CURRENT, "2026-03-05", "7.00", "DBIT", "CARTE 05/03/26 BOULANGERIE CB*08"),
            (CURRENT, "2026-03-06", "7.00", "CRDT", "VIR CB 7 JEAN TIERS"),
        )
        month = _month(session, master_key, "2026-03")
        assert month.reversals_excluded == 0

    def test_a_refund_arriving_after_a_month_is_real_income(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-01-05", "40.00", "DBIT", "CARTE 04/01/26 CDISCOUNT CB*08"),
            (CURRENT, "2026-02-20", "40.00", "CRDT", "AVOIR 19/02/26 CDISCOUNT CB*08"),
        )
        assert _month(session, master_key, "2026-02").reversals_excluded == 0


class TestStoredPatterns:
    def test_they_are_built_once_and_reused_while_nothing_moves(self, session: Session, master_key: str):
        _ops(session, master_key, *_top_up("03", "05", "20.00"))
        transfer_patterns(session, USER, master_key)
        [row] = session.exec(select(BankTransferPatterns)).all()
        built_at = row.built_at

        _month(session, master_key, "2025-03")
        session.refresh(row)
        assert row.built_at == built_at

    def test_an_account_changing_type_rebuilds_them(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-08-18", "1000.00", "DBIT", "VIR Virement vers Compte"),
            (NEOBANK, "2025-08-18", "1000.00", "CRDT", "VIR Virement depuis Compte"),
        )
        assert _month(session, master_key, "2025-08").transfer_questions == 1

        account = session.get(BankAccount, NEOBANK)
        account.account_type_enc = encrypt_data("LDD", master_key)
        session.add(account)
        session.commit()
        month = _month(session, master_key, "2025-08")
        assert (month.transfer_questions, month.internal_transfers_excluded) == (0, 1)
        assert transfer_patterns(session, USER, master_key).questions == {}

    def test_rows_stored_before_signatures_get_one(self, session: Session, master_key: str):
        _ops(session, master_key, *_top_up("03", "05", "20.00"), *_top_up("04", "10", "35.50"), *_top_up("05", "14", "12.00"))
        for row in session.exec(select(BankTransaction)).all():
            row.label_signature_bidx = None
            session.add(row)
        session.commit()

        assert _month(session, master_key, "2025-05").internal_transfers_excluded == 1
        assert all(row.label_signature_bidx for row in session.exec(select(BankTransaction)).all())

    def test_the_questions_are_counted_per_month(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2023-04-18", "85.00", "DBIT", "CARTE 17/04/23 DECATHLON 4 CB*88"),
            (NEOBANK, "2023-04-19", "85.00", "CRDT", "Virement de : Jean Tiers"),
            (CURRENT, "2023-08-10", "35.04", "DBIT", "CARTE 08/08/23 GRAND FRAIS 4 CB*88"),
            (NEOBANK, "2023-08-08", "35.04", "CRDT", "Virement de : Jean Tiers"),
        )
        assert transfer_patterns(session, USER, master_key).questions == {"2023-04": 1, "2023-08": 1}
