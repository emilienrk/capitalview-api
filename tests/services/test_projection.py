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
    # The bank keeps its own conservative default, untouched by the change.
    assert used[AccountCategory.BANK].return_rate == 0.02


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
