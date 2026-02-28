import pytest
from unittest.mock import patch
from decimal import Decimal
from datetime import datetime
from sqlmodel import Session

from services.crypto_transaction import (
    create_crypto_transaction,
    create_composite_crypto_transaction,
    get_crypto_transaction,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_account_transactions,
    get_crypto_account_summary,
)
from dtos.crypto import CryptoTransactionCreate, CryptoTransactionUpdate, CryptoCompositeTransactionCreate
from models.enums import CryptoTransactionType
from models.crypto import CryptoAccount, CryptoTransaction
from services.encryption import hash_index, encrypt_data


def test_create_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        symbol="BTC",
        type=CryptoTransactionType.BUY,
        amount=Decimal("0.5"),
        price_per_unit=Decimal("30000.0"),
        executed_at=datetime(2023, 1, 1, 12, 0, 0),
        notes="First buy",
        tx_hash="0x123",
    )
    resp = create_crypto_transaction(session, data, master_key)
    assert resp.symbol == "BTC"
    assert resp.type == "BUY"
    assert resp.amount == Decimal("0.5")
    assert resp.price_per_unit == Decimal("30000.0")
    assert resp.fees == Decimal("0")  # no fees on atomic BUY row
    assert resp.total_cost == Decimal("0.5") * Decimal("30000.0")
    assert resp.executed_at == datetime(2023, 1, 1, 12, 0, 0)
    tx_db = session.get(CryptoTransaction, resp.id)
    assert tx_db is not None
    assert tx_db.symbol_enc != "BTC"
    assert tx_db.account_id_bidx == hash_index("acc_crypto", master_key)


def test_get_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        symbol="ETH",
        type=CryptoTransactionType.BUY,
        amount=Decimal("2"),
        price_per_unit=Decimal("2000"),
        executed_at=datetime.now()
    )
    created = create_crypto_transaction(session, data, master_key)
    fetched = get_crypto_transaction(session, created.id, master_key)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.symbol == "ETH"
    assert get_crypto_transaction(session, "non_existent", master_key) is None
    tx_db = session.get(CryptoTransaction, created.id)
    tx_db.executed_at_enc = encrypt_data("BAD DATE", master_key)
    session.add(tx_db)
    session.commit()
    fetched_bad = get_crypto_transaction(session, created.id, master_key)
    assert fetched_bad.executed_at == tx_db.created_at


def test_update_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        symbol="SOL",
        type=CryptoTransactionType.BUY,
        amount=Decimal("10"),
        price_per_unit=Decimal("20"),
        executed_at=datetime(2023, 1, 1),
        notes="Old Note"
    )
    created = create_crypto_transaction(session, data, master_key)
    tx_db = session.get(CryptoTransaction, created.id)
    new_date = datetime(2023, 2, 2, 12, 0, 0)
    update_data = CryptoTransactionUpdate(
        symbol="SOLO",
        type=CryptoTransactionType.SPEND,
        amount=Decimal("5"),
        price_per_unit=Decimal("25"),
        executed_at=new_date,
        notes="New Note",
        tx_hash="0xABC"
    )
    updated = update_crypto_transaction(session, tx_db, update_data, master_key)
    assert updated.symbol == "SOLO"
    assert updated.type == "SPEND"
    assert updated.amount == Decimal("5")
    assert updated.price_per_unit == Decimal("25")
    assert updated.executed_at == new_date
    session.refresh(tx_db)
    from services.encryption import decrypt_data
    assert decrypt_data(tx_db.notes_enc, master_key) == "New Note"
    assert decrypt_data(tx_db.tx_hash_enc, master_key) == "0xABC"


def test_delete_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        symbol="ADA",
        type=CryptoTransactionType.BUY,
        amount=Decimal("100"),
        price_per_unit=Decimal("0.5"),
        executed_at=datetime.now()
    )
    created = create_crypto_transaction(session, data, master_key)
    assert delete_crypto_transaction(session, created.id) is True
    assert session.get(CryptoTransaction, created.id) is None
    assert delete_crypto_transaction(session, "non_existent") is False


def test_get_account_transactions(session: Session, master_key: str):
    acc1 = "acc_c1"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id=acc1, symbol="A", type=CryptoTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), executed_at=datetime.now()
    ), master_key)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_c2", symbol="B", type=CryptoTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), executed_at=datetime.now()
    ), master_key)
    txs = get_account_transactions(session, acc1, master_key)
    assert len(txs) == 1
    assert txs[0].symbol == "A"


@patch("services.crypto_transaction.get_crypto_info")
@patch("services.crypto_transaction.get_crypto_price")
def test_get_crypto_account_summary(mock_price, mock_info, session: Session, master_key: str):
    mock_info.side_effect = lambda s, symbol: {
        "BTC": ("Bitcoin", Decimal("40000.0")),
        "ETH": ("Ethereum", Decimal("3000.0")),
    }.get(symbol, ("Unknown", Decimal("0")))
    mock_price.side_effect = lambda s, symbol: Decimal("3000.0") if symbol == "ETH" else Decimal("1.0")

    account = CryptoAccount(uuid="acc_main_crypto", user_uuid_bidx=hash_index("user_1", master_key), name_enc=encrypt_data("My Wallet", master_key))
    session.add(account)
    session.commit()
    # BUY 1 BTC: cost = 30000 (via SPEND EUR)
    g1 = "group-btc-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g1)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g1)
    # BUY 10 ETH: cost = 20000 (via SPEND EUR)
    g2 = "group-eth-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="ETH", type=CryptoTransactionType.BUY,
        amount=Decimal("10"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)
    ), master_key, group_uuid=g2)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("20000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 2)
    ), master_key, group_uuid=g2)
    # SPEND 0.5 BTC → removes 50% of cost_basis → remaining = 15000
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="BTC", type=CryptoTransactionType.SPEND,
        amount=Decimal("0.5"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 3)
    ), master_key)
    summary = get_crypto_account_summary(session, account, master_key)
    pos_btc = next(p for p in summary.positions if p.symbol == "BTC")
    assert pos_btc.total_amount == Decimal("0.5")
    assert pos_btc.total_invested == Decimal("15000")
    assert pos_btc.current_value == Decimal("20000")
    assert pos_btc.profit_loss == Decimal("5000")

    pos_eth = next(p for p in summary.positions if p.symbol == "ETH")
    assert pos_eth.total_amount == Decimal("10")
    assert pos_eth.total_invested == Decimal("20000")
    assert pos_eth.current_value == Decimal("30000")

    # Over-spend safety: ETH position should be filtered out
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", symbol="ETH", type=CryptoTransactionType.SPEND,
        amount=Decimal("15"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 4)
    ), master_key)
    summary_safety = get_crypto_account_summary(session, account, master_key)
    pos_eth_safety = next((p for p in summary_safety.positions if p.symbol == "ETH"), None)
    assert pos_eth_safety is None


def test_create_composite_transaction_eur_only(session: Session, master_key: str):
    """BUY BTC with EUR (fees included): BUY BTC(price=0) + SPEND EUR(price=1).
    No FIAT_ANCHOR needed — SPEND EUR is the cost anchor."""
    account = CryptoAccount(uuid="acc_comp_eur", user_uuid_bidx=hash_index("u_comp", master_key), name_enc=encrypt_data("Comp", master_key))
    session.add(account)
    session.commit()
    data = CryptoCompositeTransactionCreate(
        account_id="acc_comp_eur",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("3000"),
        executed_at=datetime(2023, 6, 1),
        quote_symbol="EUR",
        quote_amount=Decimal("3000"),
        fee_included=True,
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 2  # BUY BTC + SPEND EUR
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND"}
    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    assert buy_row.symbol == "BTC"
    assert spend_row.symbol == "EUR"
    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0
    assert spend_row.price_per_unit == Decimal("1")  # EUR is fiat
    assert buy_row.group_uuid is not None
    assert buy_row.group_uuid == spend_row.group_uuid


def test_create_composite_transaction_with_crypto_quote(session: Session, master_key: str):
    """BUY BTC with USDC: BUY(price=0) + SPEND USDC(price=0) + FIAT_ANCHOR(EUR).
    FIAT_ANCHOR carries the EUR value of the trade."""
    account = CryptoAccount(uuid="acc_comp_swap", user_uuid_bidx=hash_index("u_swap", master_key), name_enc=encrypt_data("Swap", master_key))
    session.add(account)
    session.commit()
    data = CryptoCompositeTransactionCreate(
        account_id="acc_comp_swap",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("2760"),
        executed_at=datetime(2023, 6, 2),
        quote_symbol="USDC",
        quote_amount=Decimal("3000"),
        fee_included=True,
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 3  # BUY BTC + SPEND USDC + FIAT_ANCHOR EUR
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR"}
    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    anchor_row = next(r for r in rows if r.type == "FIAT_ANCHOR")
    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0
    assert spend_row.symbol == "USDC"
    assert spend_row.price_per_unit == Decimal("0")  # crypto → price = 0
    assert anchor_row.symbol == "EUR"
    assert anchor_row.amount == Decimal("2760")
    assert len({r.group_uuid for r in rows}) == 1


def test_create_composite_transaction_with_eur_fee_not_included(session: Session, master_key: str):
    """BUY BTC with EUR, external EUR fee → 2 rows: BUY BTC(price=0) + SPEND EUR (fee merged).
    SPEND EUR carries 3000 + 3.1 = 3003.1 (total cost). No FIAT_ANCHOR needed."""
    account = CryptoAccount(uuid="acc_comp_fee", user_uuid_bidx=hash_index("u_fee", master_key), name_enc=encrypt_data("Fee", master_key))
    session.add(account)
    session.commit()
    data = CryptoCompositeTransactionCreate(
        account_id="acc_comp_fee",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("3000"),
        executed_at=datetime(2023, 6, 3),
        quote_symbol="EUR",
        quote_amount=Decimal("3000"),
        fee_included=False,
        fee_eur=Decimal("3.1"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 2  # BUY BTC + SPEND EUR (fee merged into SPEND amount)
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND"}
    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    assert spend_row.symbol == "EUR"
    # SPEND carries full cost including fee: 3000 + 3.1 = 3003.1
    assert spend_row.amount == Decimal("3003.1")
    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0
    assert buy_row.group_uuid == spend_row.group_uuid


def test_create_composite_transaction_crypto_quote_with_external_fee(session: Session, master_key: str):
    """BUY BTC with USDC + EUR fee (not included) → 3 rows: BUY + SPEND(USDC) + FIAT_ANCHOR(EUR).
    FIAT_ANCHOR carries total cost: 2760 + 3.1 = 2763.1."""
    account = CryptoAccount(uuid="acc_comp_full", user_uuid_bidx=hash_index("u_full", master_key), name_enc=encrypt_data("Full", master_key))
    session.add(account)
    session.commit()
    data = CryptoCompositeTransactionCreate(
        account_id="acc_comp_full",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("2760"),
        executed_at=datetime(2023, 6, 4),
        quote_symbol="USDC",
        quote_amount=Decimal("3000"),
        fee_included=False,
        fee_eur=Decimal("3.1"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 3  # BUY + SPEND(USDC) + FIAT_ANCHOR(EUR)
    group_ids = {r.group_uuid for r in rows}
    assert len(group_ids) == 1
    type_counts = {r.type: r for r in rows}
    assert "BUY" in type_counts
    assert "SPEND" in type_counts
    assert "FIAT_ANCHOR" in type_counts
    anchor = type_counts["FIAT_ANCHOR"]
    assert anchor.symbol == "EUR"
    assert anchor.amount == Decimal("2763.1")  # total cost: 2760 + 3.1
    assert type_counts["BUY"].price_per_unit == Decimal("0")  # crypto → 0
    assert type_counts["SPEND"].price_per_unit == Decimal("0")  # crypto → 0


@patch("services.crypto_transaction.get_crypto_info")
def test_get_crypto_account_summary_with_fee_row(mock_info, session: Session, master_key: str):
    """A FEE-type row reduces amount. With price=0 on crypto FEE, fees_eur = 0."""
    mock_info.return_value = ("Bitcoin", Decimal("30000"))
    account = CryptoAccount(uuid="acc_fee_row", user_uuid_bidx=hash_index("u_fr", master_key), name_enc=encrypt_data("FR", master_key))
    session.add(account)
    session.commit()
    g = "group-fee-test"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_fee_row", symbol="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_fee_row", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_fee_row", symbol="BTC", type=CryptoTransactionType.FEE,
        amount=Decimal("0.001"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)
    ), master_key)
    summary = get_crypto_account_summary(session, account, master_key)
    pos = next(p for p in summary.positions if p.symbol == "BTC")
    assert pos.total_amount == Decimal("0.999")
    assert summary.total_fees == Decimal("0")  # crypto FEE price=0 → fees_eur=0


@patch("services.crypto_transaction.get_crypto_info")
def test_get_crypto_account_summary_reward(mock_info, session: Session, master_key: str):
    """A REWARD row increases amount but does NOT add to total_invested."""
    mock_info.return_value = ("Ethereum", Decimal("3000"))
    account = CryptoAccount(uuid="acc_staking", user_uuid_bidx=hash_index("u_stk", master_key), name_enc=encrypt_data("Stk", master_key))
    session.add(account)
    session.commit()
    g = "group-eth-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_staking", symbol="ETH", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_staking", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("3000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_staking", symbol="ETH", type=CryptoTransactionType.REWARD,
        amount=Decimal("0.1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)
    ), master_key)
    summary = get_crypto_account_summary(session, account, master_key)
    pos = next(p for p in summary.positions if p.symbol == "ETH")
    assert pos.total_amount == Decimal("1.1")
    assert pos.total_invested == Decimal("3000")  # reward not counted as invested


def test_get_crypto_account_summary_empty(session: Session, master_key: str):
    account = CryptoAccount(uuid="acc_empty", user_uuid_bidx=hash_index("u1", master_key), name_enc=encrypt_data("Empty", master_key))
    session.add(account)
    session.commit()
    summary = get_crypto_account_summary(session, account, master_key)
    assert summary.total_invested == Decimal("0")
    assert summary.total_fees == Decimal("0")
    assert summary.current_value is None
    assert summary.profit_loss is None
    assert len(summary.positions) == 0


@patch("services.crypto_transaction.get_crypto_info")
def test_group_based_pru(mock_info, session: Session, master_key: str):
    """FIAT_ANCHOR is the authoritative EUR cost for a BUY group.

    Scenario:
      BUY 0.5 BTC (price=0)           (group_A)
      SPEND 20 ETH (price=0)          (group_A) → balance update only
      FEE 0.005 BNB (price=0)         (group_A) → balance update only
      FIAT_ANCHOR EUR 30002 (price=1)  (group_A) → cost anchor

      ACB = FIAT_ANCHOR.amount = 30002 EUR
      average_buy_price = 30002 / 0.5 = 60004 EUR/BTC
    """
    mock_info.return_value = ("Bitcoin", Decimal("70000"))

    account = CryptoAccount(
        uuid="acc_group_pru",
        user_uuid_bidx=hash_index("u_gp", master_key),
        name_enc=encrypt_data("GP", master_key),
    )
    session.add(account)
    session.commit()

    from services.crypto_transaction import create_crypto_transaction
    group_a = "group-pru-test"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_group_pru", symbol="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("0.5"), price_per_unit=Decimal("0"),
        executed_at=datetime(2024, 1, 1),
    ), master_key, group_uuid=group_a)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_group_pru", symbol="ETH", type=CryptoTransactionType.SPEND,
        amount=Decimal("20"), price_per_unit=Decimal("0"),
        executed_at=datetime(2024, 1, 1),
    ), master_key, group_uuid=group_a)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_group_pru", symbol="BNB", type=CryptoTransactionType.FEE,
        amount=Decimal("0.005"), price_per_unit=Decimal("0"),
        executed_at=datetime(2024, 1, 1),
    ), master_key, group_uuid=group_a)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_group_pru", symbol="EUR", type=CryptoTransactionType.FIAT_ANCHOR,
        amount=Decimal("30002"), price_per_unit=Decimal("1"),
        executed_at=datetime(2024, 1, 1),
    ), master_key, group_uuid=group_a)

    summary = get_crypto_account_summary(session, account, master_key)
    pos_btc = next(p for p in summary.positions if p.symbol == "BTC")

    # ACB = FIAT_ANCHOR.amount = 30002
    assert pos_btc.average_buy_price == Decimal("60004")
    assert pos_btc.total_invested == Decimal("30002")


def test_crypto_deposit_creates_fiat_anchor_and_buy(session: Session, master_key: str):
    """CRYPTO_DEPOSIT creates a FIAT_ANCHOR(EUR) + BUY row sharing the same group."""
    account = CryptoAccount(
        uuid="acc_cdeposit",
        user_uuid_bidx=hash_index("u_cd", master_key),
        name_enc=encrypt_data("CDeposit", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cdeposit",
        symbol="BTC",
        type="CRYPTO_DEPOSIT",
        amount=Decimal("0.5"),
        eur_amount=Decimal("15000"),        # original EUR cost
        fee_included=True,                  # no extra fee
        executed_at=datetime(2024, 3, 1),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 2
    types = {r.type for r in rows}
    assert types == {"FIAT_ANCHOR", "BUY"}
    anchor = next(r for r in rows if r.type == "FIAT_ANCHOR")
    buy = next(r for r in rows if r.type == "BUY")
    assert anchor.symbol == "EUR"
    assert anchor.amount == Decimal("15000")
    assert buy.symbol == "BTC"
    assert buy.amount == Decimal("0.5")
    # PRU = 15000 / 0.5 = 30000
    assert buy.price_per_unit == Decimal("0")  # crypto → price = 0
    assert anchor.group_uuid == buy.group_uuid


def test_crypto_deposit_with_external_fee_inflates_anchor(session: Session, master_key: str):
    """CRYPTO_DEPOSIT fee_included=False → FIAT_ANCHOR carries eur_amount + fee_eur."""
    account = CryptoAccount(
        uuid="acc_cdfee",
        user_uuid_bidx=hash_index("u_cdf", master_key),
        name_enc=encrypt_data("CDFee", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cdfee",
        symbol="ETH",
        type="CRYPTO_DEPOSIT",
        amount=Decimal("10"),
        eur_amount=Decimal("20000"),     # asset cost
        fee_included=False,
        fee_eur=Decimal("50"),           # transfer fee paid separately
        executed_at=datetime(2024, 3, 2),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 2
    anchor = next(r for r in rows if r.type == "FIAT_ANCHOR")
    buy = next(r for r in rows if r.type == "BUY")
    # Total cost = 20000 + 50 = 20050
    assert anchor.amount == Decimal("20050")
    # PRU = 20050 / 10 = 2005
    assert buy.price_per_unit == Decimal("0")  # crypto → price = 0


@patch("services.crypto_transaction.get_crypto_info")
def test_fiat_anchor_not_counted_in_positions(mock_info, session: Session, master_key: str):
    """FIAT_ANCHOR rows are not counted in any position's balance."""
    mock_info.return_value = ("Bitcoin", Decimal("30000"))
    account = CryptoAccount(
        uuid="acc_anchor_pos",
        user_uuid_bidx=hash_index("u_ap", master_key),
        name_enc=encrypt_data("AP", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_anchor_pos",
        symbol="BTC",
        type="CRYPTO_DEPOSIT",
        amount=Decimal("1"),
        eur_amount=Decimal("25000"),
        fee_included=True,
        executed_at=datetime(2024, 3, 3),
    )
    create_composite_crypto_transaction(session, data, master_key)
    summary = get_crypto_account_summary(session, account, master_key)

    # Should have exactly one position: BTC
    assert len(summary.positions) == 1
    pos = summary.positions[0]
    assert pos.symbol == "BTC"
    assert pos.total_amount == Decimal("1")
    # cost_basis comes from group PRU (FIAT_ANCHOR is cost leg)
    assert pos.total_invested == Decimal("25000")


@patch("services.crypto_transaction.get_crypto_info")
def test_transfer_neutral_proportional_removal(mock_info, session: Session, master_key: str):
    """TRANSFER reduces amount + cost_basis proportionally (no tax event)."""
    mock_info.return_value = ("Ethereum", Decimal("3000"))
    account = CryptoAccount(
        uuid="acc_transfer",
        user_uuid_bidx=hash_index("u_tr", master_key),
        name_enc=encrypt_data("TR", master_key),
    )
    session.add(account)
    session.commit()

    # BUY 4 ETH (price=0): cost = 8000 via SPEND EUR
    g = "group-eth-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_transfer", symbol="ETH", type=CryptoTransactionType.BUY,
        amount=Decimal("4"), price_per_unit=Decimal("0"), executed_at=datetime(2024, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_transfer", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("8000"), price_per_unit=Decimal("1"), executed_at=datetime(2024, 1, 1)
    ), master_key, group_uuid=g)
    # TRANSFER 1 ETH → remove 25% of position
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_transfer", symbol="ETH", type=CryptoTransactionType.TRANSFER,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2024, 1, 2)
    ), master_key)

    summary = get_crypto_account_summary(session, account, master_key)
    pos = next(p for p in summary.positions if p.symbol == "ETH")
    assert pos.total_amount == Decimal("3")
    assert pos.total_invested == Decimal("6000")  # 8000 - 25% = 6000


@patch("services.crypto_transaction.get_crypto_info")
def test_exit_proportional_removal(mock_info, session: Session, master_key: str):
    """EXIT reduces amount + cost_basis proportionally (taxable outbound)."""
    mock_info.return_value = ("Bitcoin", Decimal("40000"))
    account = CryptoAccount(
        uuid="acc_exit",
        user_uuid_bidx=hash_index("u_ex", master_key),
        name_enc=encrypt_data("EX", master_key),
    )
    session.add(account)
    session.commit()

    g = "group-btc-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_exit", symbol="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2024, 1, 1)
    ), master_key, group_uuid=g)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_exit", symbol="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2024, 1, 1)
    ), master_key, group_uuid=g)
    # EXIT keeps its price (fiat-type, taxable)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_exit", symbol="BTC", type=CryptoTransactionType.EXIT,
        amount=Decimal("0.5"), price_per_unit=Decimal("40000"), executed_at=datetime(2024, 1, 2)
    ), master_key)

    summary = get_crypto_account_summary(session, account, master_key)
    pos = next(p for p in summary.positions if p.symbol == "BTC")
    assert pos.total_amount == Decimal("0.5")
    assert pos.total_invested == Decimal("15000")  # 30000 - 50%


def test_composite_transfer_single_row(session: Session, master_key: str):
    """Composite TRANSFER creates exactly one TRANSFER atomic row."""
    account = CryptoAccount(
        uuid="acc_ctransfer",
        user_uuid_bidx=hash_index("u_ctr", master_key),
        name_enc=encrypt_data("CTR", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_ctransfer",
        symbol="SOL",
        type="TRANSFER",
        amount=Decimal("5"),
        executed_at=datetime(2024, 4, 1),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 1
    assert rows[0].type == "TRANSFER"
    assert rows[0].symbol == "SOL"
    assert rows[0].amount == Decimal("5")


def test_composite_fiat_deposit_single_row(session: Session, master_key: str):
    """Composite FIAT_DEPOSIT creates exactly one FIAT_DEPOSIT atomic row."""
    account = CryptoAccount(
        uuid="acc_cfiat",
        user_uuid_bidx=hash_index("u_cf", master_key),
        name_enc=encrypt_data("CFiat", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cfiat",
        symbol="EUR",
        type="FIAT_DEPOSIT",
        amount=Decimal("1000"),
        executed_at=datetime(2024, 4, 2),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 1
    assert rows[0].type == "FIAT_DEPOSIT"


def test_composite_reward_single_row(session: Session, master_key: str):
    """Composite REWARD creates a single REWARD row with price_per_unit = 0."""
    account = CryptoAccount(
        uuid="acc_creward",
        user_uuid_bidx=hash_index("u_cr", master_key),
        name_enc=encrypt_data("CRew", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_creward",
        symbol="ETH",
        type="REWARD",
        amount=Decimal("0.05"),
        executed_at=datetime(2024, 4, 3),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 1
    assert rows[0].type == "REWARD"
    assert rows[0].price_per_unit == Decimal("0")


# ─── Crypto fee token tests ──────────────────────────────────────────────────

def test_eur_quote_crypto_fee_included(session: Session, master_key: str):
    """BUY BTC with EUR, fee paid in BNB (fee_included=True).

    eur_amount=3000. FEE row price=0 (balance-only). BUY price=0.
    SPEND EUR stays at face value. No FIAT_ANCHOR (fee_included + EUR quote).
    """
    account = CryptoAccount(
        uuid="acc_cfi_1",
        user_uuid_bidx=hash_index("u_cfi1", master_key),
        name_enc=encrypt_data("CFI1", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cfi_1",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("3000"),
        executed_at=datetime(2024, 5, 1),
        quote_symbol="EUR",
        quote_amount=Decimal("3000"),
        fee_included=True,
        fee_percentage=Decimal("0.1"),
        fee_symbol="BNB",
        fee_amount=Decimal("0.01"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 3  # BUY BTC + SPEND EUR + FEE BNB
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND", "FEE"}

    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    fee_row = next(r for r in rows if r.type == "FEE")

    # FEE row: balance-only, price=0
    assert fee_row.symbol == "BNB"
    assert fee_row.amount == Decimal("0.01")
    assert fee_row.price_per_unit == Decimal("0")

    # SPEND EUR: not inflated
    assert spend_row.symbol == "EUR"
    assert spend_row.amount == Decimal("3000")

    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0

    # All same group
    assert len({r.group_uuid for r in rows}) == 1


def test_eur_quote_crypto_fee_not_included_percentage(session: Session, master_key: str):
    """BUY BTC with EUR, fee in BNB (fee_included=False, fee_percentage=0.1%).

    eur_amount=3000. fee_eur = 3000 × 0.1% = 3 EUR.
    FIAT_ANCHOR carries total cost: 3000 + 3 = 3003.
    FEE BNB price=0. BUY price=0. SPEND EUR at face value.
    """
    account = CryptoAccount(
        uuid="acc_cfni_1",
        user_uuid_bidx=hash_index("u_cfni1", master_key),
        name_enc=encrypt_data("CFNI1", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cfni_1",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("3000"),
        executed_at=datetime(2024, 5, 2),
        quote_symbol="EUR",
        quote_amount=Decimal("3000"),
        fee_included=False,
        fee_percentage=Decimal("0.1"),
        fee_symbol="BNB",
        fee_amount=Decimal("0.01"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 4  # BUY + SPEND EUR + FIAT_ANCHOR + FEE BNB
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR", "FEE"}

    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    fee_row = next(r for r in rows if r.type == "FEE")
    anchor_row = next(r for r in rows if r.type == "FIAT_ANCHOR")

    assert fee_row.symbol == "BNB"
    assert fee_row.price_per_unit == Decimal("0")  # crypto → price = 0

    assert spend_row.amount == Decimal("3000")  # not inflated (crypto fee)
    assert spend_row.price_per_unit == Decimal("1")

    assert anchor_row.symbol == "EUR"
    assert anchor_row.amount == Decimal("3003")  # 3000 + 3

    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0

    assert len({r.group_uuid for r in rows}) == 1


def test_crypto_quote_crypto_fee_not_included_percentage(session: Session, master_key: str):
    """BUY BTC with USDC, fee in BNB (fee_included=False, fee_percentage=0.1%).

    eur_amount=2760. fee_eur = 2760 × 0.1% = 2.76.
    FIAT_ANCHOR EUR = 2760 + 2.76 = 2762.76 (total cost).
    All crypto rows price=0.
    """
    account = CryptoAccount(
        uuid="acc_cfni_2",
        user_uuid_bidx=hash_index("u_cfni2", master_key),
        name_enc=encrypt_data("CFNI2", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_cfni_2",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("2760"),
        executed_at=datetime(2024, 5, 3),
        quote_symbol="USDC",
        quote_amount=Decimal("3000"),
        fee_included=False,
        fee_percentage=Decimal("0.1"),
        fee_symbol="BNB",
        fee_amount=Decimal("0.01"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 4  # BUY + SPEND(USDC) + FIAT_ANCHOR + FEE(BNB)
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR", "FEE"}

    buy_row = next(r for r in rows if r.type == "BUY")
    spend_row = next(r for r in rows if r.type == "SPEND")
    fee_row = next(r for r in rows if r.type == "FEE")
    anchor_row = next(r for r in rows if r.type == "FIAT_ANCHOR")

    assert fee_row.symbol == "BNB"
    assert fee_row.price_per_unit == Decimal("0")  # crypto → price = 0

    assert spend_row.symbol == "USDC"
    assert spend_row.amount == Decimal("3000")
    assert spend_row.price_per_unit == Decimal("0")  # crypto → price = 0

    assert anchor_row.symbol == "EUR"
    assert anchor_row.amount == Decimal("2762.76")  # 2760 + 2.76

    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0

    assert len({r.group_uuid for r in rows}) == 1


def test_crypto_fee_explicit_eur_takes_priority_over_percentage(session: Session, master_key: str):
    """When both fee_eur and fee_percentage are provided, fee_eur wins.
    FIAT_ANCHOR carries total cost (base 3000 + fee 5 = 3005)."""
    account = CryptoAccount(
        uuid="acc_fee_prio",
        user_uuid_bidx=hash_index("u_fp", master_key),
        name_enc=encrypt_data("FP", master_key),
    )
    session.add(account)
    session.commit()

    data = CryptoCompositeTransactionCreate(
        account_id="acc_fee_prio",
        symbol="BTC",
        type="BUY",
        amount=Decimal("0.1"),
        eur_amount=Decimal("3000"),
        executed_at=datetime(2024, 5, 4),
        quote_symbol="EUR",
        quote_amount=Decimal("3000"),
        fee_included=False,
        fee_eur=Decimal("5"),          # explicit → 5 EUR
        fee_percentage=Decimal("0.1"), # would give 3 EUR — must be ignored
        fee_symbol="BNB",
        fee_amount=Decimal("0.01"),
    )
    rows = create_composite_crypto_transaction(session, data, master_key)
    assert len(rows) == 4  # BUY + SPEND EUR + FIAT_ANCHOR + FEE BNB
    types = {r.type for r in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR", "FEE"}

    fee_row = next(r for r in rows if r.type == "FEE")
    assert fee_row.price_per_unit == Decimal("0")  # crypto → price = 0

    anchor_row = next(r for r in rows if r.type == "FIAT_ANCHOR")
    assert anchor_row.amount == Decimal("3005")  # 3000 + 5 (fee_eur wins)

    buy_row = next(r for r in rows if r.type == "BUY")
    assert buy_row.price_per_unit == Decimal("0")  # crypto → price = 0


def test_bulk_create_with_group_uuid_pru(session: Session, master_key: str):
    """Bulk-imported atomic rows with group_uuid are costed correctly.

    BUY BTC (price=0) + SPEND EUR (price=1) share a group_uuid →
    PRU = 3000 / 0.1 = 30 000, not 0.
    """
    from unittest.mock import patch
    from services.crypto_account import get_crypto_account as _get_acc
    account = CryptoAccount(
        uuid="acc_bulk_grp",
        user_uuid_bidx=hash_index("u_bulk_grp", master_key),
        name_enc=encrypt_data("Bulk Group", master_key),
    )
    session.add(account)
    session.commit()

    group = "bulk-grp-uuid-abc"
    create_crypto_transaction(
        session,
        CryptoTransactionCreate(
            account_id="acc_bulk_grp",
            symbol="BTC",
            type=CryptoTransactionType.BUY,
            amount=Decimal("0.1"),
            price_per_unit=Decimal("0"),
            executed_at=datetime(2024, 1, 1),
        ),
        master_key,
        group_uuid=group,
    )
    create_crypto_transaction(
        session,
        CryptoTransactionCreate(
            account_id="acc_bulk_grp",
            symbol="EUR",
            type=CryptoTransactionType.SPEND,
            amount=Decimal("3000"),
            price_per_unit=Decimal("1"),
            executed_at=datetime(2024, 1, 1),
        ),
        master_key,
        group_uuid=group,
    )

    with patch("services.crypto_transaction.get_crypto_info", return_value=("Bitcoin", Decimal("40000"))):
        with patch("services.crypto_transaction.get_crypto_price", return_value=Decimal("40000")):
            summary = get_crypto_account_summary(session, account, master_key)

    btc = next(p for p in summary.positions if p.symbol == "BTC")
    assert btc.total_amount == Decimal("0.1")
    assert btc.total_invested == Decimal("3000.00")
    assert btc.average_buy_price == Decimal("30000.0000")


