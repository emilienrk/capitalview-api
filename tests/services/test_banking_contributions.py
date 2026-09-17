"""
What the user's investment accounts prove about a transfer sent
(services/banking/contributions.py), and what it types
(services/banking/cashflow_types.py).

Labels are shaped like the real ones, names replaced.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlmodel import Session

from dtos.banking import CashflowType as Type, TypeScope, TypeSource as Source
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
)
from services.banking.flows import clear_transaction_type, set_transaction_type
from services.banking.real_cashflow import real_cashflow_month
from services.crypto_transaction import create_composite_crypto_transaction
from services.encryption import encrypt_data, hash_index
from services.stock_transaction import create_eur_deposit
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, TODAY, _month, _ops

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
    for day, amount in items:
        key = (True, Decimal(amount))
        by_amount.setdefault(key, []).append(
            Contribution("PEA", date.fromisoformat(day), Decimal(amount), is_deposit=True)
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

    def test_four_days_apart_says_nothing(self):
        candidates = [Candidate(0, date(2026, 3, 10), Decimal("200"), False)]
        assert match_candidates(candidates, _contributions(("2026-03-06", "200"))) == {}

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

    def test_another_amount_is_never_matched(self):
        candidates = [Candidate(0, date(2026, 3, 5), Decimal("200"), False)]
        assert match_candidates(candidates, _contributions(("2026-03-05", "199.99"))) == {}


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

class TestOperations:
    def test_a_transfer_sent_the_day_of_a_deposit_is_an_investment_and_asks_nothing(
        self, session: Session, master_key: str
    ):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)
        assert tx.flow_question is None
        assert (tx.contribution.account_name, tx.contribution.exact) == ("PEA", True)

    def test_a_deposit_two_days_later_only_shows_beside_the_question(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-07", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.flow_question is not None
        assert (tx.contribution.day, tx.contribution.exact) == (date(2026, 3, 7), False)

    def test_a_card_payment_is_never_deduced(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE 04/03/26 DARTY CB*08"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.EXPENSE, Source.DEFAULT)
        assert tx.contribution is None

    def test_the_user_corrects_a_deduction_and_takes_it_back(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")
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

    def test_a_credit_the_day_of_a_withdrawal_is_investment_taken_back(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "80.00", "CRDT", "VIR Virement depuis PEA"))
        _pea(session, master_key)
        _withdraw(session, master_key, "2026-03-05", "80")

        [tx] = _month(session, master_key).values()
        assert (tx.cashflow_type, tx.type_source) == (Type.INVESTMENT, Source.CONTRIBUTION)

    def test_the_month_counts_it_as_invested(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement vers PEA"))
        _pea(session, master_key)
        _deposit(session, master_key, "2026-03-05", "200")

        month = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY)
        assert (month.totals.investment, month.totals.expenses) == (Decimal("200"), Decimal("0"))
