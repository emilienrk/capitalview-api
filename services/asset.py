"""Asset service — CRUD + valuation history."""

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlmodel import Session, select

from models.asset import Asset, AssetValuation
from models.account_history import AccountHistory
from dtos.asset import (
    AssetCreate,
    AssetUpdate,
    AssetSell,
    AssetResponse,
    AssetValuationCreate,
    AssetValuationUpdate,
    AssetValuationResponse,
    AssetCategorySummary,
    AssetSummaryResponse,
)
from dtos.transaction import AccountHistoryPosition, AccountHistorySnapshotResponse
from services.encryption import encrypt_data, decrypt_data, hash_index


def _valuation_sort_key(v: AssetValuation, master_key: str) -> tuple[date, datetime]:
    """Sort by valued_at first, then created_at as tiebreaker."""
    valued_at = _decrypt_valued_at(v.valued_at_enc, master_key) or date.min
    return valued_at, v.created_at


def _pick_latest_valuation(
    valuations: list[AssetValuation],
    master_key: str,
) -> Optional[AssetValuation]:
    """Return the latest valuation using valued_at then created_at ordering."""
    if not valuations:
        return None
    return max(valuations, key=lambda v: _valuation_sort_key(v, master_key))


def _latest_valuations_by_asset(
    session: Session,
    asset_ids: list[str],
    master_key: str,
) -> dict[str, AssetValuation]:
    """Load latest valuation for each asset in one query."""
    if not asset_ids:
        return {}

    rows = session.exec(
        select(AssetValuation).where(AssetValuation.asset_uuid.in_(asset_ids))
    ).all()

    grouped: dict[str, list[AssetValuation]] = defaultdict(list)
    for row in rows:
        grouped[row.asset_uuid].append(row)

    result: dict[str, AssetValuation] = {}
    for asset_uuid, vals in grouped.items():
        latest = _pick_latest_valuation(vals, master_key)
        if latest:
            result[asset_uuid] = latest

    return result


def _resolve_asset_estimated_value(
    asset: Asset,
    latest_valuation: Optional[AssetValuation],
    master_key: str,
) -> Decimal:
    """Resolve current asset value from latest valuation with safe fallbacks."""
    if asset.sold_price_enc:
        try:
            return Decimal(decrypt_data(asset.sold_price_enc, master_key))
        except Exception:
            pass

    if latest_valuation:
        try:
            return Decimal(decrypt_data(latest_valuation.estimated_value_enc, master_key))
        except Exception:
            pass

    if asset.purchase_price_enc:
        try:
            return Decimal(decrypt_data(asset.purchase_price_enc, master_key))
        except Exception:
            pass

    return Decimal("0")


def _map_asset_to_response(
    asset: Asset,
    master_key: str,
    latest_valuation: Optional[AssetValuation] = None,
) -> AssetResponse:
    """Decrypt and map an Asset to the response DTO."""
    name = decrypt_data(asset.name_enc, master_key)
    category = decrypt_data(asset.category_enc, master_key)
    estimated_value = _resolve_asset_estimated_value(asset, latest_valuation, master_key)

    description = None
    if asset.description_enc:
        description = decrypt_data(asset.description_enc, master_key)

    purchase_price: Optional[Decimal] = None
    if asset.purchase_price_enc:
        purchase_price = Decimal(decrypt_data(asset.purchase_price_enc, master_key))

    acquisition_date = None
    if asset.acquisition_date_enc:
        acquisition_date = decrypt_data(asset.acquisition_date_enc, master_key)

    # Sold fields
    sold_price: Optional[Decimal] = None
    if asset.sold_price_enc:
        sold_price = Decimal(decrypt_data(asset.sold_price_enc, master_key))

    sold_at: Optional[str] = None
    if asset.sold_at_enc:
        sold_at = decrypt_data(asset.sold_at_enc, master_key)

    # Get last valuation date
    last_valuation_date: Optional[str] = None
    if latest_valuation:
        try:
            last_valuation_date = decrypt_data(latest_valuation.valued_at_enc, master_key)
        except Exception:
            pass

    # Compute profit/loss
    profit_loss: Optional[Decimal] = None
    if purchase_price is not None and purchase_price > 0:
        profit_loss = estimated_value - purchase_price

    return AssetResponse(
        id=asset.uuid,
        name=name,
        description=description,
        category=category,
        purchase_price=purchase_price,
        estimated_value=estimated_value,
        currency=asset.currency,
        acquisition_date=acquisition_date,
        profit_loss=profit_loss,
        sold_price=sold_price,
        sold_at=sold_at,
        last_valuation_date=last_valuation_date,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _map_valuation_to_response(v: AssetValuation, master_key: str) -> AssetValuationResponse:
    """Decrypt and map a valuation entry."""
    estimated_value = Decimal(decrypt_data(v.estimated_value_enc, master_key))
    valued_at = decrypt_data(v.valued_at_enc, master_key)

    note = None
    if v.note_enc:
        note = decrypt_data(v.note_enc, master_key)

    return AssetValuationResponse(
        id=v.uuid,
        asset_id=v.asset_uuid,
        estimated_value=estimated_value,
        note=note,
        valued_at=valued_at,
        source=v.source,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def _create_valuation_row(
    session: Session,
    asset_uuid: str,
    estimated_value: Decimal,
    valued_at: str,
    master_key: str,
    note: Optional[str] = None,
    source: Optional[str] = None,
) -> AssetValuation:
    """Create and stage a valuation row (caller controls commit)."""
    note_enc = encrypt_data(note, master_key) if note else None
    valuation = AssetValuation(
        asset_uuid=asset_uuid,
        estimated_value_enc=encrypt_data(str(estimated_value), master_key),
        note_enc=note_enc,
        valued_at_enc=encrypt_data(valued_at, master_key),
        source=source,
    )
    session.add(valuation)
    return valuation


def create_asset(
    session: Session,
    data: AssetCreate,
    user_uuid: str,
    master_key: str,
) -> AssetResponse:
    """Create a new encrypted asset and initial valuation."""
    user_bidx = hash_index(user_uuid, master_key)

    purchase_price = data.purchase_price

    name_enc = encrypt_data(data.name, master_key)
    category_enc = encrypt_data(data.category, master_key)

    description_enc = None
    if data.description:
        description_enc = encrypt_data(data.description, master_key)

    purchase_price_enc = None
    if purchase_price is not None:
        purchase_price_enc = encrypt_data(str(purchase_price), master_key)

    acquisition_date_enc = None
    if data.acquisition_date:
        acquisition_date_enc = encrypt_data(data.acquisition_date, master_key)

    asset = Asset(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        description_enc=description_enc,
        category_enc=category_enc,
        purchase_price_enc=purchase_price_enc,
        currency=data.currency,
        acquisition_date_enc=acquisition_date_enc,
    )

    session.add(asset)
    session.flush()

    today_iso = datetime.now(timezone.utc).date().isoformat()
    acquisition_anchor_date = data.acquisition_date or today_iso

    # Anchor the curve at acquisition with purchase price when available.
    if purchase_price is not None:
        _create_valuation_row(
            session=session,
            asset_uuid=asset.uuid,
            estimated_value=purchase_price,
            valued_at=acquisition_anchor_date,
            master_key=master_key,
            source="asset_create_purchase",
        )

    # If user also provides a current estimated value, add a second point at "now".
    if data.estimated_value is not None:
        should_create_current_point = True

        # Avoid duplicate valuation when acquisition anchor already matches today's value/date.
        if purchase_price is not None and acquisition_anchor_date == today_iso:
            should_create_current_point = data.estimated_value != purchase_price

        if should_create_current_point:
            _create_valuation_row(
                session=session,
                asset_uuid=asset.uuid,
                estimated_value=data.estimated_value,
                valued_at=today_iso,
                master_key=master_key,
                source="asset_create_current",
            )

    session.commit()
    session.refresh(asset)

    latest = _pick_latest_valuation(
        session.exec(
            select(AssetValuation).where(AssetValuation.asset_uuid == asset.uuid)
        ).all(),
        master_key,
    )

    return _map_asset_to_response(asset, master_key, latest)


def update_asset(
    session: Session,
    asset: Asset,
    data: AssetUpdate,
    master_key: str,
) -> AssetResponse:
    """Update an existing asset."""
    if data.name is not None:
        asset.name_enc = encrypt_data(data.name, master_key)

    if data.description is not None:
        asset.description_enc = encrypt_data(data.description, master_key)

    if data.category is not None:
        asset.category_enc = encrypt_data(data.category, master_key)

    if data.purchase_price is not None:
        asset.purchase_price_enc = encrypt_data(str(data.purchase_price), master_key)

    if data.currency is not None:
        asset.currency = data.currency

    if data.acquisition_date is not None:
        asset.acquisition_date_enc = encrypt_data(data.acquisition_date, master_key)

    session.add(asset)
    session.commit()
    session.refresh(asset)

    latest = _pick_latest_valuation(
        session.exec(
            select(AssetValuation).where(AssetValuation.asset_uuid == asset.uuid)
        ).all(),
        master_key,
    )

    return _map_asset_to_response(asset, master_key, latest)


def delete_asset(
    session: Session,
    asset_uuid: str,
) -> bool:
    """Delete an asset and all its valuation history."""
    asset = session.get(Asset, asset_uuid)
    if not asset:
        return False

    # Delete valuation history
    valuations = session.exec(
        select(AssetValuation).where(AssetValuation.asset_uuid == asset_uuid)
    ).all()
    for v in valuations:
        session.delete(v)

    session.delete(asset)
    session.commit()
    return True


def sell_asset(
    session: Session,
    asset_uuid: str,
    data: AssetSell,
    master_key: str,
) -> AssetResponse:
    """Mark an asset as sold with price and date."""
    asset = session.get(Asset, asset_uuid)
    if not asset:
        raise ValueError("Asset not found")

    asset.sold_price_enc = encrypt_data(str(data.sold_price), master_key)
    asset.sold_at_enc = encrypt_data(data.sold_at, master_key)

    _create_valuation_row(
        session=session,
        asset_uuid=asset_uuid,
        estimated_value=data.sold_price,
        valued_at=data.sold_at,
        master_key=master_key,
        source="sell",
    )

    session.add(asset)
    session.commit()
    session.refresh(asset)

    latest = _pick_latest_valuation(
        session.exec(
            select(AssetValuation).where(AssetValuation.asset_uuid == asset.uuid)
        ).all(),
        master_key,
    )

    return _map_asset_to_response(asset, master_key, latest)


def get_user_assets(
    session: Session,
    user_uuid: str,
    master_key: str,
    include_sold: bool = False,
) -> AssetSummaryResponse:
    """Get all assets for a user with summary. Exclude sold by default."""
    user_bidx = hash_index(user_uuid, master_key)

    assets = session.exec(
        select(Asset).where(Asset.user_uuid_bidx == user_bidx)
    ).all()

    latest_by_asset = _latest_valuations_by_asset(
        session,
        [a.uuid for a in assets],
        master_key,
    )

    all_responses = [
        _map_asset_to_response(a, master_key, latest_by_asset.get(a.uuid))
        for a in assets
    ]

    # Filter out sold assets unless explicitly requested
    if not include_sold:
        responses = [a for a in all_responses if a.sold_at is None]
    else:
        responses = all_responses

    total_estimated = sum(a.estimated_value for a in responses)
    total_purchase = sum(a.purchase_price for a in responses if a.purchase_price is not None)

    total_pl: Optional[Decimal] = None
    if total_purchase > 0:
        total_pl = total_estimated - total_purchase

    # Group by category
    cat_map: dict[str, list[AssetResponse]] = defaultdict(list)
    for a in responses:
        cat_map[a.category].append(a)

    categories = [
        AssetCategorySummary(
            category=cat,
            count=len(items),
            total_estimated_value=sum(i.estimated_value for i in items),
        )
        for cat, items in cat_map.items()
    ]

    return AssetSummaryResponse(
        total_estimated_value=total_estimated,
        total_purchase_price=total_purchase,
        total_profit_loss=total_pl,
        asset_count=len(responses),
        categories=categories,
        assets=responses,
    )


def get_asset(
    session: Session,
    asset_uuid: str,
    user_uuid: str,
    master_key: str,
) -> Optional[AssetResponse]:
    """Get a single asset if it belongs to the user."""
    asset = session.get(Asset, asset_uuid)
    if not asset:
        return None

    user_bidx = hash_index(user_uuid, master_key)
    if asset.user_uuid_bidx != user_bidx:
        return None

    latest = _pick_latest_valuation(
        session.exec(
            select(AssetValuation).where(AssetValuation.asset_uuid == asset.uuid)
        ).all(),
        master_key,
    )

    return _map_asset_to_response(asset, master_key, latest)


def create_valuation(
    session: Session,
    asset_uuid: str,
    data: AssetValuationCreate,
    master_key: str,
) -> AssetValuationResponse:
    """Add a valuation entry for an asset."""
    _ensure_valuation_date_not_before_acquisition(
        session=session,
        asset_uuid=asset_uuid,
        valued_at=data.valued_at,
        master_key=master_key,
    )

    valuation = _create_valuation_row(
        session=session,
        asset_uuid=asset_uuid,
        estimated_value=data.estimated_value,
        valued_at=data.valued_at,
        master_key=master_key,
        note=data.note,
        source="manual",
    )

    session.add(valuation)
    session.commit()
    session.refresh(valuation)

    return _map_valuation_to_response(valuation, master_key)


def update_valuation(
    session: Session,
    valuation: AssetValuation,
    data: AssetValuationUpdate,
    master_key: str,
) -> AssetValuationResponse:
    """Update a valuation entry."""
    if "valued_at" in data.model_fields_set and data.valued_at is not None:
        _ensure_valuation_date_not_before_acquisition(
            session=session,
            asset_uuid=valuation.asset_uuid,
            valued_at=data.valued_at,
            master_key=master_key,
        )

    if "estimated_value" in data.model_fields_set and data.estimated_value is not None:
        valuation.estimated_value_enc = encrypt_data(str(data.estimated_value), master_key)

    if "note" in data.model_fields_set:
        valuation.note_enc = encrypt_data(data.note, master_key) if data.note else None

    if "valued_at" in data.model_fields_set and data.valued_at is not None:
        valuation.valued_at_enc = encrypt_data(data.valued_at, master_key)

    session.add(valuation)
    session.commit()
    session.refresh(valuation)
    return _map_valuation_to_response(valuation, master_key)


def get_asset_valuations(
    session: Session,
    asset_uuid: str,
    master_key: str,
) -> list[AssetValuationResponse]:
    """Get all valuations for an asset, sorted by date."""
    valuations = session.exec(
        select(AssetValuation).where(AssetValuation.asset_uuid == asset_uuid)
    ).all()

    valuations.sort(key=lambda v: _valuation_sort_key(v, master_key), reverse=True)
    return [_map_valuation_to_response(v, master_key) for v in valuations]


def delete_valuation(
    session: Session,
    valuation_uuid: str,
) -> bool:
    """Delete a single valuation entry."""
    v = session.get(AssetValuation, valuation_uuid)
    if not v:
        return False

    session.delete(v)
    session.commit()
    return True


def get_asset_rebuild_start_date(
    session: Session,
    asset_uuid: str,
    reference_date: date,
    master_key: str,
) -> date:
    """
    Return the earliest date from which account history must be rebuilt when a
    valuation at *reference_date* is added or removed for *asset_uuid*.

    The rebuild must start at the beginning of the interpolation segment that
    contains *reference_date*, i.e. the date of the previous valuation (or the
    asset's acquisition date if none exists).
    """
    asset = session.get(Asset, asset_uuid)
    if not asset:
        return reference_date

    # Collect all existing valuation dates for this asset
    valuations = session.exec(
        select(AssetValuation).where(AssetValuation.asset_uuid == asset_uuid)
    ).all()

    prev_date: Optional[date] = None
    for v in valuations:
        try:
            raw = _decrypt_valued_at(v.valued_at_enc, master_key)
            if raw and raw < reference_date:
                if prev_date is None or raw > prev_date:
                    prev_date = raw
        except Exception:
            continue

    if prev_date:
        return prev_date

    # Fall back to the asset's acquisition date
    if asset.acquisition_date_enc:
        try:
            from services.encryption import decrypt_data
            raw_acq = decrypt_data(asset.acquisition_date_enc, master_key)
            acq = _parse_date_str(raw_acq)
            if acq:
                return acq
        except Exception:
            pass

    return asset.created_at.date()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_date_str(value: str) -> Optional[date]:
    """Parse an ISO-like string to a date. Returns None on failure."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _decrypt_valued_at(valued_at_enc: str, master_key: str) -> Optional[date]:
    """Decrypt a valued_at field and return as a date. Returns None on failure."""
    try:
        raw = decrypt_data(valued_at_enc, master_key)
        return _parse_date_str(raw)
    except Exception:
        return None


def _ensure_valuation_date_not_before_acquisition(
    session: Session,
    asset_uuid: str,
    valued_at: str,
    master_key: str,
) -> None:
    """Reject valuation dates older than asset acquisition date."""
    asset = session.get(Asset, asset_uuid)
    if not asset:
        return

    valuation_date = _parse_date_str(valued_at)
    if not valuation_date:
        return

    acquired_at = get_asset_acquired_at(asset, master_key)
    if valuation_date < acquired_at:
        raise ValueError(
            f"La date de valorisation ne peut pas etre avant la date d'acquisition ({acquired_at.isoformat()})."
        )


def get_asset_acquired_at(asset: Asset, master_key: str) -> date:
    """
    Return the acquisition date of an asset (decrypted).
    Falls back to created_at when the field is absent or unparseable.
    """
    if asset.acquisition_date_enc:
        try:
            raw = decrypt_data(asset.acquisition_date_enc, master_key)
            parsed = _parse_date_str(raw)
            if parsed:
                return parsed
        except Exception:
            pass
    return asset.created_at.date()


def get_asset_portfolio_history(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> list[AccountHistorySnapshotResponse]:
    """
    Return decrypted daily snapshots for the user's virtual asset portfolio.
    All physical assets are stored under a single virtual account ID:
    ASSET_PORTFOLIO::{user_uuid_bidx}.
    """
    user_bidx = hash_index(user_uuid, master_key)
    virtual_account_id = f"ASSET_PORTFOLIO::{user_bidx}"
    account_id_bidx = hash_index(virtual_account_id, master_key)

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()

    result = []
    for row in rows:
        total_value = Decimal(decrypt_data(row.total_value_enc, master_key))
        total_invested = Decimal(decrypt_data(row.total_invested_enc, master_key))
        daily_pnl = (
            Decimal(decrypt_data(row.daily_pnl_enc, master_key))
            if row.daily_pnl_enc
            else None
        )

        positions = None
        if row.positions_enc:
            raw_json = decrypt_data(row.positions_enc, master_key)
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    positions = [
                        AccountHistoryPosition(
                            symbol=p["symbol"],
                            quantity=Decimal(p["quantity"]),
                            value=Decimal(p["value"]),
                            price=Decimal(p["price"]) if p.get("price") is not None else None,
                            invested=Decimal(p["invested"]),
                            percentage=Decimal(p["percentage"]),
                        )
                        for p in parsed
                    ]
                except Exception:
                    positions = None

        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=row.snapshot_date,
                total_value=total_value,
                total_invested=total_invested,
                daily_pnl=daily_pnl,
                positions=positions,
            )
        )

    return result
