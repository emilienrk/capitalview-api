"""Data export and account deletion from the security settings."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from main import app
from models.account_history import AccountHistory
from models.api_token import ApiToken
from models.asset import Asset, AssetValuation
from models.bank import BankAccount
from models.card import Card
from models.cashflow import Cashflow
from models.community import (
    CommunityFollow,
    CommunityPick,
    CommunityPosition,
    CommunityProfile,
)
from models.crypto import CryptoAccount, CryptoTransaction
from models.note import Note
from models.notification import Notification
from models.stock import StockAccount, StockTransaction
from models.user import (
    RefreshToken,
    TotpBackupCode,
    User,
    UserAIProvider,
    UserSettings,
)
from services.encryption import hash_index


@pytest.fixture(autouse=True)
def _override_deps(session):
    def _get_session():
        return session

    app.dependency_overrides.clear()
    from database import get_session

    app.dependency_overrides[get_session] = _get_session

    from routes.auth import _rate_hits

    _rate_hits.clear()

    yield

    app.dependency_overrides.clear()
    _rate_hits.clear()


PASSWORD = "StrongDelete1!"

# Every table a user owns, with how each one is joined back to them. The purge
# is only trustworthy if all of them come back empty, so the list is spelled out
# here rather than derived — a new table must fail this test until it is handled.
BIDX_MODELS = (
    BankAccount,
    StockAccount,
    CryptoAccount,
    Cashflow,
    Note,
    Card,
    Asset,
    UserSettings,
    UserAIProvider,
    AccountHistory,
)
FK_MODELS = (
    (ApiToken, "user_uuid"),
    (TotpBackupCode, "user_uuid"),
    (RefreshToken, "user_uuid"),
    (CommunityProfile, "user_id"),
    (CommunityPick, "user_id"),
    (Notification, "user_uuid"),
)


def _register(client: TestClient, session, email: str = "erase@example.com") -> tuple[str, str, str]:
    """Register a user and return (access token, master key, uuid).

    The uuid comes from the database because UserResponse does not expose it.
    """
    username = email.split("@")[0]
    payload = {"username": username, "email": email, "password": PASSWORD}
    response = client.post("/auth/register", json=payload, headers={"X-Return-Master-Key": "true"})
    assert response.status_code == 201
    body = response.json()

    # Registering sets a master_key cookie, and get_master_key reads the cookie
    # before the header. Left in place, the last user registered would silently
    # own every subsequent request in a multi-user test.
    client.cookies.clear()

    user = session.exec(select(User).where(User.username == username)).one()
    return body["access_token"], body["master_key"], user.uuid


def _auth_headers(access_token: str, master_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "X-Master-Key": master_key}


def _seed_account(client: TestClient, headers: dict[str, str]) -> None:
    """Create one row in each of the main user-owned domains."""
    bank = client.post(
        "/bank/accounts",
        json={"name": "Compte courant", "account_type": "CHECKING", "balance": "2500"},
        headers=headers,
    )
    assert bank.status_code == 201

    stock = client.post(
        "/stocks/accounts",
        json={"name": "Mon PEA", "account_type": "PEA"},
        headers=headers,
    )
    assert stock.status_code == 201
    tx = client.post(
        "/stocks/transactions",
        json={
            "account_id": stock.json()["id"],
            "asset_key": "US0378331005",
            "type": "BUY",
            "amount": "2.5",
            "price_per_unit": "150",
            "fees": "1",
            "executed_at": "2023-01-01T12:00:00",
        },
        headers=headers,
    )
    assert tx.status_code == 201

    asset = client.post(
        "/assets",
        json={"name": "AWP Dragon Lore", "category": "Gaming", "purchase_price": 800},
        headers=headers,
    )
    assert asset.status_code == 201
    valuation = client.post(
        f"/assets/{asset.json()['id']}/valuations",
        json={"estimated_value": 1500, "valued_at": "2025-06-01"},
        headers=headers,
    )
    assert valuation.status_code == 201

    cashflow = client.post(
        "/cashflow",
        json={
            "name": "Salaire",
            "flow_type": "INFLOW",
            "category": "Salary",
            "amount": "3000",
            "frequency": "MONTHLY",
            "transaction_date": "2025-01-01",
        },
        headers=headers,
    )
    assert cashflow.status_code == 201

    note = client.post(
        "/notes",
        json={"name": "Ma stratégie", "description": "DCA mensuel"},
        headers=headers,
    )
    assert note.status_code == 201


def _remaining_rows(session, user_uuid: str, master_key: str) -> dict[str, int]:
    """Count every row still attached to a user, table by table."""
    user_bidx = hash_index(user_uuid, master_key)
    counts: dict[str, int] = {}

    for model in BIDX_MODELS:
        rows = session.exec(select(model).where(model.user_uuid_bidx == user_bidx)).all()
        counts[model.__tablename__] = len(rows)

    for model, column in FK_MODELS:
        rows = session.exec(
            select(model).where(getattr(model, column) == user_uuid)
        ).all()
        counts[model.__tablename__] = len(rows)

    # Transactions hang off accounts, so they survive an account-blind sweep.
    for account_model, tx_model in (
        (StockAccount, StockTransaction),
        (CryptoAccount, CryptoTransaction),
    ):
        account_uuids = session.exec(
            select(account_model.uuid).where(account_model.user_uuid_bidx == user_bidx)
        ).all()
        account_bidx = [hash_index(uuid, master_key) for uuid in account_uuids]
        rows = (
            session.exec(
                select(tx_model).where(tx_model.account_id_bidx.in_(account_bidx))
            ).all()
            if account_bidx
            else []
        )
        counts[tx_model.__tablename__] = len(rows)

    asset_uuids = session.exec(
        select(Asset.uuid).where(Asset.user_uuid_bidx == user_bidx)
    ).all()
    valuations = (
        session.exec(
            select(AssetValuation).where(AssetValuation.asset_uuid.in_(asset_uuids))
        ).all()
        if asset_uuids
        else []
    )
    counts[AssetValuation.__tablename__] = len(valuations)

    counts[CommunityPosition.__tablename__] = len(
        session.exec(
            select(CommunityPosition).where(CommunityPosition.profile_user_id == user_uuid)
        ).all()
    )
    counts[CommunityFollow.__tablename__] = len(
        session.exec(
            select(CommunityFollow).where(
                (CommunityFollow.follower_id == user_uuid)
                | (CommunityFollow.following_id == user_uuid)
            )
        ).all()
    )
    counts["users"] = 1 if session.get(User, user_uuid) else 0
    return counts


# ──────────────────────────── Export ────────────────────────────

def test_export_contains_every_domain_the_user_filled_in(session):
    client = TestClient(app)
    access_token, master_key, _ = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    response = client.get("/auth/me/export", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["export_version"] == 1
    assert data["account"]["username"] == "erase"
    assert data["bank_accounts"][0]["name"] == "Compte courant"
    assert data["stock_accounts"][0]["name"] == "Mon PEA"
    assert data["stock_accounts"][0]["transactions"][0]["asset_key"] == "US0378331005"
    assert data["assets"][0]["name"] == "AWP Dragon Lore"
    # Valuations come back newest first, and creating the asset already seeded
    # one at today's date, so the one we posted is somewhere in the list.
    assert "2025-06-01" in [v["valued_at"] for v in data["assets"][0]["valuations"]]
    assert data["cashflows"][0]["name"] == "Salaire"
    assert data["notes"][0]["name"] == "Ma stratégie"
    assert data["settings"]["theme"]


def test_export_never_leaks_an_authentication_secret(session):
    """The file lands in a Downloads folder — a credential in it is a way back in."""
    client = TestClient(app)
    access_token, master_key, _ = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    response = client.get("/auth/me/export", headers=headers)
    assert response.status_code == 200

    raw = response.text
    for secret in (
        "password_hash",
        "auth_salt",
        "mk_wrapped_password",
        "mk_salt_password",
        "mk_wrapped_recovery",
        "mk_salt_recovery",
        "totp_secret_enc",
        "api_key_enc",
        "token_hash",
    ):
        assert secret not in raw, f"{secret} leaked into the export"

    user = session.get(User, response.json()["account"]["uuid"])
    assert user.password_hash not in raw
    assert user.auth_salt not in raw


def test_export_keeps_decimals_exact(session):
    """Serialised as strings: a JSON float would quietly round someone's net worth."""
    client = TestClient(app)
    access_token, master_key, _ = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    data = client.get("/auth/me/export", headers=headers).json()

    assert isinstance(data["stock_accounts"][0]["transactions"][0]["amount"], str)
    assert data["stock_accounts"][0]["transactions"][0]["amount"] == "2.5"


# ──────────────────────────── Deletion ────────────────────────────

def test_a_wrong_password_deletes_nothing(session):
    client = TestClient(app)
    access_token, master_key, user_uuid = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    response = client.request(
        "DELETE",
        "/auth/me",
        json={"password": "NotMyPassword1!", "confirm_username": "erase"},
        headers=headers,
    )

    assert response.status_code == 401
    assert session.get(User, user_uuid) is not None
    assert _remaining_rows(session, user_uuid, master_key)["bank_accounts"] == 1


def test_a_mistyped_username_deletes_nothing(session):
    client = TestClient(app)
    access_token, master_key, user_uuid = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    response = client.request(
        "DELETE",
        "/auth/me",
        json={"password": PASSWORD, "confirm_username": "eras"},
        headers=headers,
    )

    assert response.status_code == 400
    assert session.get(User, user_uuid) is not None
    assert _remaining_rows(session, user_uuid, master_key)["bank_accounts"] == 1


def test_deleting_the_account_leaves_no_row_behind(session):
    """The test that matters: it fails if the purge trusts a DB cascade.

    SQLite does not enforce foreign keys here, so anything left to
    ``ON DELETE CASCADE`` would still be sitting in the table afterwards.
    """
    client = TestClient(app)
    access_token, master_key, user_uuid = _register(client, session)
    headers = _auth_headers(access_token, master_key)
    _seed_account(client, headers)

    before = _remaining_rows(session, user_uuid, master_key)
    assert before["bank_accounts"] == 1
    # The BUY we posted, plus the EUR movement the account creation books itself.
    assert before["stock_transactions"] >= 1
    assert before["asset_valuations"] >= 1
    assert before["refresh_tokens"] >= 1

    response = client.request(
        "DELETE",
        "/auth/me",
        json={"password": PASSWORD, "confirm_username": "erase"},
        headers=headers,
    )

    assert response.status_code == 204
    after = _remaining_rows(session, user_uuid, master_key)
    assert after == {table: 0 for table in after}, f"rows survived the purge: {after}"


def test_the_access_token_dies_with_the_account(session):
    """No JWT blacklist needed: get_current_user resolves the row on every call."""
    client = TestClient(app)
    access_token, master_key, _ = _register(client, session)
    headers = _auth_headers(access_token, master_key)

    deleted = client.request(
        "DELETE",
        "/auth/me",
        json={"password": PASSWORD, "confirm_username": "erase"},
        headers=headers,
    )
    assert deleted.status_code == 204

    assert client.get("/auth/me", headers=headers).status_code == 401


def test_deleting_one_account_spares_the_other(session):
    client = TestClient(app)

    victim_token, victim_mk, victim_uuid = _register(client, session, "victim@example.com")
    _seed_account(client, _auth_headers(victim_token, victim_mk))

    keeper_token, keeper_mk, keeper_uuid = _register(client, session, "keeper@example.com")
    _seed_account(client, _auth_headers(keeper_token, keeper_mk))

    response = client.request(
        "DELETE",
        "/auth/me",
        json={"password": PASSWORD, "confirm_username": "victim"},
        headers=_auth_headers(victim_token, victim_mk),
    )
    assert response.status_code == 204

    keeper = _remaining_rows(session, keeper_uuid, keeper_mk)
    assert keeper["users"] == 1
    assert keeper["bank_accounts"] == 1
    assert keeper["stock_transactions"] >= 1
    assert keeper["notes"] == 1

    assert session.get(User, victim_uuid) is None
