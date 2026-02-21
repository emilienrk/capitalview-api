"""Tests for asset service layer."""

import pytest
from decimal import Decimal
from sqlmodel import Session

from services.asset import (
    create_asset,
    get_user_assets,
    get_asset,
    update_asset,
    delete_asset,
    create_valuation,
    get_asset_valuations,
    delete_valuation,
)
from dtos.asset import AssetCreate, AssetUpdate, AssetValuationCreate
from models.asset import Asset, AssetValuation
from services.encryption import hash_index


# ──────────────────────── CRUD ────────────────────────────

def test_create_asset(session: Session, master_key: str):
    user_uuid = "user_1"
    data = AssetCreate(
        name="AWP Dragon Lore",
        category="Gaming",
        estimated_value=Decimal("1500"),
        purchase_price=Decimal("800"),
        description="Factory New",
        acquisition_date="2024-06-15",
    )
    resp = create_asset(session, data, user_uuid, master_key)
    assert resp.name == "AWP Dragon Lore"
    assert resp.category == "Gaming"
    assert resp.estimated_value == Decimal("1500")
    assert resp.purchase_price == Decimal("800")
    assert resp.description == "Factory New"
    assert resp.acquisition_date == "2024-06-15"
    assert resp.currency == "EUR"

    # Verify encryption in DB
    db_asset = session.get(Asset, resp.id)
    assert db_asset is not None
    assert db_asset.user_uuid_bidx == hash_index(user_uuid, master_key)
    assert db_asset.name_enc != "AWP Dragon Lore"
    assert db_asset.estimated_value_enc != "1500"


def test_create_asset_minimal(session: Session, master_key: str):
    """Create an asset with only required fields."""
    data = AssetCreate(name="Simple Item", category="Autre", estimated_value=Decimal("50"))
    resp = create_asset(session, data, "user_1", master_key)
    assert resp.name == "Simple Item"
    assert resp.description is None
    assert resp.purchase_price is None
    assert resp.acquisition_date is None
    assert resp.profit_loss is None
    assert resp.profit_loss_percentage is None


def test_get_user_assets_summary(session: Session, master_key: str):
    user_uuid = "user_1"
    create_asset(session, AssetCreate(name="A", category="Gaming", estimated_value=Decimal("100"), purchase_price=Decimal("50")), user_uuid, master_key)
    create_asset(session, AssetCreate(name="B", category="Gaming", estimated_value=Decimal("200")), user_uuid, master_key)
    create_asset(session, AssetCreate(name="C", category="Véhicule", estimated_value=Decimal("5000"), purchase_price=Decimal("8000")), user_uuid, master_key)

    summary = get_user_assets(session, user_uuid, master_key)
    assert summary.asset_count == 3
    assert summary.total_estimated_value == Decimal("5300")
    assert summary.total_purchase_price == Decimal("8050")  # 50 + 8000
    assert summary.total_profit_loss == Decimal("5300") - Decimal("8050")
    assert len(summary.categories) == 2

    # Check category breakdown
    cat_map = {c.category: c for c in summary.categories}
    assert cat_map["Gaming"].count == 2
    assert cat_map["Gaming"].total_estimated_value == Decimal("300")
    assert cat_map["Véhicule"].count == 1


def test_get_user_assets_empty(session: Session, master_key: str):
    summary = get_user_assets(session, "user_no_assets", master_key)
    assert summary.asset_count == 0
    assert summary.total_estimated_value == Decimal("0")
    assert summary.assets == []


def test_get_user_assets_isolation(session: Session, master_key: str):
    """User A cannot see user B's assets."""
    create_asset(session, AssetCreate(name="UserA Asset", category="Autre", estimated_value=Decimal("100")), "user_a", master_key)
    create_asset(session, AssetCreate(name="UserB Asset", category="Autre", estimated_value=Decimal("200")), "user_b", master_key)

    summary_a = get_user_assets(session, "user_a", master_key)
    summary_b = get_user_assets(session, "user_b", master_key)
    assert summary_a.asset_count == 1
    assert summary_a.assets[0].name == "UserA Asset"
    assert summary_b.asset_count == 1
    assert summary_b.assets[0].name == "UserB Asset"


def test_get_asset(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_asset(session, AssetCreate(name="My Watch", category="Bijoux", estimated_value=Decimal("3000")), user_uuid, master_key)
    fetched = get_asset(session, created.id, user_uuid, master_key)
    assert fetched is not None
    assert fetched.name == "My Watch"


def test_get_asset_wrong_user(session: Session, master_key: str):
    created = create_asset(session, AssetCreate(name="Private", category="Autre", estimated_value=Decimal("10")), "user_1", master_key)
    assert get_asset(session, created.id, "user_2", master_key) is None


def test_get_asset_not_found(session: Session, master_key: str):
    assert get_asset(session, "nonexistent", "user_1", master_key) is None


def test_update_asset(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_asset(session, AssetCreate(name="Old", category="Autre", estimated_value=Decimal("50")), user_uuid, master_key)
    db_asset = session.get(Asset, created.id)

    updated = update_asset(session, db_asset, AssetUpdate(
        name="New Name",
        estimated_value=Decimal("75"),
        category="Gaming",
        description="Updated description",
    ), master_key)

    assert updated.name == "New Name"
    assert updated.estimated_value == Decimal("75")
    assert updated.category == "Gaming"
    assert updated.description == "Updated description"


def test_update_asset_partial(session: Session, master_key: str):
    """Only provided fields should be updated."""
    user_uuid = "user_1"
    created = create_asset(session, AssetCreate(
        name="Original",
        category="Gaming",
        estimated_value=Decimal("100"),
        description="Keep this",
    ), user_uuid, master_key)
    db_asset = session.get(Asset, created.id)

    updated = update_asset(session, db_asset, AssetUpdate(name="Changed"), master_key)
    assert updated.name == "Changed"
    assert updated.description == "Keep this"
    assert updated.category == "Gaming"
    assert updated.estimated_value == Decimal("100")


def test_delete_asset(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_asset(session, AssetCreate(name="Del", category="Autre", estimated_value=Decimal("10")), user_uuid, master_key)
    assert delete_asset(session, created.id) is True
    assert session.get(Asset, created.id) is None


def test_delete_asset_not_found(session: Session, master_key: str):
    assert delete_asset(session, "nonexistent") is False


def test_delete_asset_cascades_valuations(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_asset(session, AssetCreate(name="Cascade", category="Autre", estimated_value=Decimal("10")), user_uuid, master_key)

    create_valuation(session, created.id, AssetValuationCreate(estimated_value=Decimal("8"), valued_at="2025-01-01"), master_key)
    create_valuation(session, created.id, AssetValuationCreate(estimated_value=Decimal("6"), valued_at="2025-06-01"), master_key)

    valuations_before = get_asset_valuations(session, created.id, master_key)
    assert len(valuations_before) == 2

    delete_asset(session, created.id)

    # Valuations should be gone too
    from sqlmodel import select
    remaining = session.exec(select(AssetValuation).where(AssetValuation.asset_uuid == created.id)).all()
    assert len(remaining) == 0


# ──────────────────── Profit/Loss ─────────────────────────

def test_profit_loss_positive(session: Session, master_key: str):
    resp = create_asset(session, AssetCreate(
        name="Winner",
        category="Gaming",
        estimated_value=Decimal("200"),
        purchase_price=Decimal("100"),
    ), "user_1", master_key)
    assert resp.profit_loss == Decimal("100")
    assert resp.profit_loss_percentage == pytest.approx(100.0)


def test_profit_loss_negative(session: Session, master_key: str):
    resp = create_asset(session, AssetCreate(
        name="Car",
        category="Véhicule",
        estimated_value=Decimal("8000"),
        purchase_price=Decimal("15000"),
    ), "user_1", master_key)
    assert resp.profit_loss == Decimal("-7000")
    assert resp.profit_loss_percentage == pytest.approx(-46.666666666666664)


def test_profit_loss_no_purchase(session: Session, master_key: str):
    resp = create_asset(session, AssetCreate(
        name="Gift",
        category="Autre",
        estimated_value=Decimal("500"),
    ), "user_1", master_key)
    assert resp.profit_loss is None
    assert resp.profit_loss_percentage is None


# ──────────────────── Valuations ──────────────────────────

def test_create_valuation(session: Session, master_key: str):
    asset = create_asset(session, AssetCreate(name="V-Item", category="Autre", estimated_value=Decimal("100")), "user_1", master_key)
    v = create_valuation(session, asset.id, AssetValuationCreate(
        estimated_value=Decimal("90"),
        valued_at="2025-06-01",
        note="Slight depreciation",
    ), master_key)

    assert v.asset_id == asset.id
    assert v.estimated_value == Decimal("90")
    assert v.valued_at == "2025-06-01"
    assert v.note == "Slight depreciation"

    # Verify encryption in DB
    db_val = session.get(AssetValuation, v.id)
    assert db_val.estimated_value_enc != "90"
    assert db_val.valued_at_enc != "2025-06-01"


def test_create_valuation_minimal(session: Session, master_key: str):
    asset = create_asset(session, AssetCreate(name="V2", category="Autre", estimated_value=Decimal("50")), "user_1", master_key)
    v = create_valuation(session, asset.id, AssetValuationCreate(
        estimated_value=Decimal("45"),
        valued_at="2025-01-01",
    ), master_key)
    assert v.note is None


def test_get_asset_valuations(session: Session, master_key: str):
    asset = create_asset(session, AssetCreate(name="History", category="Autre", estimated_value=Decimal("100")), "user_1", master_key)
    create_valuation(session, asset.id, AssetValuationCreate(estimated_value=Decimal("50"), valued_at="2024-01-01"), master_key)
    create_valuation(session, asset.id, AssetValuationCreate(estimated_value=Decimal("75"), valued_at="2025-01-01"), master_key)
    create_valuation(session, asset.id, AssetValuationCreate(estimated_value=Decimal("90"), valued_at="2025-06-01"), master_key)

    valuations = get_asset_valuations(session, asset.id, master_key)
    assert len(valuations) == 3
    # Should be sorted by valued_at descending
    assert valuations[0].valued_at == "2025-06-01"
    assert valuations[-1].valued_at == "2024-01-01"


def test_get_asset_valuations_empty(session: Session, master_key: str):
    asset = create_asset(session, AssetCreate(name="No History", category="Autre", estimated_value=Decimal("10")), "user_1", master_key)
    valuations = get_asset_valuations(session, asset.id, master_key)
    assert valuations == []


def test_delete_valuation(session: Session, master_key: str):
    asset = create_asset(session, AssetCreate(name="Del-V", category="Autre", estimated_value=Decimal("100")), "user_1", master_key)
    v = create_valuation(session, asset.id, AssetValuationCreate(estimated_value=Decimal("80"), valued_at="2025-01-01"), master_key)

    assert delete_valuation(session, v.id) is True
    assert session.get(AssetValuation, v.id) is None


def test_delete_valuation_not_found(session: Session, master_key: str):
    assert delete_valuation(session, "nonexistent") is False
