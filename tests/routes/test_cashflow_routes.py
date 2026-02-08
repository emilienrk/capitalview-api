from decimal import Decimal

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
    app.dependency_overrides[get_session] = _get_session
    try:
        from services.auth import get_current_user, get_master_key
        app.dependency_overrides[get_current_user] = _get_user
        app.dependency_overrides[get_master_key] = _get_master_key
    except Exception:
        pass

    yield

    app.dependency_overrides.clear()


def test_cashflow_crud_and_summary(session, master_key):
    client = TestClient(app)

    payload = {
        "name": "Salary Jan",
        "flow_type": "INFLOW",
        "category": "Salary",
        "amount": "1000",
        "frequency": "ONCE",
        "transaction_date": "2023-01-01",
    }
    r = client.post("/cashflow", json=payload)
    assert r.status_code == 201
    created = r.json()
    cf_id = created["id"]

    r2 = client.get("/cashflow")
    assert r2.status_code == 200
    assert any(c["id"] == cf_id for c in r2.json())

    r3 = client.get("/cashflow/me/inflows")
    assert r3.status_code == 200

    r4 = client.get("/cashflow/me/balance")
    assert r4.status_code == 200

    r5 = client.put(f"/cashflow/{cf_id}", json={"category": "Updated"})
    assert r5.status_code == 200

    r6 = client.delete(f"/cashflow/{cf_id}")
    assert r6.status_code == 204
