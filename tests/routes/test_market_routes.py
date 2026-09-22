from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from models.market import MarketAsset, MarketPriceHistory
from models.enums import AssetType


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    """Override FastAPI dependencies to use the test DB session and a fake user/master key."""
    def _get_session():
        return session

    def _get_user():
        return User(uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    try:
        from services.auth import get_current_user, get_master_key
        app.dependency_overrides[get_current_user] = _get_user
        app.dependency_overrides[get_master_key] = _get_master_key
    except Exception:
        pass

    yield

    app.dependency_overrides.clear()


def test_get_market_assets_route(session):
    client = TestClient(app)

    # Setup some assets in DB
    ma1 = MarketAsset(asset_key="ISIN_AAPL", symbol="AAPL", name="Apple", asset_type=AssetType.STOCK)
    ma2 = MarketAsset(asset_key="BTC", symbol="BTC", name="Bitcoin", asset_type=AssetType.CRYPTO)
    ma3 = MarketAsset(asset_key="EUR", symbol="EUR", name="Euro", asset_type=AssetType.FIAT)
    session.add(ma1)
    session.add(ma2)
    session.add(ma3)
    session.commit()

    # Query route
    resp = client.get("/market/assets")
    assert resp.status_code == 200
    data = resp.json()
    # Since only_owned=True, and no stock/crypto accounts/transactions were created, 
    # the user does not own any of the created assets.
    assert len(data) == 0


def _seed_apple_prices(session, closes: dict) -> MarketAsset:
    """Register Apple with a handful of daily closes, in EUR as the table stores them."""
    asset = MarketAsset(
        asset_key="US0378331005", symbol="AAPL", name="Apple", asset_type=AssetType.STOCK
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)

    for day, price in closes.items():
        session.add(
            MarketPriceHistory(market_asset_id=asset.id, price=price, price_date=day)
        )
    session.commit()
    return asset


def _open_account(session, master_key: str) -> str:
    from dtos.stock import StockAccountCreate
    from services.stock_account import create_stock_account

    account = create_stock_account(
        session,
        StockAccountCreate(name="CTO Timeline", account_type="CTO"),
        "user_1",
        master_key,
    )
    return account.id


def _record(session, master_key: str, account_id: str, **fields) -> None:
    from dtos.stock import StockTransactionCreate
    from services.stock_transaction import create_stock_transaction

    create_stock_transaction(
        session,
        StockTransactionCreate(account_id=account_id, asset_key="US0378331005", **fields),
        master_key,
    )


@patch("services.market.ensure_price_history")
def test_asset_price_timeline_reports_running_cost_basis(_ensure, session, master_key):
    """Each trade carries the unit cost held right after it, average-cost style."""
    _seed_apple_prices(
        session,
        {
            date(2024, 1, 10): Decimal("100"),
            date(2024, 2, 10): Decimal("180"),
            date(2024, 3, 10): Decimal("250"),
        },
    )
    account_id = _open_account(session, master_key)
    _record(session, master_key, account_id, type="BUY", amount="2", price_per_unit="100",
            fees="0", executed_at="2024-01-10T12:00:00")
    _record(session, master_key, account_id, type="BUY", amount="2", price_per_unit="200",
            fees="0", executed_at="2024-02-10T12:00:00")
    _record(session, master_key, account_id, type="SELL", amount="2", price_per_unit="250",
            fees="0", executed_at="2024-03-10T12:00:00")

    resp = TestClient(app).get("/market/assets/US0378331005/price-timeline")
    assert resp.status_code == 200
    data = resp.json()

    assert [e["type"] for e in data["events"]] == ["BUY", "BUY", "SELL"]
    # 2 @ 100 → 100; then 2 @ 200 averages to 150; a sale takes cost away in
    # proportion to the quantity it removes, so the unit cost does not move.
    assert [Decimal(e["cost_basis_after"]) for e in data["events"]] == [
        Decimal("100"), Decimal("150"), Decimal("150"),
    ]
    # Money out is negative, money in positive, so the signs read as cash flow.
    assert [Decimal(e["total"]) for e in data["events"]] == [
        Decimal("-200"), Decimal("-400"), Decimal("500"),
    ]
    assert Decimal(data["average_buy_price"]) == Decimal("150")
    assert Decimal(data["quantity_held"]) == Decimal("2")
    assert data["currency"] == "EUR"


@patch("services.market.ensure_price_history")
def test_asset_price_timeline_curve_starts_at_first_trade(_ensure, session, master_key):
    """Closes quoted before the user ever owned the asset are not their history."""
    _seed_apple_prices(
        session,
        {
            date(2023, 6, 1): Decimal("80"),
            date(2024, 1, 10): Decimal("100"),
            date(2024, 2, 10): Decimal("180"),
        },
    )
    account_id = _open_account(session, master_key)
    _record(session, master_key, account_id, type="BUY", amount="1", price_per_unit="100",
            fees="0", executed_at="2024-01-10T12:00:00")

    data = TestClient(app).get("/market/assets/US0378331005/price-timeline").json()

    assert [p["date"] for p in data["points"]] == ["2024-01-10", "2024-02-10"]


@patch("services.market.ensure_price_history")
def test_asset_price_timeline_puts_income_on_the_curve(_ensure, session, master_key):
    """A dividend has no price of its own, so its marker rides the price line."""
    _seed_apple_prices(
        session,
        {
            date(2024, 1, 10): Decimal("100"),
            date(2024, 2, 10): Decimal("180"),
        },
    )
    account_id = _open_account(session, master_key)
    _record(session, master_key, account_id, type="BUY", amount="10", price_per_unit="100",
            fees="0", executed_at="2024-01-10T12:00:00")
    _record(session, master_key, account_id, type="DIVIDEND", amount="10", price_per_unit="1.5",
            fees="0", executed_at="2024-02-10T12:00:00")

    data = TestClient(app).get("/market/assets/US0378331005/price-timeline").json()

    dividend = next(e for e in data["events"] if e["type"] == "INCOME")
    assert Decimal(dividend["price"]) == Decimal("180")
    assert Decimal(dividend["total"]) == Decimal("15")
    # Cash income leaves the position untouched.
    assert Decimal(data["quantity_held"]) == Decimal("10")


@patch("services.market.ensure_price_history")
def test_asset_price_timeline_falls_back_to_last_close(_ensure, session, master_key):
    """A trade on a closed market takes the last quote, not a hole in the data."""
    _seed_apple_prices(session, {date(2024, 1, 10): Decimal("100")})
    account_id = _open_account(session, master_key)
    _record(session, master_key, account_id, type="BUY", amount="10", price_per_unit="100",
            fees="0", executed_at="2024-01-10T12:00:00")
    # 2024-01-13 is a Saturday: no close of its own.
    _record(session, master_key, account_id, type="DIVIDEND", amount="10", price_per_unit="2",
            fees="0", executed_at="2024-01-13T12:00:00")

    data = TestClient(app).get("/market/assets/US0378331005/price-timeline").json()

    dividend = next(e for e in data["events"] if e["type"] == "INCOME")
    assert Decimal(dividend["price"]) == Decimal("100")


def test_asset_price_timeline_unknown_asset_is_404(session):
    resp = TestClient(app).get("/market/assets/NOPE/price-timeline")
    assert resp.status_code == 404


def _open_wallet(client: TestClient) -> str:
    resp = client.post("/crypto/accounts", json={"name": "Wallet Timeline"})
    assert resp.status_code == 201
    return resp.json()["id"]


def _bulk(client: TestClient, account_id: str, rows: list[dict]) -> None:
    resp = client.post(
        "/crypto/transactions/bulk", json={"account_id": account_id, "transactions": rows}
    )
    assert resp.status_code == 201, resp.text


def _seed_btc_prices(session, closes: dict) -> MarketAsset:
    asset = MarketAsset(asset_key="BTC", symbol="BTC", name="Bitcoin", asset_type=AssetType.CRYPTO)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    for day, price in closes.items():
        session.add(MarketPriceHistory(market_asset_id=asset.id, price=price, price_date=day))
    session.commit()
    return asset


@patch("services.market.ensure_price_history")
def test_crypto_timeline_prices_a_buy_from_its_group(_ensure, session, master_key):
    """A crypto BUY carries no price: the euros are on the group's other rows.

    Reproduces the shape the app actually stores — a crypto-to-crypto trade
    whose cost lives on an ANCHOR booked under EUR, and a euro purchase whose
    cost lives on a fiat SPEND. Reading price_per_unit off the BUY row gives 0
    and plots every marker on the zero line.
    """
    _seed_btc_prices(session, {date(2024, 1, 1): Decimal("30000")})
    client = TestClient(app)
    account_id = _open_wallet(client)

    _bulk(client, account_id, [
        # Bought with euros: the fiat SPEND carries the cost.
        {"asset_key": "BTC", "type": "BUY", "amount": "0.1", "price_per_unit": "0",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        {"asset_key": "EUR", "type": "SPEND", "amount": "3000", "price_per_unit": "1",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        # Bought with USDC: no fiat leg at all, the ANCHOR carries the cost.
        {"asset_key": "BTC", "type": "BUY", "amount": "0.1", "price_per_unit": "0",
         "executed_at": "2024-02-01T12:00:00", "group_uuid": "g2"},
        {"asset_key": "USDC", "type": "SPEND", "amount": "100", "price_per_unit": "1",
         "executed_at": "2024-02-01T12:00:00", "group_uuid": "g2"},
        {"asset_key": "EUR", "type": "ANCHOR", "amount": "1000", "price_per_unit": "1",
         "executed_at": "2024-02-01T12:00:00", "group_uuid": "g2"},
    ])

    data = client.get(f"/market/assets/BTC/price-timeline?account_id={account_id}").json()

    assert [e["type"] for e in data["events"]] == ["BUY", "BUY"]
    assert [Decimal(e["price"]) for e in data["events"]] == [
        Decimal("30000"), Decimal("10000"),
    ]
    assert [Decimal(e["total"]) for e in data["events"]] == [
        Decimal("-3000"), Decimal("-1000"),
    ]
    # 3000 then 4000 spread over 0.2 — never zero, which is the bug this covers.
    assert [Decimal(e["cost_basis_after"]) for e in data["events"]] == [
        Decimal("30000"), Decimal("20000"),
    ]
    assert Decimal(data["average_buy_price"]) == Decimal("20000")


@patch("services.market.ensure_price_history")
def test_crypto_timeline_prices_a_disposal_from_its_group(_ensure, session, master_key):
    """A SPEND of the asset is a sale priced by the euros the group brought in."""
    _seed_btc_prices(session, {date(2024, 1, 1): Decimal("30000")})
    client = TestClient(app)
    account_id = _open_wallet(client)

    _bulk(client, account_id, [
        {"asset_key": "BTC", "type": "BUY", "amount": "0.2", "price_per_unit": "0",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        {"asset_key": "EUR", "type": "SPEND", "amount": "4000", "price_per_unit": "1",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        {"asset_key": "BTC", "type": "SPEND", "amount": "0.1", "price_per_unit": "0",
         "executed_at": "2024-03-01T12:00:00", "group_uuid": "g3"},
        {"asset_key": "EUR", "type": "DEPOSIT", "amount": "2500", "price_per_unit": "1",
         "executed_at": "2024-03-01T12:00:00", "group_uuid": "g3"},
    ])

    data = client.get(f"/market/assets/BTC/price-timeline?account_id={account_id}").json()

    sale = next(e for e in data["events"] if e["type"] == "SELL")
    assert Decimal(sale["price"]) == Decimal("25000")
    assert Decimal(sale["total"]) == Decimal("2500")
    # Half the quantity leaves, so half the cost goes with it and the unit cost holds.
    assert Decimal(sale["cost_basis_after"]) == Decimal("20000")


@patch("services.market.ensure_price_history")
def test_crypto_timeline_fee_in_kind_raises_the_unit_cost(_ensure, session, master_key):
    """A fee paid in the asset takes quantity without refunding any cost."""
    _seed_btc_prices(session, {date(2024, 1, 1): Decimal("30000")})
    client = TestClient(app)
    account_id = _open_wallet(client)

    _bulk(client, account_id, [
        {"asset_key": "BTC", "type": "BUY", "amount": "0.1", "price_per_unit": "0",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        {"asset_key": "EUR", "type": "SPEND", "amount": "2000", "price_per_unit": "1",
         "executed_at": "2024-01-01T12:00:00", "group_uuid": "g1"},
        {"asset_key": "BTC", "type": "FEE", "amount": "0.02", "price_per_unit": "30000",
         "executed_at": "2024-01-02T12:00:00"},
    ])

    data = client.get(f"/market/assets/BTC/price-timeline?account_id={account_id}").json()

    # The fee itself is not a decision, so it carries no marker...
    assert [e["type"] for e in data["events"]] == ["BUY"]
    # ...but 2000 € now sits on 0.08 BTC instead of 0.1.
    assert Decimal(data["quantity_held"]) == Decimal("0.08")
    assert Decimal(data["average_buy_price"]) == Decimal("25000")


def test_asset_price_timeline_refuses_a_currency(session):
    """EUR is what the curve is priced in, not a position with a curve."""
    resp = TestClient(app).get("/market/assets/EUR/price-timeline")
    assert resp.status_code == 404
