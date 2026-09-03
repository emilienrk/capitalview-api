"""Cross-domain read models: the figures every consumer reads."""

import uuid as uuid_lib
from decimal import Decimal

import pytest

from dtos.transaction import AccountSummaryResponse, PositionResponse
from models.stock import StockAccount
from services import overview
from services.encryption import encrypt_data, hash_index


@pytest.fixture(name="stock_account")
def stock_account_fixture(session, master_key) -> str:
    """A stock account holding one line plus idle cash."""
    user_uuid = str(uuid_lib.uuid4())
    session.add(
        StockAccount(
            uuid=str(uuid_lib.uuid4()),
            user_uuid_bidx=hash_index(user_uuid, master_key),
            name_enc=encrypt_data("PEA", master_key),
            account_type_enc=encrypt_data("PEA", master_key),
        )
    )
    session.commit()
    return user_uuid


def _summary(profit_loss: Decimal | None) -> AccountSummaryResponse:
    """An account worth 1 500 € in holdings, sitting on 3 801 € of idle cash."""
    return AccountSummaryResponse(
        total_invested=Decimal("1282.40"),
        total_fees=Decimal("3"),
        current_value=Decimal("1500"),
        cash_balance=Decimal("3801"),
        profit_loss=profit_loss,
        positions=[
            PositionResponse(
                symbol="IWDA",
                asset_key="IE00B4L5Y983",
                total_amount=Decimal("12"),
                average_buy_price=Decimal("106.8667"),
                total_invested=Decimal("1282.40"),
                total_fees=Decimal("3"),
                fees_percentage=Decimal("0.23"),
                current_value=Decimal("1500"),
                profit_loss=profit_loss,
            )
        ],
    )


@pytest.fixture(autouse=True)
def _stub_stock_summary(monkeypatch):
    monkeypatch.setattr(overview, "get_stock_transactions", lambda *a, **k: [])
    monkeypatch.setattr(overview, "get_crypto_transactions", lambda *a, **k: [])


def test_idle_cash_is_not_counted_as_a_gain(session, master_key, stock_account, monkeypatch):
    """The per-type totals fold in each account's cash balance.

    Deriving the gain as "value minus cost" against those totals reported an
    untouched cash balance as profit: 5 301 − 1 282,40 = 4 018,60 € of claimed
    gain on a line that is actually up 217,60 €.
    """
    monkeypatch.setattr(
        overview, "get_stock_account_summary", lambda *a, **k: _summary(Decimal("217.60"))
    )

    balance = overview.get_user_balance(session, stock_account, master_key)

    assert balance["unrealized_profit_loss"] == 217.60
    assert balance["invested_total"] == 1282.40
    # The account's worth still includes its cash — only the gain excludes it.
    assert balance["stocks_total"] == 5301.0


def test_an_unpriced_portfolio_reports_no_gain_rather_than_a_flat_one(
    session, master_key, stock_account, monkeypatch
):
    """Zero would read as 'you are exactly break-even', which is a different claim."""
    monkeypatch.setattr(overview, "get_stock_account_summary", lambda *a, **k: _summary(None))

    balance = overview.get_user_balance(session, stock_account, master_key)

    assert balance["unrealized_profit_loss"] is None


def test_positions_carry_what_they_cost_next_to_what_they_are_worth(
    session, master_key, stock_account, monkeypatch
):
    monkeypatch.setattr(
        overview, "get_stock_account_summary", lambda *a, **k: _summary(Decimal("217.60"))
    )

    balance = overview.get_user_balance(session, stock_account, master_key, details=True)

    position = balance["stock_accounts_details"][0]["positions"][0]
    assert position["total_invested"] == 1282.40
    assert position["current_value"] == 1500.0
    assert position["profit_loss"] == 217.60
    assert position["average_buy_price"] == 106.8667


class TestCashflowDirectionFilter:
    """`get_user_cashflow(flow_type=…)` is what the in-app assistant and the MCP
    server call to answer "how much do I spend". Both speak lowercase; FlowType's
    values are uppercase. Lowercasing raised on every call, the filter silently
    did nothing, and asking for spending alone answered with everything."""

    def _two_flows(self, session, master_key, user_uuid):
        from datetime import date

        from dtos.cashflow import CashflowCreate
        from models.enums import FlowType, Frequency
        from services.cashflow import create_cashflow

        for name, flow_type, amount in (
            ("Salaire", FlowType.INFLOW, "2000"),
            ("Loyer", FlowType.OUTFLOW, "800"),
        ):
            create_cashflow(
                session,
                CashflowCreate(
                    name=name,
                    flow_type=flow_type,
                    category="test",
                    amount=Decimal(amount),
                    frequency=Frequency.MONTHLY,
                    transaction_date=date(2026, 1, 1),
                ),
                user_uuid,
                master_key,
            )

    @pytest.mark.parametrize(
        "flow_type,kept,dropped",
        [("outflow", "outflow", "inflow"), ("inflow", "inflow", "outflow")],
    )
    def test_one_direction_returns_only_that_direction(
        self, session, master_key, flow_type, kept, dropped
    ):
        user_uuid = str(uuid_lib.uuid4())
        self._two_flows(session, master_key, user_uuid)

        result = overview.get_user_cashflow(session, user_uuid, master_key, flow_type=flow_type)

        assert kept in result
        assert dropped not in result
        # The balance keys belong to the unfiltered answer only.
        assert "savings_rate" not in result

    def test_no_direction_returns_both_and_the_balance(self, session, master_key):
        user_uuid = str(uuid_lib.uuid4())
        self._two_flows(session, master_key, user_uuid)

        result = overview.get_user_cashflow(session, user_uuid, master_key)

        assert {"inflow", "outflow", "balance", "savings_rate"} <= set(result)


class TestBalanceAtADate:
    """`get_user_balance(date=…)` reaches `get_all_bank_accounts_snapshot_for_date`,
    which answers with a dict while the undated branch answers with a
    `BankSummaryResponse`. Attribute access on the dict raised for every user who
    owned a bank account — reachable from the MCP `get_portfolio_overview(date=…)`."""

    def _bank_account(self, session, master_key, user_uuid):
        from unittest.mock import patch

        from dtos.bank import BankAccountCreate
        from models.enums import BankAccountType
        from services.bank import create_bank_account

        with patch("services.bank.has_exchange_rate", return_value=True):
            return create_bank_account(
                session,
                BankAccountCreate(
                    name="Compte courant",
                    balance=Decimal("300"),
                    account_type=BankAccountType.CHECKING,
                    currency="EUR",
                ),
                user_uuid,
                master_key,
            )

    def test_a_dated_balance_does_not_raise_when_an_account_exists(self, session, master_key):
        from datetime import date

        user_uuid = str(uuid_lib.uuid4())
        self._bank_account(session, master_key, user_uuid)

        result = overview.get_user_balance(
            session, user_uuid, master_key, date=date(2026, 1, 15).isoformat()
        )

        assert "cash_total" in result

    def test_a_dated_balance_with_details_names_the_account(self, session, master_key):
        """`getattr` on a dict answered None for every field, so the detail rows
        came back nameless and at zero."""
        from datetime import date

        user_uuid = str(uuid_lib.uuid4())
        self._bank_account(session, master_key, user_uuid)

        result = overview.get_user_balance(
            session, user_uuid, master_key, details=True, date=date(2026, 1, 15).isoformat()
        )

        rows = result["bank_accounts_details"]
        assert [row["name"] for row in rows] == ["Compte courant"]

    def test_the_undated_details_path_still_names_the_account(self, session, master_key):
        """The other branch answers with `BankAccountResponse` objects, not dicts."""
        user_uuid = str(uuid_lib.uuid4())
        self._bank_account(session, master_key, user_uuid)

        result = overview.get_user_balance(session, user_uuid, master_key, details=True)

        rows = result["bank_accounts_details"]
        assert [row["name"] for row in rows] == ["Compte courant"]
        assert [row["balance"] for row in rows] == [300.0]
