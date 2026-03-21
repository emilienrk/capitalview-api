"""
Tests for services/account_history.py

Cover the core pure and DB-backed helpers:
  - _get_last_snapshot_dates
  - _get_price_matrix
  - _fill_price_gaps
  - _generate_missing_snapshots
"""

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session

from models.account_history import AccountHistory
from models.asset import Asset, AssetValuation
from models.enums import AccountCategory
from models.market import MarketAsset, MarketPriceHistory
from services.account_history import (
    _AccountSnapshot,
    FrozenPosition,
    _build_asset_snapshots,
    _fill_price_gaps,
    _generate_missing_snapshots,
    _get_last_snapshot_dates,
    _parse_positions_json,
    _get_price_matrix,
)
from services.encryption import decrypt_data, encrypt_data, hash_index


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_history_row(
    session: Session,
    *,
    user_uuid_bidx: str,
    account_id_bidx: str,
    account_type: AccountCategory,
    snapshot_date: date,
    total_value: str,
    master_key: str,
) -> AccountHistory:
    """Insert a minimal AccountHistory row and return it."""
    row = AccountHistory(
        uuid=str(uuid.uuid4()),
        user_uuid_bidx=user_uuid_bidx,
        account_id_bidx=account_id_bidx,
        account_type=account_type,
        snapshot_date=snapshot_date,
        total_value_enc=encrypt_data(total_value, master_key),
        total_invested_enc=encrypt_data("1000.00", master_key),
        daily_pnl_enc=encrypt_data("0.00", master_key),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _make_market_asset(
    session: Session, *, isin: str, symbol: str, name: str
) -> MarketAsset:
    """Insert a MarketAsset and return it."""
    asset = MarketAsset(isin=isin, symbol=symbol, name=name)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return asset


def _make_price(
    session: Session, asset_id: int, price: Decimal, price_date: date
) -> None:
    """Insert a MarketPriceHistory row."""
    now = datetime.now(timezone.utc)
    entry = MarketPriceHistory(
        market_asset_id=asset_id,
        price=price,
        price_date=price_date,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    session.commit()


# ---------------------------------------------------------------------------
# _get_last_snapshot_dates
# ---------------------------------------------------------------------------


def test_get_last_snapshot_dates_empty(session: Session, master_key: str):
    """Returns empty dict when no snapshots exist for the user."""
    result = _get_last_snapshot_dates(session, "bidx_ghost_user")
    assert result == {}


def test_get_last_snapshot_dates_returns_max_per_account(session: Session, master_key: str):
    """Returns the most recent snapshot date per account."""
    user_bidx = "bidx_user_multi_dates"
    acc1_bidx = "bidx_acc_alpha"
    acc2_bidx = "bidx_acc_beta"

    # acc1 has two rows – the later date must be returned
    _make_history_row(
        session, user_uuid_bidx=user_bidx, account_id_bidx=acc1_bidx,
        account_type=AccountCategory.STOCK, snapshot_date=date(2024, 1, 1),
        total_value="100.00", master_key=master_key,
    )
    _make_history_row(
        session, user_uuid_bidx=user_bidx, account_id_bidx=acc1_bidx,
        account_type=AccountCategory.STOCK, snapshot_date=date(2024, 1, 5),
        total_value="105.00", master_key=master_key,
    )
    # acc2 has one row
    _make_history_row(
        session, user_uuid_bidx=user_bidx, account_id_bidx=acc2_bidx,
        account_type=AccountCategory.CRYPTO, snapshot_date=date(2024, 1, 3),
        total_value="200.00", master_key=master_key,
    )

    result = _get_last_snapshot_dates(session, user_bidx)

    assert result[acc1_bidx] == date(2024, 1, 5)
    assert result[acc2_bidx] == date(2024, 1, 3)


def test_get_last_snapshot_dates_scoped_to_user(session: Session, master_key: str):
    """Rows belonging to another user do not appear in the result."""
    user1_bidx = "bidx_user_owner"
    user2_bidx = "bidx_user_stranger"
    acc_bidx = "bidx_acc_owned"

    _make_history_row(
        session, user_uuid_bidx=user1_bidx, account_id_bidx=acc_bidx,
        account_type=AccountCategory.STOCK, snapshot_date=date(2024, 1, 10),
        total_value="500.00", master_key=master_key,
    )

    assert _get_last_snapshot_dates(session, user2_bidx) == {}
    result_user1 = _get_last_snapshot_dates(session, user1_bidx)
    assert acc_bidx in result_user1
    assert result_user1[acc_bidx] == date(2024, 1, 10)


# ---------------------------------------------------------------------------
# _get_price_matrix
# ---------------------------------------------------------------------------


def test_get_price_matrix_empty_symbols(session: Session):
    """Returns empty dict immediately when the symbols list is empty."""
    result = _get_price_matrix(session, [], date(2024, 1, 1), date(2024, 1, 7))
    assert result == {}


def test_get_price_matrix_basic(session: Session):
    """Returns prices indexed by (isin, date) for a simple case."""
    asset = _make_market_asset(session, isin="US_AAPL_TEST", symbol="AAPL", name="Apple Inc.")
    _make_price(session, asset.id, Decimal("180.00"), date(2024, 1, 2))
    _make_price(session, asset.id, Decimal("182.50"), date(2024, 1, 3))

    result = _get_price_matrix(session, ["US_AAPL_TEST"], date(2024, 1, 1), date(2024, 1, 7))

    assert "US_AAPL_TEST" in result
    assert result["US_AAPL_TEST"][date(2024, 1, 2)] == Decimal("180.00")
    assert result["US_AAPL_TEST"][date(2024, 1, 3)] == Decimal("182.50")
    # Dates without prices are absent (sparse matrix)
    assert date(2024, 1, 1) not in result["US_AAPL_TEST"]


def test_get_price_matrix_out_of_range_excluded(session: Session):
    """Prices outside [from_date, to_date] are not included."""
    asset = _make_market_asset(session, isin="US_RANGE_TEST", symbol="RNG", name="Range Co.")
    _make_price(session, asset.id, Decimal("50.00"), date(2024, 1, 1))   # before range
    _make_price(session, asset.id, Decimal("55.00"), date(2024, 1, 5))   # in range
    _make_price(session, asset.id, Decimal("60.00"), date(2024, 1, 10))  # after range

    result = _get_price_matrix(session, ["US_RANGE_TEST"], date(2024, 1, 3), date(2024, 1, 7))

    sym = result.get("US_RANGE_TEST", {})
    assert date(2024, 1, 5) in sym
    assert date(2024, 1, 1) not in sym
    assert date(2024, 1, 10) not in sym


def test_get_price_matrix_multiple_symbols(session: Session):
    """Multiple symbols are returned in independent sub-dicts."""
    asset_a = _make_market_asset(session, isin="SYM_AA", symbol="AAA", name="Asset A")
    asset_b = _make_market_asset(session, isin="SYM_BB", symbol="BBB", name="Asset B")
    _make_price(session, asset_a.id, Decimal("10.00"), date(2024, 2, 1))
    _make_price(session, asset_b.id, Decimal("20.00"), date(2024, 2, 1))

    result = _get_price_matrix(session, ["SYM_AA", "SYM_BB"], date(2024, 2, 1), date(2024, 2, 1))

    assert result["SYM_AA"][date(2024, 2, 1)] == Decimal("10.00")
    assert result["SYM_BB"][date(2024, 2, 1)] == Decimal("20.00")


def test_get_price_matrix_unknown_symbol(session: Session):
    """An unknown symbol yields no entry in the result (no error)."""
    result = _get_price_matrix(session, ["DOES_NOT_EXIST"], date(2024, 1, 1), date(2024, 1, 5))
    assert result == {}


# ---------------------------------------------------------------------------
# _fill_price_gaps
# ---------------------------------------------------------------------------


def test_fill_price_gaps_no_action_when_all_covered(session: Session):
    """When all missing dates already have a price, nothing changes."""
    missing_dates = [date(2024, 1, 1), date(2024, 1, 2)]
    matrix = {
        "BTC": {
            date(2024, 1, 1): Decimal("40000.00"),
            date(2024, 1, 2): Decimal("41000.00"),
        }
    }
    result = _fill_price_gaps(matrix, ["BTC"], missing_dates, session)
    assert result["BTC"][date(2024, 1, 1)] == Decimal("40000.00")
    assert result["BTC"][date(2024, 1, 2)] == Decimal("41000.00")


def test_fill_price_gaps_propagates_price_forward(session: Session):
    """A price from day N is carried forward to days N+1, N+2 … when missing."""
    missing_dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
    matrix = {"BTC": {date(2024, 1, 1): Decimal("40000.00")}}

    result = _fill_price_gaps(matrix, ["BTC"], missing_dates, session)

    assert result["BTC"][date(2024, 1, 2)] == Decimal("40000.00")
    assert result["BTC"][date(2024, 1, 3)] == Decimal("40000.00")


def test_fill_price_gaps_new_price_overrides_carry(session: Session):
    """A newer in-range price replaces the carried-forward value."""
    missing_dates = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
    matrix = {
        "ETH": {
            date(2024, 1, 1): Decimal("2000.00"),
            date(2024, 1, 3): Decimal("2500.00"),
        }
    }
    result = _fill_price_gaps(matrix, ["ETH"], missing_dates, session)

    # Jan 2 carries Jan 1 price
    assert result["ETH"][date(2024, 1, 2)] == Decimal("2000.00")
    # Jan 3 has its own price – must not be overridden
    assert result["ETH"][date(2024, 1, 3)] == Decimal("2500.00")


def test_fill_price_gaps_fallback_from_db(session: Session):
    """A symbol absent from the matrix is filled from the latest DB price before range start."""
    asset = _make_market_asset(session, isin="SOL_FALLBACK", symbol="SOL", name="Solana")
    # Price exists *before* the range we're filling
    _make_price(session, asset.id, Decimal("100.00"), date(2023, 12, 31))

    missing_dates = [date(2024, 1, 1), date(2024, 1, 2)]
    matrix: dict = {}  # SOL has no in-range prices

    result = _fill_price_gaps(matrix, ["SOL_FALLBACK"], missing_dates, session)

    assert result["SOL_FALLBACK"][date(2024, 1, 1)] == Decimal("100.00")
    assert result["SOL_FALLBACK"][date(2024, 1, 2)] == Decimal("100.00")


def test_fill_price_gaps_no_fallback_available(session: Session):
    """Symbol with no DB price at all remains absent (no KeyError)."""
    missing_dates = [date(2024, 1, 1)]
    matrix: dict = {}

    result = _fill_price_gaps(matrix, ["UNKNOWN_COIN"], missing_dates, session)

    # Symbol may be absent or map to an empty dict — no price should exist
    assert not result.get("UNKNOWN_COIN")


# ---------------------------------------------------------------------------
# _generate_missing_snapshots
# ---------------------------------------------------------------------------


def test_generate_missing_snapshots_stock_values(master_key: str):
    """Total value and daily PnL are computed correctly for a stock account."""
    user_bidx = hash_index("user_stock_test", master_key)
    acc_bidx = hash_index("acc_stock_test", master_key)

    positions = [
        FrozenPosition(symbol="US0378331005", quantity=Decimal("10"), total_invested=Decimal("1800.00"))
    ]
    price_matrix = {
        "US0378331005": {
            date(2024, 3, 1): Decimal("180.00"),
            date(2024, 3, 2): Decimal("185.00"),
        }
    }

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.STOCK,
            frozen_positions=positions,
            total_invested=Decimal("1800.00"),
        ),
        price_matrix=price_matrix,
        missing_dates=[date(2024, 3, 1), date(2024, 3, 2)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    assert len(rows) == 2

    # Day 1: 10 × 180 = 1 800
    assert rows[0]["snapshot_date"] == date(2024, 3, 1)
    assert decrypt_data(rows[0]["total_value_enc"], master_key) == "1800.00"
    assert decrypt_data(rows[0]["total_invested_enc"], master_key) == "1800.00"
    # PnL = 1800 - 0 (prev_value) = 1800
    assert decrypt_data(rows[0]["daily_pnl_enc"], master_key) == "1800.00"

    # Day 2: 10 × 185 = 1 850
    assert rows[1]["snapshot_date"] == date(2024, 3, 2)
    assert decrypt_data(rows[1]["total_value_enc"], master_key) == "1850.00"
    # PnL = 1850 - 1800 = 50
    assert decrypt_data(rows[1]["daily_pnl_enc"], master_key) == "50.00"


def test_generate_missing_snapshots_bank_frozen(master_key: str):
    """Bank account value is frozen and exported as a single EUR position."""
    user_bidx = hash_index("user_bank_test", master_key)
    acc_bidx = hash_index("acc_bank_test", master_key)
    balance = Decimal("5000.00")

    positions = [FrozenPosition(symbol="EUR", quantity=balance, total_invested=balance)]

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.BANK,
            frozen_positions=positions,
            total_invested=balance,
        ),
        price_matrix={},
        missing_dates=[date(2024, 3, 1), date(2024, 3, 2)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    assert len(rows) == 2
    for row in rows:
        assert decrypt_data(row["total_value_enc"], master_key) == "5000.00"
        assert row["positions_enc"] is not None
        positions_dec = json.loads(decrypt_data(row["positions_enc"], master_key))
        assert len(positions_dec) == 1
        assert positions_dec[0]["symbol"] == "EUR"
        assert positions_dec[0]["percentage"] == "100.00"
    assert rows[0]["account_type"] == AccountCategory.BANK.value


def test_generate_missing_snapshots_position_with_missing_price_zero(master_key: str):
    """A position whose price is absent for a day is included with value=0 in positions_enc."""
    user_bidx = hash_index("user_partial", master_key)
    acc_bidx = hash_index("acc_partial", master_key)

    positions = [
        FrozenPosition(symbol="PRICED", quantity=Decimal("2"), total_invested=Decimal("200.00")),
        FrozenPosition(symbol="UNPRICED", quantity=Decimal("5"), total_invested=Decimal("500.00")),
    ]
    price_matrix = {
        "PRICED": {date(2024, 3, 1): Decimal("100.00")},
        # UNPRICED intentionally absent
    }

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.STOCK,
            frozen_positions=positions,
            total_invested=Decimal("700.00"),
        ),
        price_matrix=price_matrix,
        missing_dates=[date(2024, 3, 1)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    assert len(rows) == 1
    # Only PRICED contributes to total: 2 × 100 = 200
    assert decrypt_data(rows[0]["total_value_enc"], master_key) == "200.00"

    # positions_enc should contain both: PRICED with its value, UNPRICED with value=0
    positions_dec = json.loads(decrypt_data(rows[0]["positions_enc"], master_key))
    assert len(positions_dec) == 2
    by_symbol = {p["symbol"]: p for p in positions_dec}
    assert by_symbol["PRICED"]["value"] == "200.00"
    assert by_symbol["UNPRICED"]["value"] == "0.00"
    assert by_symbol["PRICED"]["invested"] == "200.00"
    assert by_symbol["UNPRICED"]["invested"] == "500.00"


def test_generate_missing_snapshots_positions_json_structure(master_key: str):
    """positions_enc JSON contains symbol, quantity, and rounded value."""
    user_bidx = hash_index("user_json_test", master_key)
    acc_bidx = hash_index("acc_json_test", master_key)

    positions = [
        FrozenPosition(symbol="BTC", quantity=Decimal("0.5"), total_invested=Decimal("15000.00"))
    ]
    price_matrix = {"BTC": {date(2024, 4, 1): Decimal("40000.00")}}

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.CRYPTO,
            frozen_positions=positions,
            total_invested=Decimal("15000.00"),
        ),
        price_matrix=price_matrix,
        missing_dates=[date(2024, 4, 1)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    assert len(rows) == 1
    positions_dec = json.loads(decrypt_data(rows[0]["positions_enc"], master_key))
    assert len(positions_dec) == 1
    entry = positions_dec[0]
    assert entry["symbol"] == "BTC"
    assert entry["quantity"] == "0.5"
    # 0.5 × 40000 = 20000
    assert entry["value"] == "20000.00"
    assert entry["invested"] == "15000.00"


def test_parse_positions_json_reads_invested_with_backward_compat():
    """Parser should read invested when present and default to 0 when absent."""
    parsed = _parse_positions_json(
        json.dumps([
            {"symbol": "BTC", "quantity": "1.25", "invested": "42000.50"},
            {"symbol": "ETH", "quantity": "3"},  # old payload without invested
        ])
    )

    assert len(parsed) == 2
    by_symbol = {p.symbol: p for p in parsed}
    assert by_symbol["BTC"].total_invested == Decimal("42000.50")
    assert by_symbol["ETH"].total_invested == Decimal("0")


def test_generate_missing_snapshots_no_positions(master_key: str):
    """An empty frozen_positions list yields zero total_value and no positions_enc."""
    user_bidx = hash_index("user_empty", master_key)
    acc_bidx = hash_index("acc_empty", master_key)

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.STOCK,
            frozen_positions=[],
            total_invested=Decimal("0"),
        ),
        price_matrix={},
        missing_dates=[date(2024, 5, 1)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    assert len(rows) == 1
    assert decrypt_data(rows[0]["total_value_enc"], master_key) == "0.00"
    assert rows[0]["positions_enc"] is None


def test_generate_missing_snapshots_row_metadata(master_key: str):
    """Each generated row has the expected metadata keys and encrypted user/account indexes."""
    user_bidx = hash_index("user_meta", master_key)
    acc_bidx = hash_index("acc_meta", master_key)

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.CRYPTO,
            frozen_positions=[],
            total_invested=Decimal("0"),
        ),
        price_matrix={},
        missing_dates=[date(2024, 6, 1)],
        prev_value=Decimal("0"),
        master_key=master_key,
    )

    row = rows[0]
    assert row["user_uuid_bidx"] == user_bidx
    assert row["account_id_bidx"] == acc_bidx
    assert row["account_type"] == AccountCategory.CRYPTO.value
    assert "uuid" in row
    assert "created_at" in row
    assert "updated_at" in row


def test_generate_missing_snapshots_exact_mode_with_bootstrap_state(master_key: str):
    """Exact mode must start from bootstrap positions when only post-snapshot txs are replayed."""
    user_bidx = hash_index("user_bootstrap", master_key)
    acc_bidx = hash_index("acc_bootstrap", master_key)

    class Tx:
        def __init__(self, executed_at: datetime, tx_type: str, amount: Decimal, price: Decimal):
            self.executed_at = executed_at
            self.type = tx_type
            self.amount = amount
            self.price_per_unit = price
            self.fees = Decimal("0")
            self.symbol = "BTC"
            self.isin = None

    # Represents only transactions after the previous snapshot.
    post_snapshot_txs = [
        Tx(datetime(2024, 3, 2, 12, 0, 0), "BUY", Decimal("0.2"), Decimal("50000")),
    ]

    rows = _generate_missing_snapshots(
        user_uuid_bidx=user_bidx,
        account_id_bidx=acc_bidx,
        account_snapshot=_AccountSnapshot(
            account_id="fake_id",
            account_type=AccountCategory.CRYPTO,
            frozen_positions=[
                FrozenPosition(symbol="BTC", quantity=Decimal("1.0"), total_invested=Decimal("40000"))
            ],
            total_invested=Decimal("40000"),
            transactions=post_snapshot_txs,
        ),
        price_matrix={
            "BTC": {
                date(2024, 3, 2): Decimal("50000"),
                date(2024, 3, 3): Decimal("55000"),
            }
        },
        missing_dates=[date(2024, 3, 2), date(2024, 3, 3)],
        prev_value=Decimal("50000"),  # previous snapshot: 1 BTC * 50k
        master_key=master_key,
    )

    assert len(rows) == 2
    # Day 1: (1.0 + 0.2) * 50k
    assert decrypt_data(rows[0]["total_value_enc"], master_key) == "60000.00"
    # Day 2: 1.2 * 55k
    assert decrypt_data(rows[1]["total_value_enc"], master_key) == "66000.00"


def test_build_asset_snapshots_keeps_past_then_drops_after_sale(session: Session, master_key: str):
    """Asset snapshots keep detailed physical assets with sale and valuation metadata."""
    user_uuid = "user-assets-sale"
    user_bidx = hash_index(user_uuid, master_key)

    sold_asset = Asset(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data("Montre", master_key),
        category_enc=encrypt_data("Luxe", master_key),
        estimated_value_enc=encrypt_data("1000", master_key),
        sold_at_enc=encrypt_data("2024-03-10", master_key),
    )
    active_asset = Asset(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data("Or", master_key),
        category_enc=encrypt_data("Métal", master_key),
        estimated_value_enc=encrypt_data("300", master_key),
    )
    session.add(sold_asset)
    session.add(active_asset)
    session.commit()
    session.refresh(sold_asset)

    session.add(
        AssetValuation(
            asset_uuid=sold_asset.uuid,
            estimated_value_enc=encrypt_data("1200", master_key),
            valued_at_enc=encrypt_data("2024-03-09", master_key),
        )
    )
    session.commit()

    snapshots = _build_asset_snapshots(session, master_key, user_bidx)
    assert len(snapshots) == 1

    snap = snapshots[0]
    assert snap.account_type == AccountCategory.ASSET
    assert len(snap.physical_assets) == 2

    by_name = {a.name: a for a in snap.physical_assets}
    assert "Montre" in by_name
    assert "Or" in by_name

    assert by_name["Montre"].sold_at == date(2024, 3, 10)
    assert by_name["Montre"].valuations == [(date(2024, 3, 9), Decimal("1200"))]
