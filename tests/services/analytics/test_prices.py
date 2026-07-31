from datetime import date, timedelta
from decimal import Decimal

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.analytics.prices import fill_price_gaps, get_price_matrix

D = date(2026, 1, 5)


def _days(*offsets: int) -> list[date]:
    return [D + timedelta(days=n) for n in offsets]


def _seed(session, asset_key: str, prices: dict[date, str]) -> None:
    asset = MarketAsset(
        asset_key=asset_key, symbol=asset_key, name=asset_key, asset_type=AssetType.STOCK
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)
    for day, price in prices.items():
        session.add(
            MarketPriceHistory(market_asset_id=asset.id, price=Decimal(price), price_date=day)
        )
    session.commit()


def test_matrix_is_sparse_and_keyed_by_asset_then_date(session):
    _seed(session, "AAA", {D: "100", D + timedelta(days=2): "102"})

    matrix = get_price_matrix(session, ["AAA"], D, D + timedelta(days=3))

    assert matrix["AAA"][D] == Decimal("100")
    assert matrix["AAA"][D + timedelta(days=2)] == Decimal("102")
    # The raw matrix is deliberately sparse — gap filling is a separate step.
    assert D + timedelta(days=1) not in matrix["AAA"]


def test_matrix_covers_several_assets_in_one_query(session):
    _seed(session, "AAA", {D: "100"})
    _seed(session, "BBB", {D: "50"})

    matrix = get_price_matrix(session, ["AAA", "BBB"], D, D)

    assert matrix["AAA"][D] == Decimal("100")
    assert matrix["BBB"][D] == Decimal("50")


def test_matrix_ignores_rows_outside_the_range(session):
    _seed(session, "AAA", {D - timedelta(days=5): "90", D: "100"})

    matrix = get_price_matrix(session, ["AAA"], D, D)

    assert list(matrix["AAA"]) == [D]


def test_no_asset_keys_yields_an_empty_matrix(session):
    assert get_price_matrix(session, [], D, D) == {}


def test_gaps_are_forward_filled(session):
    _seed(session, "AAA", {D: "100", D + timedelta(days=2): "102"})
    days = _days(0, 1, 2, 3)

    filled = fill_price_gaps(get_price_matrix(session, ["AAA"], days[0], days[-1]), ["AAA"], days, session)

    assert [filled["AAA"][d] for d in days] == [
        Decimal("100"),
        Decimal("100"),
        Decimal("102"),
        Decimal("102"),
    ]


def test_a_window_opening_on_a_closed_day_is_seeded_from_the_last_prior_quote(session):
    """The safety net that makes an arbitrary window start usable.

    Analysis windows are derived from user data, so they almost never land on a
    trading day. Without this seed the first days would stay empty.
    """
    _seed(session, "AAA", {D - timedelta(days=3): "97", D + timedelta(days=1): "101"})
    days = _days(0, 1)

    filled = fill_price_gaps(get_price_matrix(session, ["AAA"], days[0], days[-1]), ["AAA"], days, session)

    assert filled["AAA"][days[0]] == Decimal("97")
    assert filled["AAA"][days[1]] == Decimal("101")


def test_an_asset_with_no_price_at_all_stays_absent_rather_than_zero(session):
    days = _days(0, 1)

    filled = fill_price_gaps(get_price_matrix(session, ["NOPE"], days[0], days[-1]), ["NOPE"], days, session)

    # A missing price must never become a zero price — callers decide what to do.
    assert filled["NOPE"] == {}


def test_no_days_requested_returns_the_matrix_untouched(session):
    _seed(session, "AAA", {D: "100"})
    matrix = get_price_matrix(session, ["AAA"], D, D)

    assert fill_price_gaps(matrix, ["AAA"], [], session) is matrix
