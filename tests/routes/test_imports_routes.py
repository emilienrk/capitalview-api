import textwrap

import pytest
from fastapi.testclient import TestClient

from main import app

BINANCE_CSV = textwrap.dedent("""\
    User_ID,UTC_Time,Account,Operation,Coin,Change,Remark
    123,2024-01-10 10:00:00,Spot,Deposit,EUR,100.0,
    123,2024-01-15 12:00:01,Spot,Transaction Buy,BTC,0.002,
    123,2024-01-15 12:00:02,Spot,Transaction Spend,EUR,-90.0,
    123,2024-01-15 12:00:03,Spot,Transaction Fee,BNB,-0.0001,
""")


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
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


@pytest.fixture()
def client_with_user(session):
    client = TestClient(app)
    r = client.post("/auth/register", json={
        "username": "importer", "email": "importer@example.com", "password": "Strongpass1!",
    })
    assert r.status_code == 201
    return client, {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def crypto_account(client_with_user):
    client, auth = client_with_user
    # Enable multi-account crypto mode is not needed for a single account
    r = client.post("/crypto/accounts", json={"name": "Binance", "platform": "Binance"}, headers=auth)
    assert r.status_code == 201
    return r.json()["id"]


def test_sources_lists_binance(client_with_user):
    client, auth = client_with_user
    r = client.get("/imports/sources", headers=auth)
    assert r.status_code == 200
    sources = {s["source_id"]: s for s in r.json()["sources"]}
    assert "binance" in sources
    assert sources["binance"]["category"] == "crypto"


def test_detect_binance(client_with_user):
    client, auth = client_with_user
    r = client.post("/imports/detect", json={"csv_content": BINANCE_CSV}, headers=auth)
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert matches and matches[0]["source_id"] == "binance"
    assert matches[0]["score"] >= 0.9


def test_unknown_source_404(client_with_user):
    client, auth = client_with_user
    r = client.post("/imports/nope/preview", json={"csv_content": "a,b\n1,2"}, headers=auth)
    assert r.status_code == 404


def test_preview_matches_legacy_binance_route(client_with_user):
    client, auth = client_with_user
    r_new = client.post("/imports/binance/preview", json={"csv_content": BINANCE_CSV}, headers=auth)
    assert r_new.status_code == 200
    r_old = client.post("/crypto/import/binance/preview", json={"csv_content": BINANCE_CSV}, headers=auth)
    assert r_old.status_code == 200
    assert r_new.json()["crypto"] == r_old.json()


def test_confirm_and_dedup_roundtrip(client_with_user, crypto_account):
    client, auth = client_with_user

    r_prev = client.post(
        "/imports/binance/preview",
        json={"csv_content": BINANCE_CSV, "account_id": crypto_account},
        headers=auth,
    )
    assert r_prev.status_code == 200
    preview = r_prev.json()
    assert preview["duplicates_count"] == 0
    groups = preview["crypto"]["groups"]

    r_conf = client.post(
        "/imports/binance/confirm",
        json={"account_id": crypto_account, "crypto_groups": groups},
        headers=auth,
    )
    assert r_conf.status_code == 201
    body = r_conf.json()
    assert body["imported_count"] == 4
    assert body["skipped_duplicates"] == 0

    # Preview again: everything flagged duplicate
    r_prev2 = client.post(
        "/imports/binance/preview",
        json={"csv_content": BINANCE_CSV, "account_id": crypto_account},
        headers=auth,
    )
    assert r_prev2.json()["duplicates_count"] == len(groups)

    # Confirm again with skip_duplicates (default): nothing imported
    r_conf2 = client.post(
        "/imports/binance/confirm",
        json={"account_id": crypto_account, "crypto_groups": groups},
        headers=auth,
    )
    assert r_conf2.status_code == 201
    assert r_conf2.json()["imported_count"] == 0
    assert r_conf2.json()["skipped_duplicates"] == len(groups)

    # skip_duplicates=false forces a re-import
    r_conf3 = client.post(
        "/imports/binance/confirm",
        json={"account_id": crypto_account, "crypto_groups": groups, "skip_duplicates": False},
        headers=auth,
    )
    assert r_conf3.json()["imported_count"] == 4


def test_confirm_unknown_account_404(client_with_user):
    client, auth = client_with_user
    r = client.post(
        "/imports/binance/confirm",
        json={"account_id": "does-not-exist", "crypto_groups": []},
        headers=auth,
    )
    assert r.status_code == 404


def test_csv_too_large_413(client_with_user):
    client, auth = client_with_user
    r = client.post(
        "/imports/detect",
        json={"csv_content": "x" * (5 * 1024 * 1024 + 1)},
        headers=auth,
    )
    assert r.status_code == 413
