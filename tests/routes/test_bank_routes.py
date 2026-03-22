import uuid as uuid_mod
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import select

from main import app
from models.account_history import AccountHistory
from models.bank import BankAccount
from models.enums import AccountCategory
from models.user import User
from services.encryption import encrypt_data, hash_index


@pytest.fixture(autouse=True)
def _sqlite_pg_insert(monkeypatch):
    """Replace pg_insert with a plain SA insert so history import works on SQLite."""
    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


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


def test_bank_crud(session, master_key):
    client = TestClient(app)

    payload = {"name": "Main", "account_type": "CHECKING", "institution_name": "MyBank"}
    r = client.post("/bank/accounts", json=payload)
    assert r.status_code == 201
    acc = r.json()
    account_id = acc["id"]

    r2 = client.get(f"/bank/accounts/{account_id}")
    assert r2.status_code == 200

    r3 = client.put(f"/bank/accounts/{account_id}", json={"name": "Main Updated"})
    assert r3.status_code == 200
    assert r3.json()["name"] == "Main Updated"

    r4 = client.delete(f"/bank/accounts/{account_id}")
    assert r4.status_code == 204


def test_delete_account_history(session, master_key):
    """DELETE /bank/accounts/{id}/history removes all snapshots for the account."""
    client = TestClient(app)

    r = client.post("/bank/accounts", json={"name": "Hist", "account_type": "CHECKING"})
    assert r.status_code == 201
    account_id = r.json()["id"]

    account_id_bidx = hash_index(account_id, master_key)
    user_bidx = hash_index("user_1", master_key)
    for d in [date(2025, 1, 1), date(2025, 1, 2)]:
        session.add(AccountHistory(
            uuid=str(uuid_mod.uuid4()),
            user_uuid_bidx=user_bidx,
            account_id_bidx=account_id_bidx,
            account_type=AccountCategory.BANK,
            snapshot_date=d,
            total_value_enc=encrypt_data("100", master_key),
            total_invested_enc=encrypt_data("100", master_key),
        ))
    session.commit()

    r_del = client.delete(f"/bank/accounts/{account_id}/history")
    assert r_del.status_code == 204

    rows = session.exec(
        select(AccountHistory).where(AccountHistory.account_id_bidx == account_id_bidx)
    ).all()
    assert len(rows) == 0


def test_delete_account_history_not_found(session, master_key):
    """DELETE /bank/accounts/{id}/history returns 404 for unknown account."""
    client = TestClient(app)
    r = client.delete("/bank/accounts/non-existent-id/history")
    assert r.status_code == 404


def test_import_account_history(session, master_key):
    """POST /bank/accounts/{id}/history/import fills history from the provided entries."""
    client = TestClient(app)

    r = client.post("/bank/accounts", json={"name": "Import", "account_type": "CHECKING"})
    assert r.status_code == 201
    account_id = r.json()["id"]

    payload = {
        "entries": [
            {"snapshot_date": "2025-01-01", "value": "1000.00"},
            {"snapshot_date": "2025-06-01", "value": "2000.00"},
        ],
        "overwrite": False,
    }
    r_import = client.post(f"/bank/accounts/{account_id}/history/import", json=payload)
    assert r_import.status_code == 200
    assert r_import.json()["inserted"] > 0


def test_import_account_history_overwrite(session, master_key):
    """overwrite=True clears existing history before importing."""
    client = TestClient(app)

    r = client.post("/bank/accounts", json={"name": "Overwrite", "account_type": "CHECKING"})
    assert r.status_code == 201
    account_id = r.json()["id"]

    # First import
    payload_v1 = {
        "entries": [{"snapshot_date": "2025-01-01", "value": "9999"}],
        "overwrite": False,
    }
    client.post(f"/bank/accounts/{account_id}/history/import", json=payload_v1)

    # Second import with overwrite
    payload_v2 = {
        "entries": [{"snapshot_date": "2025-01-01", "value": "1234"}],
        "overwrite": True,
    }
    r2 = client.post(f"/bank/accounts/{account_id}/history/import", json=payload_v2)
    assert r2.status_code == 200

    # Verify history endpoint now reflects the new value
    r_hist = client.get(f"/bank/accounts/{account_id}/history")
    assert r_hist.status_code == 200
    jan1 = next((s for s in r_hist.json() if s["snapshot_date"] == "2025-01-01"), None)
    assert jan1 is not None
    assert Decimal(jan1["total_value"]) == Decimal("1234")


def test_import_account_history_not_found(session, master_key):
    """POST /bank/accounts/{id}/history/import returns 404 for unknown account."""
    client = TestClient(app)
    payload = {"entries": [{"snapshot_date": "2025-01-01", "value": "100"}], "overwrite": False}
    r = client.post("/bank/accounts/non-existent-id/history/import", json=payload)
    assert r.status_code == 404
