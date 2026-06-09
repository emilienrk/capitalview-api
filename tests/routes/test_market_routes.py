import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from models.market import MarketAsset
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
    assert len(data) == 2
    keys = {a["asset_key"] for a in data}
    assert keys == {"ISIN_AAPL", "BTC"}
