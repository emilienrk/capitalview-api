"""Tests for the wealth projection service."""

from decimal import Decimal

from sqlmodel import Session

from dtos.projection import ProjectionAssetParameters, ProjectionParameters
from models.enums import AccountCategory
from models.user import User
from services.projection import generate_wealth_projection


def _make_user() -> User:
    return User(
        uuid="user_projection",
        auth_salt="salt",
        username="proj_user",
        email="proj@example.com",
        password_hash="hash",
    )


def test_projection_zero_rate_with_injection_is_not_flagged_as_loss(
    session: Session, master_key: str
):
    """A 0% return with monthly injections is break-even, not a loss.

    The projection must keep its data points and the final total must equal
    exactly the sum of the injections reflected in the data points.
    """
    params = ProjectionParameters(
        months_to_project=12,
        assets={
            AccountCategory.STOCK: ProjectionAssetParameters(
                monthly_injection=100.0, return_rate=0.0
            ),
            AccountCategory.CRYPTO: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
            AccountCategory.BANK: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
        },
    )

    resp = generate_wealth_projection(session, _make_user(), master_key, params)

    assert len(resp.data) == 13  # month 0 .. month 12 inclusive
    assert resp.data[0].total_value == 0.0
    # 12 injections of 100 must be reflected in the final data point
    assert resp.data[-1].total_value == 1200.0


def test_projection_injections_match_data_points(session: Session, master_key: str):
    """Each month after the first must grow by exactly one injection at 0% rate."""
    params = ProjectionParameters(
        months_to_project=3,
        assets={
            AccountCategory.STOCK: ProjectionAssetParameters(
                monthly_injection=50.0, return_rate=0.0
            ),
            AccountCategory.CRYPTO: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
            AccountCategory.BANK: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
        },
    )

    resp = generate_wealth_projection(session, _make_user(), master_key, params)

    totals = [point.total_value for point in resp.data]
    assert totals == [0.0, 50.0, 100.0, 150.0]


def test_projection_negative_rate_returns_empty(session: Session, master_key: str):
    """A clearly losing projection must return an empty data array."""
    params = ProjectionParameters(
        months_to_project=12,
        assets={
            AccountCategory.STOCK: ProjectionAssetParameters(
                monthly_injection=100.0, return_rate=-0.5
            ),
            AccountCategory.CRYPTO: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
            AccountCategory.BANK: ProjectionAssetParameters(
                monthly_injection=0.0, return_rate=0.0
            ),
        },
    )

    resp = generate_wealth_projection(session, _make_user(), master_key, params)

    assert resp.data == []


def test_defaults_come_from_the_measured_basis(session: Session, master_key: str):
    """The service no longer invents its own rate from value over cost.

    An account with nothing to measure projects flat: no contribution, no
    return. The previous shortcut would have extrapolated whatever ratio the
    cost basis happened to produce, which is what made the web app and an agent
    disagree about the same portfolio.
    """
    params = ProjectionParameters(months_to_project=12)

    response = generate_wealth_projection(session, _make_user(), master_key, params)

    used = response.parameters_used.assets
    assert used[AccountCategory.STOCK].monthly_injection == 0
    assert used[AccountCategory.STOCK].return_rate == 0.0
    assert used[AccountCategory.CRYPTO].return_rate == 0.0
    # The bank starts from its real balance now, so a default rate would pay
    # interest on the current account: without a declared rate it earns nothing.
    bank = used[AccountCategory.BANK]
    assert bank.return_rate == 0.0
    assert {w.code for w in bank.basis.warnings} == {"short_cashflow_history", "no_declared_rate"}


def test_the_curve_starts_from_the_whole_net_worth(session: Session, master_key: str):
    """The bank balances and the possessions are part of today's value: a curve
    starting from the investments alone read as a net worth half the real one."""
    from unittest.mock import patch

    from dtos.asset import AssetCreate
    from datetime import date

    from dtos.bank import BankAccountCreate, BankEntryRequest
    from models.bank import BankAccount
    from models.enums import BankAccountType
    from services.asset import create_asset
    from services.bank import create_bank_account
    from services.bank_ledger import add_entry

    user = _make_user()
    with patch("services.bank.has_exchange_rate", return_value=True):
        created = create_bank_account(
            session,
            BankAccountCreate(name="Courant", account_type=BankAccountType.CHECKING),
            user.uuid, master_key,
        )
    add_entry(
        session, session.get(BankAccount, created.id),
        BankEntryRequest(day=date(2026, 1, 2), amount=Decimal("3000")), master_key,
    )
    create_asset(
        session, AssetCreate(name="Montre", category="Bijoux", estimated_value=Decimal("500")), user.uuid, master_key
    )

    response = generate_wealth_projection(session, user, master_key, ProjectionParameters(months_to_project=12))

    start, end = response.data[0], response.data[-1]
    assert start.asset_values[AccountCategory.BANK] == 3000.0
    assert start.asset_values[AccountCategory.ASSET] == 500.0
    assert start.total_value == 3500.0
    # Nothing measured, nothing declared: both stay where they are.
    assert end.total_value == 3500.0


def test_the_bank_surplus_is_the_median_month_left_after_spending_and_investing(
    session: Session, master_key: str, monkeypatch
):
    """Money sent to the investment accounts is their contribution already;
    money set aside on a livret stays in the bank."""
    from dtos.banking import RealCashflowMonth
    from services.analytics.projection_basis import _bank_basis

    months = [
        RealCashflowMonth(period=f"2026-{m:02d}", operation_count=10, income=Decimal("2500"),
                          expenses=Decimal(expenses), saving=Decimal("300"), investment=Decimal("400"))
        for m, expenses in enumerate(("1500", "1600", "1400", "1500", "3000", "1500"), start=1)
    ]

    class _Recent:
        pass

    recent = _Recent()
    recent.months = months
    monkeypatch.setattr(
        "services.banking.real_cashflow.real_cashflow_recent", lambda *a, **k: recent
    )

    basis = _bank_basis(session, "nobody", master_key)

    # 2500 - 1500 - 400 = 600 on the median month; the 3 000 € month does not set it.
    assert basis.monthly_contribution == Decimal("600")
    assert basis.contribution_source == "real_cashflow"
    assert basis.contribution_months == 6


def test_the_bank_surplus_waits_for_six_months_of_operations(session: Session, master_key: str, monkeypatch):
    from dtos.banking import RealCashflowMonth
    from services.analytics.projection_basis import _bank_basis

    class _Recent:
        months = [RealCashflowMonth(period="2026-01", operation_count=3, income=Decimal("2000"))]

    monkeypatch.setattr("services.banking.real_cashflow.real_cashflow_recent", lambda *a, **k: _Recent())

    basis = _bank_basis(session, "nobody", master_key)

    assert basis.monthly_contribution is None
    assert [w.code for w in basis.warnings][0] == "short_cashflow_history"


def test_a_supplied_basis_is_used_instead_of_being_measured_again(
    session: Session, master_key: str
):
    """Deriving reads every transaction and snapshot; callers may pass theirs."""
    from services.analytics.projection_basis import CategoryBasis

    supplied = CategoryBasis(
        monthly_contribution=Decimal("250"), annual_return_rate=Decimal("0.07")
    )
    params = ProjectionParameters(months_to_project=12)

    response = generate_wealth_projection(
        session,
        _make_user(),
        master_key,
        params,
        basis={"STOCK": supplied, "CRYPTO": CategoryBasis(), "BANK": CategoryBasis()},
    )

    used = response.parameters_used.assets[AccountCategory.STOCK]
    assert used.monthly_injection == 250
    assert used.return_rate == 0.07
