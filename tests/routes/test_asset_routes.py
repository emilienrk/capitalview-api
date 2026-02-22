"""Tests for /assets routes."""

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


# ──────────────────────── CRUD ────────────────────────────

def test_create_asset(session, master_key):
    client = TestClient(app)
    payload = {
        "name": "AWP Dragon Lore",
        "category": "Gaming",
        "estimated_value": 1500,
        "purchase_price": 800,
        "description": "Factory New",
        "acquisition_date": "2024-06-15",
    }
    r = client.post("/assets", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "AWP Dragon Lore"
    assert data["category"] == "Gaming"
    assert float(data["estimated_value"]) == 1500
    assert float(data["purchase_price"]) == 800
    assert data["description"] == "Factory New"
    assert data["acquisition_date"] == "2024-06-15"
    assert data["currency"] == "EUR"
    assert data["id"]


def test_create_asset_auto_fill(session, master_key):
    """Providing only purchase_price should auto-fill estimated_value."""
    client = TestClient(app)
    r = client.post("/assets", json={"name": "Car", "category": "Véhicule", "purchase_price": 15000})
    assert r.status_code == 201
    data = r.json()
    assert float(data["purchase_price"]) == 15000
    assert float(data["estimated_value"]) == 15000


def test_create_asset_no_price_fails(session, master_key):
    """At least one price is required."""
    client = TestClient(app)
    r = client.post("/assets", json={"name": "Nothing", "category": "Autre"})
    assert r.status_code == 422


def test_list_assets(session, master_key):
    client = TestClient(app)
    client.post("/assets", json={"name": "Item 1", "category": "Gaming", "estimated_value": 100})
    client.post("/assets", json={"name": "Item 2", "category": "Véhicule", "estimated_value": 5000})

    r = client.get("/assets")
    assert r.status_code == 200
    data = r.json()
    assert data["asset_count"] == 2
    assert float(data["total_estimated_value"]) == 5100
    assert len(data["assets"]) == 2
    assert len(data["categories"]) == 2


def test_get_asset(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={"name": "Watch", "category": "Bijoux", "estimated_value": 3000}).json()
    asset_id = created["id"]

    r = client.get(f"/assets/{asset_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Watch"


def test_get_asset_not_found(session, master_key):
    client = TestClient(app)
    r = client.get("/assets/nonexistent")
    assert r.status_code == 404


def test_update_asset(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={"name": "Old Name", "category": "Autre", "estimated_value": 50}).json()
    asset_id = created["id"]

    r = client.put(f"/assets/{asset_id}", json={"name": "New Name", "estimated_value": 75})
    assert r.status_code == 200
    assert r.json()["name"] == "New Name"
    assert float(r.json()["estimated_value"]) == 75


def test_update_asset_not_found(session, master_key):
    client = TestClient(app)
    r = client.put("/assets/nonexistent", json={"name": "X"})
    assert r.status_code == 404


def test_delete_asset(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={"name": "Del Me", "category": "Autre", "estimated_value": 10}).json()
    asset_id = created["id"]

    r = client.delete(f"/assets/{asset_id}")
    assert r.status_code == 204

    r2 = client.get(f"/assets/{asset_id}")
    assert r2.status_code == 404


def test_delete_asset_not_found(session, master_key):
    client = TestClient(app)
    r = client.delete("/assets/nonexistent")
    assert r.status_code == 404


# ──────────────────────── Sell ────────────────────────────

def test_sell_asset(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={
        "name": "Sold Skin",
        "category": "Gaming",
        "estimated_value": 200,
        "purchase_price": 100,
    }).json()
    asset_id = created["id"]

    r = client.post(f"/assets/{asset_id}/sell", json={
        "sold_price": 250,
        "sold_at": "2025-06-15",
    })
    assert r.status_code == 200
    data = r.json()
    assert float(data["sold_price"]) == 250
    assert data["sold_at"] == "2025-06-15"
    assert float(data["estimated_value"]) == 250


def test_sell_asset_hidden_from_list(session, master_key):
    client = TestClient(app)
    client.post("/assets", json={"name": "Active", "category": "Autre", "estimated_value": 100})
    sold = client.post("/assets", json={"name": "To Sell", "category": "Autre", "estimated_value": 200}).json()

    client.post(f"/assets/{sold['id']}/sell", json={"sold_price": 220, "sold_at": "2025-01-01"})

    r = client.get("/assets")
    data = r.json()
    assert data["asset_count"] == 1
    assert data["assets"][0]["name"] == "Active"


def test_sell_asset_not_found(session, master_key):
    client = TestClient(app)
    r = client.post("/assets/nonexistent/sell", json={"sold_price": 100, "sold_at": "2025-01-01"})
    assert r.status_code == 404


# ──────────────────────── Profit/Loss ─────────────────────

def test_profit_loss_calculation(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={
        "name": "Skin",
        "category": "Gaming",
        "estimated_value": 200,
        "purchase_price": 100,
    }).json()

    assert float(created["profit_loss"]) == 100


def test_no_profit_loss_percentage_field(session, master_key):
    """profit_loss_percentage should no longer be in the response."""
    client = TestClient(app)
    created = client.post("/assets", json={
        "name": "Gift",
        "category": "Autre",
        "estimated_value": 500,
    }).json()

    assert "profit_loss_percentage" not in created


# ──────────────────────── Valuations ──────────────────────

def test_valuation_crud(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={"name": "Car", "category": "Véhicule", "estimated_value": 15000}).json()
    asset_id = created["id"]

    # Add valuations
    v1 = client.post(f"/assets/{asset_id}/valuations", json={
        "estimated_value": 14000,
        "valued_at": "2025-06-01",
        "note": "After 1 year",
    })
    assert v1.status_code == 201
    v1_data = v1.json()
    assert float(v1_data["estimated_value"]) == 14000
    assert v1_data["note"] == "After 1 year"
    assert v1_data["valued_at"] == "2025-06-01"

    v2 = client.post(f"/assets/{asset_id}/valuations", json={
        "estimated_value": 13000,
        "valued_at": "2026-01-01",
    })
    assert v2.status_code == 201

    # List valuations
    r = client.get(f"/assets/{asset_id}/valuations")
    assert r.status_code == 200
    valuations = r.json()
    assert len(valuations) == 2

    # Delete one valuation
    v_id = valuations[0]["id"]
    r_del = client.delete(f"/assets/{asset_id}/valuations/{v_id}")
    assert r_del.status_code == 204

    r2 = client.get(f"/assets/{asset_id}/valuations")
    assert len(r2.json()) == 1


def test_valuation_asset_not_found(session, master_key):
    client = TestClient(app)
    r = client.get("/assets/nonexistent/valuations")
    assert r.status_code == 404

    r2 = client.post("/assets/nonexistent/valuations", json={
        "estimated_value": 100,
        "valued_at": "2025-01-01",
    })
    assert r2.status_code == 404


def test_valuation_not_found(session, master_key):
    client = TestClient(app)
    created = client.post("/assets", json={"name": "X", "category": "Autre", "estimated_value": 10}).json()
    asset_id = created["id"]

    r = client.delete(f"/assets/{asset_id}/valuations/nonexistent")
    assert r.status_code == 404


def test_delete_asset_cascades_valuations(session, master_key):
    """Deleting an asset should also delete its valuation history."""
    client = TestClient(app)
    created = client.post("/assets", json={"name": "Temp", "category": "Autre", "estimated_value": 10}).json()
    asset_id = created["id"]

    client.post(f"/assets/{asset_id}/valuations", json={"estimated_value": 5, "valued_at": "2025-01-01"})
    client.post(f"/assets/{asset_id}/valuations", json={"estimated_value": 8, "valued_at": "2025-06-01"})

    r = client.get(f"/assets/{asset_id}/valuations")
    assert len(r.json()) == 2

    # Delete the asset
    client.delete(f"/assets/{asset_id}")

    # Asset is gone
    assert client.get(f"/assets/{asset_id}").status_code == 404
