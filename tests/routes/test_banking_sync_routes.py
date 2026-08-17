"""
Route tests for POST /banking/sync (ruling R16: global trigger, no body, the
server decides the order; a second call the same day is a 200, never an error).

No real network: the sync service is exercised through a `build_client` double,
as in tests/routes/test_banking_linking.py.
"""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dtos.bank import BankAccountCreate
from dtos.banking import BankConnectionUpdate
from main import app
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession
from models.enums import BankAccountType
from models.user import User
from services.bank import create_bank_account
from services.banking.credentials import upsert_connection
from services.encryption import encrypt_data, hash_index

USER_UUID = "sync_route_user"
TODAY = date.today()


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(uuid=USER_UUID, auth_salt="salt", username="t", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    from services.auth import get_current_user, get_master_key
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_master_key] = _get_master_key

    yield

    app.dependency_overrides.clear()


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Same workaround as tests/services/test_bank.py:30."""
    import sqlalchemy as sa

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


class FakeClient:
    def __init__(self):
        self.psu_context = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def get_balances(self, uid):
        return {
            "balances": [
                {
                    "name": "Booked balance",
                    "balance_amount": {"currency": "EUR", "amount": "1000.00"},
                    "balance_type": "CLBD",
                }
            ]
        }

    def iter_transactions(self, uid, date_from=None, strategy="default"):
        return iter(())


@pytest.fixture
def linked_account(session, master_key) -> BankAccount:
    upsert_connection(
        session,
        USER_UUID,
        master_key,
        BankConnectionUpdate(application_id="app", private_key="key"),
    )
    bank_session = BankSession(
        user_uuid_bidx=hash_index(USER_UUID, master_key),
        session_id_enc=encrypt_data("sess", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime.now(timezone.utc) + timedelta(days=180),
        authorized_at=datetime.now(timezone.utc),
        accounts_enc=encrypt_data(
            json.dumps([{"uid": "uid-1", "identification_hash": "ih-1", "cash_account_type": "CACC"}]),
            master_key,
        ),
    )
    session.add(bank_session)
    session.commit()

    resp = create_bank_account(
        session,
        BankAccountCreate(name="Compte", balance=Decimal("1000"), account_type=BankAccountType.CHECKING),
        USER_UUID,
        master_key,
    )
    account = session.get(BankAccount, resp.id)
    session.add(
        BankAccountLink(
            user_uuid_bidx=hash_index(USER_UUID, master_key),
            bank_account_uuid_bidx=hash_index(account.uuid, master_key),
            session_uuid=bank_session.uuid,
            identification_hash_bidx=hash_index("ih-1", master_key),
            account_uid_enc=encrypt_data("uid-1", master_key),
            anchor_date=TODAY - timedelta(days=2),
            anchor_balance_enc=encrypt_data("1000", master_key),
            last_synced_at=TODAY - timedelta(days=2),
        )
    )
    session.commit()
    return account


def test_sync_takes_no_body_and_returns_a_summary(monkeypatch, linked_account, sqlite_pg_insert):
    monkeypatch.setattr("services.banking.sync.build_client", lambda *a, **kw: FakeClient())
    client = TestClient(app)

    response = client.post("/banking/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["synced"] == 1
    assert body["results"][0]["status"] == "synced"


def test_a_second_call_the_same_day_is_not_an_error(monkeypatch, linked_account, sqlite_pg_insert):
    monkeypatch.setattr("services.banking.sync.build_client", lambda *a, **kw: FakeClient())
    client = TestClient(app)

    client.post("/banking/sync")
    response = client.post("/banking/sync")

    assert response.status_code == 200
    assert response.json()["synced"] == 0
    assert response.json()["results"][0]["status"] == "skipped_daily_cap"


def test_psu_context_headers_come_from_the_real_request(monkeypatch, linked_account, sqlite_pg_insert):
    captured = {}

    def _build_client(application_id, private_key, psu_context=None):
        captured["psu_context"] = psu_context
        return FakeClient()

    monkeypatch.setattr("services.banking.sync.build_client", _build_client)
    client = TestClient(app)

    client.post("/banking/sync", headers={"user-agent": "CapitalView-Test/1.0"})

    assert captured["psu_context"]["Psu-User-Agent"] == "CapitalView-Test/1.0"
    assert captured["psu_context"]["Psu-Ip-Address"]


def test_psu_context_is_all_or_nothing(monkeypatch, linked_account, sqlite_pg_insert):
    captured = {}

    def _build_client(application_id, private_key, psu_context=None):
        captured["psu_context"] = psu_context
        return FakeClient()

    monkeypatch.setattr("services.banking.sync.build_client", _build_client)
    # httpx sends its own user-agent unless it is explicitly removed.
    client = TestClient(app, headers={"user-agent": ""})

    client.post("/banking/sync")

    assert captured["psu_context"] is None


def test_sync_without_any_link_is_a_200_not_a_configuration_error(session, master_key):
    client = TestClient(app)

    response = client.post("/banking/sync")

    assert response.status_code == 200
    assert response.json() == {"synced": 0, "results": []}
