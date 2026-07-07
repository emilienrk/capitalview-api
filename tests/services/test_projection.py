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


def test_compute_defaults_bounds_cagr_and_average_injection():
    """CAGR must be clamped to [-0.99, 2] and the default injection must be
    the average of invested capital over the elapsed months."""
    from services.projection import _compute_defaults

    # Explosive growth over a short period → CAGR clamped to the 2.0 ceiling.
    injection, rate = _compute_defaults(value=Decimal("100000"), invested=Decimal("1000"), days=30)
    assert rate == 2.0
    assert injection == Decimal("1000")  # invested / max(30/30.41, 1.0) == invested / 1.0

    # Near-total loss over a year → CAGR clamped to the -0.99 floor.
    _, rate_loss = _compute_defaults(value=Decimal("1"), invested=Decimal("100000"), days=365)
    assert rate_loss == -0.99

    # No history at all → no default injection, no default rate.
    injection_zero, rate_zero = _compute_defaults(value=Decimal("0"), invested=Decimal("0"), days=0)
    assert injection_zero == Decimal("0")
    assert rate_zero == 0.0
