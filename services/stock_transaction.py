"""Stock transaction services."""

from decimal import Decimal
from datetime import datetime, timedelta, date

from sqlmodel import Session, select

from models import StockAccount, StockTransaction
from models.market import MarketAsset
from models.enums import AssetType
from dtos import (
    StockTransactionCreate,
    StockTransactionUpdate,
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_stock_info, get_or_create_market_asset


def _decrypt_transaction(tx: StockTransaction, master_key: str) -> TransactionResponse:
    """Decrypt a StockTransaction and return a response with calculated totals."""
    isin = decrypt_data(tx.isin_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    fees = Decimal(decrypt_data(tx.fees_enc, master_key))
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str.replace("Z", "+00:00"))
    except ValueError:
        executed_at = tx.created_at

    notes = decrypt_data(tx.notes_enc, master_key) if tx.notes_enc else None

    if type_str == "DEPOSIT" and isin == "EUR":
        total_cost = amount - fees
        fees_pct = (fees / amount * 100) if amount > 0 else Decimal("0")
    else:
        total_cost = (amount * price) + fees
        fees_pct = (fees / total_cost * 100) if total_cost > 0 else Decimal("0")

    symbol = "EUR" if isin == "EUR" else None
    name = "Euros" if isin == "EUR" else None

    return TransactionResponse(
        id=tx.uuid,
        isin=isin,
        symbol=symbol,
        name=name,
        exchange=None,
        type=type_str,
        amount=amount,
        price_per_unit=price,
        fees=fees,
        executed_at=executed_at,
        notes=notes,
        total_cost=total_cost,
        fees_percentage=round(fees_pct, 2),
    )


def create_eur_deposit(
    session: Session,
    account_uuid: str,
    amount: Decimal,
    executed_at: datetime,
    master_key: str,
    notes: str | None = None,
    fees: Decimal = Decimal("0"),
) -> TransactionResponse:
    """Record a EUR cash deposit into a stock account.
    
    Uses type=DEPOSIT + isin=EUR as sentinel: price_per_unit=1 (EUR is source of
    truth, no market call needed).
    """
    account_bidx = hash_index(account_uuid, master_key)

    transaction = StockTransaction(
        account_id_bidx=account_bidx,
        isin_enc=encrypt_data("EUR", master_key),
        type_enc=encrypt_data("DEPOSIT", master_key),
        amount_enc=encrypt_data(str(amount), master_key),
        price_per_unit_enc=encrypt_data("1", master_key),
        fees_enc=encrypt_data(str(fees), master_key),
        executed_at_enc=encrypt_data(executed_at.isoformat(), master_key),
        notes_enc=encrypt_data(notes, master_key) if notes else None,
    )
    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    return TransactionResponse(
        id=transaction.uuid,
        isin="EUR",
        symbol="EUR",
        name="Euros",
        exchange=None,
        type="DEPOSIT",
        amount=amount,
        price_per_unit=Decimal("1"),
        fees=fees,
        executed_at=executed_at,
        total_cost=amount - fees,
        fees_percentage=round((fees / amount * 100), 2) if amount > 0 else Decimal("0"),
    )


def _compute_eur_balance(session: Session, account_uuid: str, master_key: str) -> Decimal:
    txs = get_account_transactions(session, account_uuid, master_key)
    txs.sort(key=lambda x: x.executed_at)
    eur = Decimal("0")
    for tx in txs:
        if tx.type == "DEPOSIT" and tx.isin == "EUR":
            eur += tx.amount - tx.fees
        elif tx.type == "BUY" and tx.isin != "EUR":
            eur -= (tx.amount * tx.price_per_unit) + tx.fees
        elif tx.type == "DIVIDEND":
            eur += (tx.amount * tx.price_per_unit) - tx.fees
        elif tx.type == "SELL" and tx.isin != "EUR":
            eur += (tx.amount * tx.price_per_unit) - tx.fees
    return eur


def _compute_held_quantity(session: Session, account_uuid: str, isin: str, master_key: str) -> Decimal:
    """Return the net quantity currently held for a given ISIN in an account."""
    txs = get_account_transactions(session, account_uuid, master_key)
    held = Decimal("0")
    for tx in txs:
        if tx.isin != isin:
            continue
        if tx.type == "BUY":
            held += tx.amount
        elif tx.type == "SELL":
            held -= tx.amount
    return held


def _compute_held_quantity_by_bidx(
    session: Session, account_id_bidx: str, isin: str, master_key: str,
    exclude_tx_uuid: str | None = None,
) -> Decimal:
    """Same as _compute_held_quantity but works directly from the stored bidx.
    
    Used in update_stock_transaction where we have the bidx but not the UUID.
    exclude_tx_uuid allows discounting the current transaction being edited.
    """
    raw_txs = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_id_bidx)
    ).all()
    held = Decimal("0")
    for raw in raw_txs:
        if exclude_tx_uuid and raw.uuid == exclude_tx_uuid:
            continue
        tx_type = decrypt_data(raw.type_enc, master_key)
        tx_isin = decrypt_data(raw.isin_enc, master_key)
        if tx_isin != isin:
            continue
        tx_amount = Decimal(decrypt_data(raw.amount_enc, master_key))
        if tx_type == "BUY":
            held += tx_amount
        elif tx_type == "SELL":
            held -= tx_amount
    return held


def create_stock_transaction(
    session: Session,
    data: StockTransactionCreate,
    master_key: str,
) -> TransactionResponse:
    """Create a new encrypted stock transaction.

    For BUY transactions: if the account has insufficient EUR cash, an automatic
    EUR deposit is created first to cover the shortfall (without bank deduction).
    """
    if data.isin:
        data.isin = data.isin.strip()
    if data.symbol:
        data.symbol = data.symbol.strip()

    # Validate SELL quantity against current position
    if data.type.value == "SELL" and data.isin != "EUR":
        held = _compute_held_quantity(session, data.account_id, data.isin, master_key)
        if data.amount > held:
            raise ValueError(
                f"Quantité vendue ({data.amount}) supérieure à la position détenue ({round(held, 8)})"
            )

    # Auto-fund EUR balance for BUY transactions if needed
    if data.type.value == "BUY" and data.isin != "EUR":
        cost = (data.amount * data.price_per_unit) + data.fees
        current_eur = max(_compute_eur_balance(session, data.account_id, master_key), Decimal("0"))
        shortage = cost - current_eur
        if shortage > Decimal("0"):
            # Auto-deposit happens 1 second before the BUY so it's replayed first
            deposit_time = data.executed_at - timedelta(seconds=1)
            create_eur_deposit(
                session, data.account_id, round(shortage, 2), deposit_time, master_key,
                notes="Provision automatique",
            )

    account_bidx = hash_index(data.account_id, master_key)
    isin_enc = encrypt_data(data.isin, master_key)
    type_enc = encrypt_data(data.type.value, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    price_enc = encrypt_data(str(data.price_per_unit), master_key)
    fees_enc = encrypt_data(str(data.fees), master_key)
    exec_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    

    mp = session.exec(select(MarketAsset).where(MarketAsset.isin == data.isin)).first()
    if not mp:
        mp = get_or_create_market_asset(
            session, data.isin, AssetType.STOCK, symbol_hint=data.symbol or None
        )
    if not mp and data.isin and data.symbol:
        # Fallback: persist a minimal MarketAsset so the symbol survives across requests
        # (happens when external market API is unavailable or the ISIN is unknown)
        mp = MarketAsset(
            isin=data.isin,
            symbol=data.symbol,
            name=data.name or data.symbol,
            asset_type=AssetType.STOCK,
        )
        session.add(mp)
        try:
            session.commit()
            session.refresh(mp)
        except Exception:
            session.rollback()
            mp = session.exec(select(MarketAsset).where(MarketAsset.isin == data.isin)).first()
    elif mp and data.symbol and not mp.symbol:
        mp.symbol = data.symbol
        session.add(mp)
        session.commit()

    notes_enc = None
    if data.notes:
        notes_enc = encrypt_data(data.notes, master_key)

    transaction = StockTransaction(
        account_id_bidx=account_bidx,
        isin_enc=isin_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        fees_enc=fees_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc,
    )

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    resp = _decrypt_transaction(transaction, master_key)
    if resp.isin:
        if data.symbol:
            resp.symbol = data.symbol
        if data.exchange:
            resp.exchange = data.exchange
        if data.name:
            resp.name = data.name
        
        if not resp.symbol or not resp.exchange or not resp.name:
             mp = session.exec(select(MarketAsset).where(MarketAsset.isin == resp.isin)).first()
             if mp:
                 if not resp.symbol: resp.symbol = mp.symbol
                 if not resp.exchange: resp.exchange = mp.exchange
                 if not resp.name: resp.name = mp.name
    return resp


def get_stock_transaction(
    session: Session,
    transaction_uuid: str,
    master_key: str,
) -> TransactionResponse | None:
    """Get a single transaction by UUID."""
    transaction = session.get(StockTransaction, transaction_uuid)
    if not transaction:
        return None
    resp = _decrypt_transaction(transaction, master_key)
    
    mp = session.exec(select(MarketAsset).where(MarketAsset.isin == resp.isin)).first()
    if mp:
        resp.symbol = mp.symbol
        resp.exchange = mp.exchange
        resp.name = mp.name
            
    return resp


def update_stock_transaction(
    session: Session,
    transaction: StockTransaction,
    data: StockTransactionUpdate,
    master_key: str,
) -> TransactionResponse:
    """Update an existing stock transaction (only provided fields)."""
    if data.isin:
        data.isin = data.isin.strip()
    if data.symbol:
        data.symbol = data.symbol.strip()

    # Validate SELL quantity: compute held quantity excluding this transaction itself
    current = _decrypt_transaction(transaction, master_key)
    effective_type = data.type.value if data.type is not None else current.type
    effective_isin = data.isin if data.isin is not None else current.isin
    effective_amount = data.amount if data.amount is not None else current.amount

    if effective_type == "SELL" and effective_isin and effective_isin != "EUR":
        # Compute held quantity without this transaction so we can re-validate cleanly
        held = _compute_held_quantity_by_bidx(
            session, transaction.account_id_bidx, effective_isin, master_key,
            exclude_tx_uuid=transaction.uuid,
        )
        if effective_amount > held:
            raise ValueError(
                f"Quantité vendue ({effective_amount}) supérieure à la position détenue ({round(held, 8)})"
            )

    if data.isin is not None:
        transaction.isin_enc = encrypt_data(data.isin, master_key) if data.isin else None
        
    if data.type is not None:
        transaction.type_enc = encrypt_data(data.type.value, master_key)
    if data.amount is not None:
        transaction.amount_enc = encrypt_data(str(data.amount), master_key)
    if data.price_per_unit is not None:
        transaction.price_per_unit_enc = encrypt_data(str(data.price_per_unit), master_key)
    if data.fees is not None:
        transaction.fees_enc = encrypt_data(str(data.fees), master_key)
    if data.executed_at is not None:
        transaction.executed_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    if data.notes is not None:
        transaction.notes_enc = encrypt_data(data.notes, master_key)

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    resp = _decrypt_transaction(transaction, master_key)
    if resp.isin:
        mp = session.exec(select(MarketAsset).where(MarketAsset.isin == resp.isin)).first()
        if mp:
            resp.symbol = mp.symbol
            resp.exchange = mp.exchange
            resp.name = mp.name
            
    return resp


def delete_stock_transaction(session: Session, transaction_uuid: str) -> bool:
    """Delete a transaction by UUID."""
    transaction = session.get(StockTransaction, transaction_uuid)
    if not transaction:
        return False

    session.delete(transaction)
    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> list[TransactionResponse]:
    """Get all transactions for a specific account with enriched market data."""
    account_bidx = hash_index(account_uuid, master_key)

    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
    ).all()

    decoded_transactions = [_decrypt_transaction(tx, master_key) for tx in transactions]
    
    # Use ISIN for reliable market data lookup
    isins = {tx.isin for tx in decoded_transactions if tx.isin}
    
    market_map = {}
    if isins:
        market_assets = session.exec(
            select(MarketAsset).where(MarketAsset.isin.in_(isins))
        ).all()
        for mp in market_assets:
            market_map[mp.isin] = {
                "name": mp.name, 
                "symbol": mp.symbol, 
                "exchange": mp.exchange
            }
            
    for tx in decoded_transactions:
        # Prioritize locally stored name, fallback to market map
        if tx.isin and tx.isin in market_map:
            if not tx.name:
                tx.name = market_map[tx.isin]["name"]
            if not tx.symbol:
                tx.symbol = market_map[tx.isin]["symbol"]
            if not tx.exchange:
                tx.exchange = market_map[tx.isin]["exchange"]
                
    return decoded_transactions


def get_stock_account_summary(
    session: Session,
    transactions: list[TransactionResponse],
    as_of: date = None,
    db_only: bool = False,
    preloaded_prices: dict[str, Decimal] = None,
) -> AccountSummaryResponse:
    if as_of is None:
        as_of = date.today()

    transactions.sort(key=lambda x: x.executed_at)
    transactions = [tx for tx in transactions if tx.executed_at.date() <= as_of]

    total_deposits_acc = Decimal("0")

    positions_map: dict[str, dict] = {
        "EUR": {
            "isin": "EUR",
            "symbol": "EUR",
            "name": "Euros",
            "exchange": None,
            "total_amount": Decimal("0"),
            "total_cost": Decimal("0"),
            "total_buy_fees": Decimal("0"),
            "total_fees": Decimal("0"),
            "total_dividends": Decimal("0"),
        }
    }

    for tx in transactions:
        position_key = tx.isin

        if position_key not in positions_map:
            positions_map[position_key] = {
                "isin": tx.isin,
                "symbol": tx.symbol,
                "name": tx.name,
                "exchange": tx.exchange,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "total_buy_fees": Decimal("0"),
                "total_fees": Decimal("0"),
                "total_dividends": Decimal("0"),
            }

        pos = positions_map[position_key]

        if tx.symbol and not pos["symbol"]:
            pos["symbol"] = tx.symbol
        if tx.name and not pos["name"]:
            pos["name"] = tx.name
        if tx.exchange and not pos["exchange"]:
            pos["exchange"] = tx.exchange

        if tx.type == "DEPOSIT" and tx.isin == "EUR":
            net_deposit = tx.amount - tx.fees
            positions_map["EUR"]["total_amount"] += net_deposit
            total_deposits_acc += net_deposit

        elif tx.type == "BUY":
            cost = (tx.amount * tx.price_per_unit) + tx.fees
            pos["total_amount"] += tx.amount
            pos["total_cost"] += cost
            pos["total_buy_fees"] += tx.fees
            pos["total_fees"] += tx.fees
            positions_map["EUR"]["total_amount"] -= cost

        elif tx.type == "DIVIDEND":
            proceeds = (tx.amount * tx.price_per_unit) - tx.fees
            positions_map["EUR"]["total_amount"] += proceeds
            pos["total_fees"] += tx.fees
            pos["total_dividends"] += proceeds

        elif tx.type == "SELL" and pos["total_amount"] > 0:
            fraction = min(tx.amount / pos["total_amount"], Decimal("1"))
            proceeds = (tx.amount * tx.price_per_unit) - tx.fees
            pos["total_amount"] = max(pos["total_amount"] - tx.amount, Decimal("0"))
            pos["total_cost"] = max(pos["total_cost"] * (Decimal("1") - fraction), Decimal("0"))
            pos["total_fees"] += tx.fees
            positions_map["EUR"]["total_amount"] += proceeds

    positions: list[PositionResponse] = []

    for position_key, data in positions_map.items():
        isin = data.get("isin")

        if isin == "EUR":
            eur_amount = max(data["total_amount"], Decimal("0"))
            if eur_amount <= 0:
                continue
            positions.append(PositionResponse(
                symbol="EUR",
                exchange=None,
                name="Euros",
                isin="EUR",
                total_amount=eur_amount,
                average_buy_price=Decimal("1"),
                total_invested=round(eur_amount, 2),
                total_fees=Decimal("0"),
                fees_percentage=Decimal("0"),
                currency="EUR",
                current_price=Decimal("1"),
                current_value=round(eur_amount, 2),
                profit_loss=Decimal("0"),
                profit_loss_percentage=Decimal("0"),
            ))
            continue

        if data["total_amount"] <= 0:
            continue

        total_invested = data["total_cost"]
        avg_price = total_invested / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        fees_pct = (data["total_buy_fees"] / total_invested * 100) if total_invested > 0 else Decimal("0")

        if preloaded_prices is not None:
            market_name = data.get("name") or isin
            current_price = preloaded_prices.get(isin)
        else:
            market_name, current_price = (
                get_stock_info(session, isin, db_only=db_only, as_of=as_of)
                if isin
                else (None, None)
            )

        current_value = profit_loss = profit_loss_pct = None
        if current_price is not None:
            current_value = data["total_amount"] * current_price
            profit_loss = current_value - total_invested
            profit_loss_pct = (profit_loss / total_invested * 100) if total_invested > 0 else Decimal("0")

        positions.append(PositionResponse(
            symbol=data.get("symbol") or position_key,
            exchange=data.get("exchange"),
            name=market_name or data.get("name"),
            isin=isin,
            total_amount=data["total_amount"],
            average_buy_price=round(avg_price, 4),
            total_invested=round(total_invested, 2),
            total_fees=round(data["total_fees"], 2),
            fees_percentage=round(fees_pct, 2),
            total_dividends=round(data["total_dividends"], 2),
            currency="EUR",
            current_price=current_price,
            current_value=round(current_value, 2) if current_value is not None else None,
            profit_loss=round(profit_loss, 2) if profit_loss is not None else None,
            profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct is not None else None,
        ))

    stock_positions = [p for p in positions if p.isin != "EUR"]
    total_invested_acc = sum(p.total_invested for p in stock_positions)
    total_fees_acc = sum(p.total_fees for p in stock_positions)
    current_value_acc = sum(p.current_value for p in positions if p.current_value is not None)

    profit_loss_acc = profit_loss_pct_acc = None
    if any(p.current_value is not None for p in stock_positions):
        stock_value = sum(p.current_value for p in stock_positions if p.current_value is not None)
        profit_loss_acc = stock_value - total_invested_acc
        if total_invested_acc > 0:
            profit_loss_pct_acc = (profit_loss_acc / total_invested_acc * 100)

    total_dividends_acc = sum(
        v["total_dividends"] for k, v in positions_map.items() if k != "EUR"
    )

    return AccountSummaryResponse(
        total_invested=round(total_invested_acc, 2),
        total_deposits=round(total_deposits_acc, 2),
        total_fees=round(total_fees_acc, 2),
        total_dividends=round(total_dividends_acc, 2),
        current_value=round(current_value_acc, 2) if current_value_acc else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc is not None else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc is not None else None,
        positions=positions,
    )