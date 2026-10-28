"""What a model reads off a photo reaches the user, even written as the screen shows it."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def _override_deps(session):
    from database import get_session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def headers(client):
    r = client.post(
        "/auth/register",
        json={"username": "photouser", "email": "photo@example.com", "password": "StrongPh1!x"},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _extract(client, headers, answer, path="/stocks/transactions/extract", module="routes.stocks"):
    agent = MagicMock()
    agent.analyse = AsyncMock(return_value=json.dumps(answer))
    with patch(f"{module}.ExtractTxAgent", return_value=agent):
        return client.post(path, files={"file": ("tx.png", b"img", "image/png")}, headers=headers)


def test_stock_orders_as_a_french_screen_writes_them(client, headers):
    # What DeepSeek, held to JSON but not to the schema, hands back
    r = _extract(client, headers, {"transactions": [{
        "reasoning": "BNP Paribas Easy Stoxx Europe, ACHAT / 5, EXÉCUTÉ / 20,87 €",
        "asset_key": None,
        "type": "buy",
        "amount": "5",
        "price_per_unit": "20,87 €",
        "fees": None,
        "executed_at": "09/10/2026 15:23:09",
    }]})

    assert r.status_code == 200
    [tx] = r.json()["transactions"]
    assert tx["type"] == "BUY"
    assert float(tx["price_per_unit"]) == 20.87
    assert float(tx["fees"]) == 0
    assert tx["executed_at"].startswith("2026-10-09T15:23:09")


@pytest.mark.parametrize("raw, expected", [("1 234,56", 1234.56), ("1.234,56", 1234.56), ("1,234.56", 1234.56)])
def test_thousands_separators_are_read(client, headers, raw, expected):
    r = _extract(client, headers, {"transactions": [{
        "type": "BUY", "amount": "1", "price_per_unit": raw, "fees": "0",
        "executed_at": "2026-10-09T15:23:09",
    }]})

    assert float(r.json()["transactions"][0]["price_per_unit"]) == expected


def test_transactions_read_but_unusable_say_why(client, headers):
    r = _extract(client, headers, {"transactions": [{
        "type": "BUY", "amount": "5", "executed_at": "2026-10-09T15:23:09",
    }]})

    assert r.status_code == 422
    assert "price_per_unit" in r.json()["detail"]


def test_nothing_read_stays_an_empty_list(client, headers):
    r = _extract(client, headers, {"transactions": []})

    assert r.status_code == 200
    assert r.json()["transactions"] == []


def test_crypto_amounts_with_commas(client, headers):
    r = _extract(client, headers, {"transactions": [{
        "type": "buy", "asset_key": "BTC", "amount": "0,0123",
        "quote_asset_key": "EUR", "quote_amount": "1 050,00 €",
        "fee_amount": None, "executed_at": "09/10/2026 15:23",
    }]}, "/crypto/transactions/extract", "routes.crypto")

    assert r.status_code == 200
    [tx] = r.json()["transactions"]
    assert float(tx["amount"]) == 0.0123
    assert float(tx["quote_amount"]) == 1050
