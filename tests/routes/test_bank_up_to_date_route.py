"""POST /bank/accounts/{id}/up-to-date: the user's word that nothing more happened."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from main import app
from models import BankAccount
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x"
    )
    app.dependency_overrides[get_master_key] = lambda: master_key
    yield
    app.dependency_overrides.clear()


def test_confirming_an_account_stamps_today(session):
    client = TestClient(app)
    account_id = client.post(
        "/bank/accounts", json={"name": "Plaisir", "account_type": "CHECKING", "balance": "0"}
    ).json()["id"]

    assert client.post(f"/bank/accounts/{account_id}/up-to-date").status_code == 204

    session.expire_all()
    assert session.get(BankAccount, account_id).history_confirmed_on == date.today()


def test_an_unknown_account_is_not_found():
    assert TestClient(app).post("/bank/accounts/nope/up-to-date").status_code == 404
