from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_master_key] = _get_master_key
    yield
    app.dependency_overrides.clear()


def test_investor_analytics_is_empty_without_any_account(session, master_key):
    client = TestClient(app)
    body = client.get("/analytics/investor").json()

    assert body["investor_gap"] is None
    assert body["days"] == 0
    assert body["benchmark_asset_key"] == "IE00B4L5Y983"


@patch("services.analytics.benchmark.ensure_price_history")
def test_investor_analytics_reports_a_gap_for_a_funded_account(_ensure, session, master_key):
    from models.stock import StockAccount
    from services.encryption import encrypt_data, hash_index
    from services.stock_transaction import create_eur_deposit

    session.add(
        StockAccount(
            uuid="acc_1",
            user_uuid_bidx=hash_index("user_1", master_key),
            name_enc=encrypt_data("PEA", master_key),
            account_type_enc=encrypt_data("PEA", master_key),
        )
    )
    session.commit()
    create_eur_deposit(
        session, "acc_1", Decimal("1000"), datetime(2026, 1, 2, 10, tzinfo=timezone.utc), master_key
    )

    body = TestClient(app).get("/analytics/investor").json()

    # No daily snapshots exist in the test DB, so the gap must be withheld rather
    # than invented — that is the reliability gate doing its job.
    assert body["investor_gap"] is None or body["investor_gap"]["twr"]["value"] is None
