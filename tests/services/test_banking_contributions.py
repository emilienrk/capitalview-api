"""
What the user's investment accounts prove about a transfer sent
(services/banking/contributions.py), and what it types
(services/banking/cashflow_types.py).

Labels are shaped like the real ones, names replaced.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlmodel import Session

from dtos.banking import (
    BankContributionDecisionKind as Said,
    BankHistoryKind,
    BankTransferDecisionKind,
    CashflowType as Type,
    TypeScope,
    TypeSource as Source,
)
from dtos.crypto import CryptoCompositeTransactionCreate
from models.crypto import CryptoAccount
from models.enums import CryptoCompositeTransactionType
from models.stock import StockAccount, StockTransaction
from services.banking.contributions import (
    Candidate,
    Contribution,
    Contributions,
    load_contributions,
    match_candidates,
    unfaced,
)
from services.banking.flows import (
    clear_transaction_type, list_flow_group, list_month_transactions, review_queue, set_transaction_type,
)
from services.banking.contribution_decisions import record_contribution_decision
from services.banking.history import action_history, undo_action
from services.banking.real_cashflow import real_cashflow_month
from services.banking.transfer_decisions import DecisionError, record_decision
from services.crypto_transaction import create_composite_crypto_transaction
from services.encryption import encrypt_data, hash_index
from services.stock_transaction import create_eur_deposit
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, TODAY, _month, _ops

PEA = "pea-account"
WALLET = "wallet-account"


def _pea(session: Session, master_key: str) -> None:
    session.add(StockAccount(
        uuid=PEA,
        user_uuid_bidx=hash_index(USER, master_key),
        name_enc=encrypt_data("PEA", master_key),
        account_type_enc=encrypt_data("PEA", master_key),
    ))
    session.commit()


def _wallet(session: Session, master_key: str) -> None:
    session.add(CryptoAccount(
        uuid=WALLET,
        user_uuid_bidx=hash_index(USER, master_key),
        name_enc=encrypt_data("Mon Portefeuille", master_key),
    ))
    session.commit()


def _deposit(session: Session, master_key: str, day: str, amount: str, account: str = PEA, **kwargs):
    return create_eur_deposit(
        session, account, Decimal(amount), datetime.fromisoformat(f"{day}T10:00:00"), master_key, **kwargs
    )


def _withdraw(session: Session, master_key: str, day: str, amount: str, account: str = PEA) -> None:
    """A EUR withdrawal, stored as `create_eur_deposit` stores a deposit: going
    through `create_stock_transaction` would look "EUR" up on the market."""
    session.add(StockTransaction(
        account_id_bidx=hash_index(account, master_key),
        asset_key_enc=encrypt_data("EUR", master_key),
        type_enc=encrypt_data("WITHDRAW", master_key),
        amount_enc=encrypt_data(amount, master_key),
        price_per_unit_enc=encrypt_data("1", master_key),
        fees_enc=encrypt_data("0", master_key),
        executed_at_enc=encrypt_data(f"{day}T10:00:00", master_key),
    ))
    session.commit()


# ---------------------------------------------------------------------------
# The matching itself
# ---------------------------------------------------------------------------

def _contributions(*items: tuple[str, str]) -> Contributions:
    by_amount: dict[tuple[bool, Decimal], list[Contribution]] = {}
    for n, (day, amount) in enumerate(items):
        key = (True, Decimal(amount))
        by_amount.setdefault(key, []).append(
            Contribution("PEA", date.fromisoformat(day), Decimal(amount), is_deposit=True, ref=f"deposit-{n}", account_id=PEA)
        )
    return Contributions(by_amount=by_amount)


class TestMatching:
    def test_the_same_day_and_amount_is_evidence(self):
        candidates = [Candidate(0, date(2026, 3, 5), Decimal("200"), False)]
        [match] = match_candidates(candidates, _contributions(("2026-03-05", "200"))).values()
        assert match.exact and match.contribution.amount == Decimal("200")

    def test_a_nearby_day_is_only_a_hint(self):
        candidates = [Candidate(0, date(2026, 3, 8), Decimal("200"), False)]
        [match] = match_candidates(candidates, _contributions(("2026-03-06", "200"))).values()
        assert not match.exact

    def test_a_friday_withdrawal_booked_on_tuesday_is_a_hint(self):
        """Four calendar days, two banking days: a broker's withdrawal on a Friday."""
        candidates = [Candidate(0, date(2024, 11, 19), Decimal("32.89"), True)]
        withdrawal = Contribution("CTO", date(2024, 11, 15), Decimal("32.89"), is_deposit=False, ref="w", account_id=PEA)
        contributions = Contributions(by_amount={(False, Decimal("32.89")): [withdrawal]})
        [match] = match_candidates(candidates, contributions).values()
        assert not match.exact

    def test_the_easter_closing_days_do_not_count(self):
        """The Thursday before Easter to the Wednesday after: two banking days."""
        candidates = [Candidate(0, date(2026, 4, 8), Decimal("200"), False)]
        [match] = match_candidates(candidates, _contributions(("2026-04-02", "200"))).values()
        assert not match.exact

    def test_three_banking_days_apart_says_nothing(self):
        candidates = [Candidate(0, date(2026, 3, 5), Decimal("200"), False)]
        assert match_candidates(candidates, _contributions(("2026-03-02", "200"))) == {}

    def test_one_deposit_never_proves_two_debits_of_the_day(self):
        candidates = [
            Candidate(0, date(2026, 3, 5), Decimal("200"), False),
            Candidate(1, date(2026, 3, 5), Decimal("200"), False),
        ]
        matches = match_candidates(candidates, _contributions(("2026-03-05", "200")))
        assert [m.exact for m in matches.values()] == [False, False]

    def test_two_deposits_prove_two_debits_of_the_day(self):
        candidates = [
            Candidate(0, date(2026, 3, 5), Decimal("200"), False),
            Candidate(1, date(2026, 3, 5), Decimal("200"), False),
        ]
        matches = match_candidates(candidates, _contributions(("2026-03-05", "200"), ("2026-03-05", "200")))
        assert [m.exact for m in matches.values()] == [True, True]

    def test_a_deposit_spent_as_evidence_is_not_a_hint_elsewhere(self):
        candidates = [
            Candidate(0, date(2026, 3, 5), Decimal("200"), False),
            Candidate(1, date(2026, 3, 7), Decimal("200"), False),
        ]
        matches = match_candidates(candidates, _contributions(("2026-03-05", "200")))
        assert matches[0].exact
        assert 1 not in matches

    def test_a_credit_faces_a_withdrawal_not_a_deposit(self):
        candidates = [Candidate(0, date(2026, 3, 5), Decimal("200"), True)]
        assert match_candidates(candidates, _contributions(("2026-03-05", "200"))) == {}

    def test_a_deposit_above_the_debit_is_never_matched(self):
        candidates = [Candidate(0, date(2026, 3, 5), Decimal("199.99"), False)]
        assert match_candidates(candidates, _contributions(("2026-03-05", "200"))) == {}

    def test_a_fee_kept_on_the_way_still_proves_the_deposit(self):
        candidates = [Candidate(0, date(2026, 6, 2), Decimal("100"), False)]
        [match] = match_candidates(candidates, _contributions(("2026-06-02", "99"))).values()
        assert match.exact and match.contribution.amount == Decimal("99")

    def test_past_the_largest_fee_nothing_is_matched(self):
        # 2 % of 1 000 € is 20 €: 21 € kept is another movement.
        candidates = [Candidate(0, date(2026, 6, 2), Decimal("1021"), False)]
        assert match_candidates(candidates, _contributions(("2026-06-02", "1000"))) == {}
        candidates = [Candidate(0, date(2026, 6, 2), Decimal("1020"), False)]
        assert match_candidates(candidates, _contributions(("2026-06-02", "1000")))[0].exact

    def test_a_fee_apart_proves_nothing_the_day_after(self):
        candidates = [Candidate(0, date(2026, 6, 3), Decimal("100"), False)]
        assert match_candidates(candidates, _contributions(("2026-06-02", "99"))) == {}

    def test_two_debits_a_fee_above_one_deposit_prove_nothing(self):
        candidates = [
            Candidate(0, date(2026, 6, 2), Decimal("100"), False),
            Candidate(1, date(2026, 6, 2), Decimal("99.50"), False),
        ]
        assert match_candidates(candidates, _contributions(("2026-06-02", "99"))) == {}

    def test_an_exact_amount_wins_over_a_fee_apart(self):
        candidates = [
            Candidate(0, date(2026, 6, 2), Decimal("100"), False),
            Candidate(1, date(2026, 6, 2), Decimal("99"), False),
        ]
        matches = match_candidates(candidates, _contributions(("2026-06-02", "99")))
        assert matches == {1: matches[1]} and matches[1].exact

    def test_a_withdrawal_arrives_short_of_itself(self):
        candidates = [Candidate(0, date(2026, 6, 2), Decimal("99"), True)]
        withdrawal = Contribution("PEA", date(2026, 6, 2), Decimal("100"), is_deposit=False, ref="w", account_id=PEA)
        contributions = Contributions(by_amount={(False, Decimal("100")): [withdrawal]})
        assert match_candidates(candidates, contributions)[0].exact


# ---------------------------------------------------------------------------
# The declared movements no bank movement faces
# ---------------------------------------------------------------------------

def _debit(index: int, day: str, amount: str) -> Candidate:
    return Candidate(index, date.fromisoformat(day), Decimal(amount), False)


# The bank history spans March: every deposit below is judged.
_SPAN = [_debit(90, "2026-03-01", "1"), _debit(91, "2026-03-31", "1")]


class TestUnfaced:
    def test_a_deposit_no_debit_faces_is_reported(self):
        [found] = unfaced([*_SPAN, _debit(0, "2026-03-05", "150")], _contributions(("2026-03-05", "200")))
        assert (found.day, found.amount) == (date(2026, 3, 5), Decimal("200"))

    def test_a_debit_two_banking_days_off_faces_it(self):
        assert unfaced([*_SPAN, _debit(0, "2026-03-09", "200")], _contributions(("2026-03-05", "200"))) == []

    def test_a_debit_three_banking_days_off_does_not(self):
        assert len(unfaced([*_SPAN, _debit(0, "2026-03-05", "200")], _contributions(("2026-03-02", "200")))) == 1

    def test_a_fee_kept_on_the_way_still_faces_it(self):
        assert unfaced([*_SPAN, _debit(0, "2026-03-05", "100")], _contributions(("2026-03-05", "99"))) == []

    def test_a_credit_never_faces_a_deposit(self):
        credit = Candidate(0, date(2026, 3, 5), Decimal("200"), True)
        assert len(unfaced([*_SPAN, credit], _contributions(("2026-03-05", "200")))) == 1

    def test_one_debit_faces_one_deposit_only(self):
        found = unfaced(
            [*_SPAN, _debit(0, "2026-03-05", "200")], _contributions(("2026-03-05", "200"), ("2026-03-06", "200")),
        )
        assert [c.day for c in found] == [date(2026, 3, 6)]

    def test_outside_the_bank_history_nothing_is_judged(self):
        """Before the first operation no debit can be read; too close to the
        last one, it may not be booked yet."""
        found = unfaced(_SPAN, _contributions(("2026-02-27", "200"), ("2026-03-30", "200"), ("2026-03-27", "300")))
        assert [c.day for c in found] == [date(2026, 3, 27)]


# ---------------------------------------------------------------------------
# What is read as a declared movement
# ---------------------------------------------------------------------------

class TestLoading:
    def test_a_deposit_and_a_withdrawal_are_read_a_provision_is_not(self, session: Session, master_key: str):
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        _deposit(session, master_key, "2026-03-06", "50", notes="Provision automatique", auto_provision=True)
        _withdraw(session, master_key, "2026-03-09", "80")

        loaded = load_contributions(session, USER, master_key)
        assert sorted((c.day, c.amount, c.is_deposit) for items in loaded.by_amount.values() for c in items) == [
            (date(2026, 3, 5), Decimal("200.00"), True),
            (date(2026, 3, 9), Decimal("80.00"), False),
        ]

    def test_a_provision_stored_before_the_flag_is_marked_on_the_way(self, session: Session, master_key: str):
        _pea(session, master_key)
        row = _deposit(session, master_key, "2026-03-06", "50", notes="Provision automatique")
        load_contributions(session, USER, master_key)

        assert session.get(StockTransaction, row.id).is_auto_provision is True
        assert not load_contributions(session, USER, master_key)

    def test_a_crypto_purchase_funds_itself_and_proves_nothing(self, session: Session, master_key: str):
        _wallet(session, master_key)
        create_composite_crypto_transaction(session, CryptoCompositeTransactionCreate(
            account_id=WALLET, asset_key="BTC", type=CryptoCompositeTransactionType.CRYPTO_DEPOSIT,
            amount=Decimal("0.01"), eur_amount=Decimal("200"), executed_at=datetime(2026, 3, 5),
        ), master_key)
        assert not load_contributions(session, USER, master_key)

    def test_a_fiat_deposit_on_a_wallet_is_a_contribution(self, session: Session, master_key: str):
        _wallet(session, master_key)
        create_composite_crypto_transaction(session, CryptoCompositeTransactionCreate(
            account_id=WALLET, asset_key="EUR", type=CryptoCompositeTransactionType.FIAT_DEPOSIT,
            amount=Decimal("200"), executed_at=datetime(2026, 3, 5),
        ), master_key)
        [contribution] = [c for items in load_contributions(session, USER, master_key).by_amount.values() for c in items]
        assert (contribution.account_name, contribution.amount, contribution.is_deposit) == (
            "Mon Portefeuille", Decimal("200.00"), True,
        )


# ---------------------------------------------------------------------------
# What it changes in the operations list
# ---------------------------------------------------------------------------

def _answer(session: Session, master_key: str, tx_id: str, kind: Type = Type.INVESTMENT) -> None:
    set_transaction_type(session, USER, master_key, tx_id, kind, TypeScope.OPERATION)


def _say(session: Session, master_key: str, tx, kind: Said = Said.CONTRIBUTION) -> None:
    """The user's yes, or no, to the declared movement shown beside an operation."""
    record_contribution_decision(session, USER, master_key, tx.id, tx.contribution.id, kind)


class TestOperations:
    def test_a_debit_the_day_of_a_deposit_asks_first_and_shows_it(self, session: Session, master_key: str):
        """The same amount on the same day is a coincidence until the user says
        otherwise: a 200 € purchase the day of a 200 € deposit looks the same."""
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.flow_question is not None
        assert (tx.contribution.account_name, tx.contribution.exact) == ("PEA", True)

    def test_once_answered_the_same_label_to_the_same_account_is_invested_unasked(
        self, session: Session, master_key: str
    ):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-03-05", "300")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)
        assert tx.flow_question is None

    def test_an_answer_vouches_for_its_investment_account_only(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        session.add(StockAccount(
            uuid="cto-account",
            user_uuid_bidx=hash_index(USER, master_key),
            name_enc=encrypt_data("CTO", master_key),
            account_type_enc=encrypt_data("CTO", master_key),
        ))
        session.commit()
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-03-05", "300", account="cto-account")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)

        [tx] = _month(session, master_key).values()
        assert (tx.type_source, tx.flow_question is not None) == (Source.DEFAULT, True)

    def test_a_deposit_two_days_later_only_shows_beside_the_question(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-07", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.flow_question is not None
        assert (tx.contribution.day, tx.contribution.exact) == (date(2026, 3, 7), False)

    def test_a_card_payment_facing_a_deposit_asks_like_any_debit(self, session: Session, master_key: str):
        """No means of payment is read from a label (docs/bank-sorting.md)."""
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE 04/03/26 DARTY CB*08"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.flow_question is not None

    def test_a_debit_nothing_faces_under_the_threshold_is_spending_and_asks_nothing(
        self, session: Session, master_key: str
    ):
        _ops(session, master_key, (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"))

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.flow_question) == (Type.EXPENSE, Source.DEFAULT, None)

    def test_the_user_corrects_a_deduction_and_takes_it_back(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-03-05", "300")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)
        [tx] = _month(session, master_key).values()

        forced = set_transaction_type(session, USER, master_key, tx.id, Type.EXPENSE, TypeScope.OPERATION)
        assert (forced.transaction.cashflow_type, forced.transaction.type_source) == (Type.EXPENSE, Source.OVERRIDE)

        back = clear_transaction_type(session, USER, master_key, tx.id)
        assert (back.cashflow_type, back.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)

    def test_a_provision_the_app_wrote_proves_nothing(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200", notes="Provision automatique", auto_provision=True)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.contribution is None

    def test_a_credit_the_day_of_a_withdrawal_is_investment_taken_back_once_answered(
        self, session: Session, master_key: str
    ):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "50.00", "CRDT", "VIR Virement depuis PEA"),
            (CURRENT, "2026-03-05", "80.00", "CRDT", "VIR Virement depuis PEA"),
        )
        _pea(session, master_key)
        _withdraw(session, master_key, "2026-02-05", "50")
        _withdraw(session, master_key, "2026-03-05", "80")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)

    def test_the_month_counts_a_vouched_deposit_as_invested(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-03-05", "200")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)

        month = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY)
        assert (month.totals.investment, month.totals.expenses) == (Decimal("200"), Decimal("0"))

    def test_the_operations_facing_a_deposit_ask_apart_from_their_label(self, session: Session, master_key: str):
        """Seen on the dump: the label's question sat on its last operation,
        which faced nothing, and only said that 21 others might be deposits."""
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-20", "150.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-25", "600.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        _deposit(session, master_key, "2026-03-21", "150")

        asked = {tx.amount: tx for tx in _operations(session, master_key) if tx.flow_question}
        assert {amount: (tx.asks_contribution, tx.flow_question.operation_count) for amount, tx in asked.items()} == {
            Decimal("150.00"): (True, 2), Decimal("600.00"): (False, 1),
        }
        assert [tx.amount for tx in list_flow_group(session, USER, master_key, asked[Decimal("150.00")].id)] == [
            Decimal("150.00"), Decimal("200.00"),
        ]

    def test_each_investment_account_asks_on_its_own(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-20", "150.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _pea(session, master_key)
        session.add(StockAccount(
            uuid="cto-account",
            user_uuid_bidx=hash_index(USER, master_key),
            name_enc=encrypt_data("CTO", master_key),
            account_type_enc=encrypt_data("CTO", master_key),
        ))
        session.commit()
        _deposit(session, master_key, "2026-03-05", "200")
        _deposit(session, master_key, "2026-03-20", "150", account="cto-account")

        assert sorted(tx.flow_question.operation_count for tx in _operations(session, master_key) if tx.flow_question) == [1, 1]

    def test_the_ticked_operations_take_the_answer_each_with_its_own_deposit(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-20", "150.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-07", "200")
        _deposit(session, master_key, "2026-03-21", "150")
        first, last = sorted(_operations(session, master_key), key=lambda tx: tx.operation_date)

        record_contribution_decision(
            session, USER, master_key, last.id, last.contribution.id, Said.CONTRIBUTION,
            [(first.id, first.contribution.id)],
        )

        assert {tx.type_source for tx in _operations(session, master_key)} == {Source.CONTRIBUTION}

    def test_one_ticked_operation_that_faces_nothing_refuses_them_all(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-20", "150.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _pea(session, master_key)
        deposit = _deposit(session, master_key, "2026-03-05", "200")
        first, last = sorted(_operations(session, master_key), key=lambda tx: tx.operation_date)

        with pytest.raises(DecisionError):
            record_contribution_decision(
                session, USER, master_key, first.id, deposit.id, Said.CONTRIBUTION, [(last.id, deposit.id)],
            )

    def test_a_rule_on_its_label_still_asks_the_day_a_deposit_faces_it(self, session: Session, master_key: str):
        """One label may go to the investment account one day and anywhere the next."""
        _ops(
            session, master_key,
            (CURRENT, "2026-02-20", "150.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-18", "150")
        _deposit(session, master_key, "2026-03-05", "200")
        [asked] = _month(session, master_key, "2026-02").values()
        set_transaction_type(session, USER, master_key, asked.id, Type.EXPENSE, TypeScope.LABEL)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.RULE)
        assert (tx.flow_question is not None, tx.asks_contribution) == (True, True)

    def test_a_type_given_by_hand_asks_again_the_day_a_deposit_faces_it(self, session: Session, master_key: str):
        """The deposit imported after the user typed the operation: a yes makes it that deposit."""
        _ops(session, master_key, (CURRENT, "2026-03-05", "500.00", "DBIT", "VIR INST JEAN MARTIN"))
        [typed] = _month(session, master_key).values()
        _answer(session, master_key, typed.id, Type.NEUTRAL)
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "500")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.asks_contribution) == (Type.NEUTRAL, Source.OVERRIDE, True)

        _say(session, master_key, tx)
        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.asks_contribution) == (Type.INVESTMENT, Source.CONTRIBUTION, False)

    def test_a_no_keeps_the_type_given_by_hand(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "500.00", "DBIT", "VIR INST JEAN MARTIN"))
        [typed] = _month(session, master_key).values()
        _answer(session, master_key, typed.id, Type.NEUTRAL)
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "500")
        [tx] = _month(session, master_key).values()

        _say(session, master_key, tx, Said.NOT_CONTRIBUTION)
        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.asks_contribution) == (Type.NEUTRAL, Source.OVERRIDE, False)

    def test_a_yes_on_its_label_leaves_a_type_given_by_hand_asked(self, session: Session, master_key: str):
        def by_amount():
            return {tx.amount: tx for tx in _operations(session, master_key)}

        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "500.00", "DBIT", "VIR INST JEAN MARTIN"),
            (CURRENT, "2026-03-12", "300.00", "DBIT", "VIR INST JEAN MARTIN"),
        )
        _answer(session, master_key, by_amount()[Decimal("300.00")].id, Type.SAVING)
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "500")
        _deposit(session, master_key, "2026-03-12", "300")

        _say(session, master_key, by_amount()[Decimal("500.00")])
        listed = by_amount()
        assert listed[Decimal("500.00")].type_source is Source.CONTRIBUTION
        kept = listed[Decimal("300.00")]
        assert (kept.cashflow_type, kept.type_source, kept.asks_contribution) == (Type.SAVING, Source.OVERRIDE, True)

    def test_an_investment_given_by_hand_asks_nothing(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "500.00", "DBIT", "VIR INST JEAN MARTIN"))
        [typed] = _month(session, master_key).values()
        _answer(session, master_key, typed.id, Type.INVESTMENT)
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "500")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.asks_contribution) == (Type.INVESTMENT, Source.OVERRIDE, False)

    def test_a_rule_on_its_label_asks_nothing_of_a_deposit_days_away(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR INST JEAN MARTIN"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-07", "200")
        [asked] = _month(session, master_key).values()
        set_transaction_type(session, USER, master_key, asked.id, Type.EXPENSE, TypeScope.LABEL)

        [tx] = _month(session, master_key).values()
        assert (tx.type_source, tx.contribution.exact, tx.asks_contribution) == (Source.RULE, False, False)


def _operations(session: Session, master_key: str, period: str = "2026-03"):
    return list_month_transactions(session, USER, master_key, period).transactions


class TestDecisions:
    """What the user says of an operation facing a declared movement
    (services/banking/contribution_decisions.py)."""

    def test_a_yes_vouches_for_its_label_in_every_month_read(self, session: Session, master_key: str):
        """The lesson is drawn from the whole history: a month far from the
        answer reads the same as the month answered."""
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-06-05", "300.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-06-05", "300")
        [last] = _month(session, master_key, "2026-06").values()
        _say(session, master_key, last)

        [tx] = _month(session, master_key, "2026-02").values()
        assert (tx.cashflow_type, tx.type_source, tx.flow_question) == (Type.INVESTMENT, Source.CONTRIBUTION, None)

    def test_a_type_given_teaches_its_label_nothing(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "200")
        _deposit(session, master_key, "2026-03-05", "300")
        [first] = _month(session, master_key, "2026-02").values()
        _answer(session, master_key, first.id)

        [tx] = _month(session, master_key).values()
        assert (tx.type_source, tx.contribution.exact) == (Source.DEFAULT, True)

    def test_a_no_reads_the_operation_as_any_other(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE 04/03/26 DARTY CB*08"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        [asked] = _month(session, master_key).values()
        _say(session, master_key, asked, Said.NOT_CONTRIBUTION)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.contribution, tx.flow_question) == (
            Type.EXPENSE, Source.DEFAULT, None, None,
        )

    def test_a_no_leaves_the_deposit_to_the_other_operation_of_the_day(self, session: Session, master_key: str):
        """Two 200 € debits and one 200 € deposit: which one went is unknown
        until the user rules one out."""
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE 04/03/26 DARTY CB*08"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        month = _month(session, master_key)
        assert not month["VIR Virement vers PEA"].contribution.exact
        _say(session, master_key, month["CARTE 04/03/26 DARTY CB*08"], Said.NOT_CONTRIBUTION)

        assert _month(session, master_key)["VIR Virement vers PEA"].contribution.exact

    def test_a_yes_settles_a_day_holding_two_candidates(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE 04/03/26 DARTY CB*08"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        _say(session, master_key, _month(session, master_key)["VIR Virement vers PEA"])

        month = _month(session, master_key)
        assert month["VIR Virement vers PEA"].type_source == Source.CONTRIBUTION
        assert month["CARTE 04/03/26 DARTY CB*08"].contribution is None

    def test_a_yes_to_a_deposit_two_days_later_counts_it(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-07", "200")
        _say(session, master_key, *_month(session, master_key).values())

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source, tx.contribution.exact) == (Type.INVESTMENT, Source.CONTRIBUTION, True)

    def test_a_yes_holds_no_longer_once_the_deposit_moved(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        deposit = session.get(StockTransaction, _deposit(session, master_key, "2026-03-05", "200").id)
        _say(session, master_key, *_month(session, master_key).values())

        deposit.executed_at_enc = encrypt_data("2026-03-20T10:00:00", master_key)
        session.add(deposit)
        session.commit()

        [tx] = _month(session, master_key).values()
        assert (tx.type_source, tx.contribution) == (Source.DEFAULT, None)

    def test_a_type_given_by_hand_withdraws_the_yes(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        [asked] = _month(session, master_key).values()
        _say(session, master_key, asked)

        _answer(session, master_key, asked.id, Type.EXPENSE)

        assert [item.kind for item in action_history(session, USER, master_key)] == [BankHistoryKind.TYPE]
        back = clear_transaction_type(session, USER, master_key, asked.id)
        assert (back.cashflow_type, back.type_source) == (Type.EXPENSE, Source.DEFAULT)

    def test_a_yes_replaces_the_type_given_by_hand(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        [asked] = _month(session, master_key).values()
        _answer(session, master_key, asked.id, Type.EXPENSE)
        _say(session, master_key, asked)

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)

    def test_a_movement_that_cannot_be_the_operation_is_refused(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        far = _deposit(session, master_key, "2026-03-25", "200")
        [tx] = _month(session, master_key).values()

        with pytest.raises(DecisionError):
            record_contribution_decision(session, USER, master_key, tx.id, far.id, Said.CONTRIBUTION)

    def test_the_history_lists_the_answer_and_withdraws_it(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
        [asked] = _month(session, master_key).values()
        _say(session, master_key, asked)

        [item] = action_history(session, USER, master_key)
        assert (item.kind, item.name, [op.id for op in item.operations]) == (
            BankHistoryKind.CONTRIBUTION, "PEA", [asked.id],
        )
        undo_action(session, USER, master_key, item.kind, item.id)
        [tx] = _month(session, master_key).values()
        assert tx.type_source == Source.DEFAULT


class TestSuggestedPair:
    def test_a_deposit_facing_a_suggested_pair_shows_in_its_question(self, session: Session, master_key: str):
        """Seen on the dump: the deposit went as a hint to another 20 € debit,
        two days later, while the pair's own question never showed it."""
        _ops(
            session, master_key,
            ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"),
            (CURRENT, "2026-03-07", "20.00", "DBIT", "Virement a : JEAN AUTRE"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "20")

        month = _month(session, master_key)
        pair = month["VIR Virement interne depuis BoursoBank"]
        assert (pair.transfer_status.value, pair.cashflow_type, pair.type_source) == ("suggested", Type.EXPENSE, Source.DEFAULT)
        assert (pair.contribution.account_name, pair.contribution.exact) == ("PEA", True)
        assert pair.flow_question is None
        assert month["Virement a : JEAN AUTRE"].contribution is None
        [question] = review_queue(session, USER, master_key).questions
        assert (question.kind.value, question.transaction.id) == ("transfer", pair.id)

    def test_answered_as_the_deposit_it_asks_nothing_more(self, session: Session, master_key: str):
        """What "Versement sur PEA" does: the yes alone sets the pair aside, and
        the other leg counts as any credit nothing pairs."""
        _ops(
            session, master_key,
            ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "20")
        pair = _month(session, master_key)["VIR Virement interne depuis BoursoBank"]

        _say(session, master_key, pair)

        month = _month(session, master_key)
        tx = month["VIR Virement interne depuis BoursoBank"]
        assert (tx.transfer_status, tx.cashflow_type, tx.flow_question) == (None, Type.INVESTMENT, None)
        assert (month["Paiement envoye par M. TIERS"].transfer_status, month["Paiement envoye par M. TIERS"].cashflow_type) == (
            None, Type.INCOME,
        )
        assert pair.id not in {q.transaction.id for q in review_queue(session, USER, master_key).questions}

    def test_a_pair_offered_after_the_yes_asks_nothing(self, session: Session, master_key: str):
        """Seen as a flaw: a credit of the amount booked later on another account
        offered a pair again, and the yes stopped counting meanwhile."""
        _ops(session, master_key, ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "20")
        _say(session, master_key, *_month(session, master_key).values())

        _ops(session, master_key, (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"))

        tx = _month(session, master_key)["VIR Virement interne depuis BoursoBank"]
        assert (tx.transfer_status, tx.cashflow_type, tx.type_source) == (None, Type.INVESTMENT, Source.CONTRIBUTION)

    def test_no_pair_found_on_its_own_takes_a_yes(self, session: Session, master_key: str):
        """Even one the pairing trusts: what the operation is was said."""
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (LIVRET, "2026-03-05", "200.00", "CRDT", "VIR Virement depuis compte courant"),
        )
        _pea(session, master_key)
        tx = _month(session, master_key)["VIR Virement vers PEA"]
        assert tx.transfer_status.value == "savings"
        deposit = _deposit(session, master_key, "2026-03-05", "200")

        record_contribution_decision(session, USER, master_key, tx.id, deposit.id, Said.CONTRIBUTION)

        tx = _month(session, master_key)["VIR Virement vers PEA"]
        assert (tx.transfer_status, tx.cashflow_type) == (None, Type.INVESTMENT)

    def test_once_the_deposit_moved_the_pair_comes_back(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"),
        )
        _pea(session, master_key)
        deposit = session.get(StockTransaction, _deposit(session, master_key, "2026-03-05", "20").id)
        _say(session, master_key, _month(session, master_key)["VIR Virement interne depuis BoursoBank"])

        deposit.executed_at_enc = encrypt_data("2026-03-20T10:00:00", master_key)
        session.add(deposit)
        session.commit()

        tx = _month(session, master_key)["VIR Virement interne depuis BoursoBank"]
        assert (tx.transfer_status.value, tx.type_source) == ("suggested", Source.DEFAULT)

    def test_binding_the_operation_by_hand_withdraws_its_yes(self, session: Session, master_key: str):
        """One decision per operation: the history keeps no yes that no longer counts."""
        _ops(
            session, master_key,
            ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "20")
        month = _month(session, master_key)
        pair = month["VIR Virement interne depuis BoursoBank"]
        _say(session, master_key, pair)

        record_decision(session, USER, master_key, pair.id, month["Paiement envoye par M. TIERS"].id, BankTransferDecisionKind.TRANSFER)

        tx = _month(session, master_key)["VIR Virement interne depuis BoursoBank"]
        assert (tx.transfer_status.value, tx.type_source) == ("confirmed", Source.PAIR)
        assert [item.kind for item in action_history(session, USER, master_key)] == [BankHistoryKind.TRANSFER]

    def test_a_validated_deposit_shape_waits_for_the_pair_to_be_settled(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            ("neobank", "2026-02-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            ("neobank", "2026-03-05", "20.00", "DBIT", "VIR Virement interne depuis BoursoBank"),
            (CURRENT, "2026-03-06", "20.00", "CRDT", "Paiement envoye par M. TIERS"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-05", "20")
        _deposit(session, master_key, "2026-03-05", "20")
        [first] = _month(session, master_key, "2026-02").values()
        _say(session, master_key, first)

        pair = _month(session, master_key)["VIR Virement interne depuis BoursoBank"]
        assert (pair.transfer_status.value, pair.type_source) == ("suggested", Source.DEFAULT)


class TestReviewQueue:
    def test_a_deposit_no_bank_account_paid_is_listed_newest_first(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-01", "20.00", "DBIT", "CARTE BOULANGERIE"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"),
            (CURRENT, "2026-03-31", "20.00", "DBIT", "CARTE BOULANGERIE"),
        )
        _pea(session, master_key)
        _deposit(session, master_key, "2026-02-10", "500")
        _deposit(session, master_key, "2026-03-05", "200")
        _deposit(session, master_key, "2026-03-12", "180")

        queue = review_queue(session, USER, master_key)
        assert [(u.day, u.account_name, u.amount, u.is_deposit) for u in queue.unfaced] == [
            (date(2026, 3, 12), "PEA", Decimal("180.00"), True),
            (date(2026, 2, 10), "PEA", Decimal("500.00"), True),
        ]
        assert [u.day for u in review_queue(session, USER, master_key, 2026).unfaced] == [
            date(2026, 3, 12), date(2026, 2, 10),
        ]
        assert review_queue(session, USER, master_key, 2025).unfaced == []
