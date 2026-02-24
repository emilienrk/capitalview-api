"""Crypto transaction services — atomic ledger model."""

from decimal import Decimal
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlmodel import Session, select

from models import CryptoAccount, CryptoTransaction
from models.market import MarketPrice
from models.enums import CryptoTransactionType
from dtos import (
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from dtos.crypto import CryptoCompositeTransactionCreate, FIAT_SYMBOLS
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_crypto_info, get_crypto_price
from services.crypto_account import _map_account_to_response


def _upsert_market_cache(session: Session, symbol: str, name: Optional[str]) -> None:
    market_price = session.exec(
        select(MarketPrice).where(MarketPrice.isin == symbol.upper())
    ).first()
    if market_price:
        if name and not market_price.name:
            market_price.name = name
            session.add(market_price)
    else:
        market_price = MarketPrice(
            isin=symbol.upper(),
            symbol=symbol.upper(),
            name=name or symbol.upper(),
            current_price=Decimal("0"),
            currency="EUR",
            last_updated=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        session.add(market_price)


def _decrypt_transaction(
    tx: CryptoTransaction, master_key: str
) -> TransactionResponse:
    symbol = decrypt_data(tx.symbol_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str)
    except ValueError:
        executed_at = tx.created_at

    total_cost = amount * price
    is_fee_row = type_str == CryptoTransactionType.FEE.value
    fees = total_cost if is_fee_row else Decimal("0")

    return TransactionResponse(
        id=tx.uuid,
        symbol=symbol,
        isin=symbol,
        type=type_str,
        amount=amount,
        price_per_unit=price,
        fees=fees,
        executed_at=executed_at,
        currency="EUR",
        total_cost=total_cost,
        fees_percentage=Decimal("100") if is_fee_row else Decimal("0"),
        group_uuid=tx.group_uuid,
    )


def create_crypto_transaction(
    session: Session,
    data: CryptoTransactionCreate,
    master_key: str,
    group_uuid: Optional[str] = None,
) -> TransactionResponse:
    if data.name and data.symbol:
        _upsert_market_cache(session, data.symbol, data.name)

    account_bidx = hash_index(data.account_id, master_key)

    symbol_enc = encrypt_data(data.symbol.upper(), master_key)
    type_enc = encrypt_data(data.type.value, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    price_enc = encrypt_data(str(data.price_per_unit), master_key)
    exec_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)

    notes_enc = encrypt_data(data.notes, master_key) if data.notes else None
    tx_hash_enc = encrypt_data(data.tx_hash, master_key) if data.tx_hash else None

    transaction = CryptoTransaction(
        account_id_bidx=account_bidx,
        symbol_enc=symbol_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc,
        tx_hash_enc=tx_hash_enc,
        group_uuid=group_uuid,
    )

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    return _decrypt_transaction(transaction, master_key)


def create_composite_crypto_transaction(
    session: Session,
    data: CryptoCompositeTransactionCreate,
    master_key: str,
) -> List[TransactionResponse]:
    group = str(uuid4())
    rows: List[TransactionResponse] = []

    if data.type == "CRYPTO_DEPOSIT":
        deposit_fee_eur = Decimal("0") if data.fee_included else (data.fee_eur or Decimal("0"))
        eur_amount = data.eur_amount or Decimal("0")
        total_cost_eur = eur_amount + deposit_fee_eur

        anchor = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            type=CryptoTransactionType.FIAT_ANCHOR,
            amount=total_cost_eur,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, anchor, master_key, group_uuid=group))

        buy = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType.BUY,
            amount=data.amount,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, buy, master_key, group_uuid=group))
        return rows

    if data.type == "EXIT":
        spend = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType.SPEND,
            amount=data.amount,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, spend, master_key, group_uuid=group))

        eur_received = data.eur_amount or Decimal("0")
        if eur_received > 0:
            fiat = CryptoTransactionCreate(
                account_id=data.account_id,
                symbol="EUR",
                type=CryptoTransactionType.FIAT_DEPOSIT,
                amount=eur_received,
                price_per_unit=Decimal("1"),
                executed_at=data.executed_at,
                notes=data.notes,
            )
            rows.append(create_crypto_transaction(session, fiat, master_key, group_uuid=group))
        return rows

    if data.type in ("REWARD", "FIAT_DEPOSIT", "TRANSFER"):
        atomic_price = Decimal("1") if data.type == "FIAT_DEPOSIT" else Decimal("0")

        single = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType(data.type),
            amount=data.amount,
            price_per_unit=atomic_price,
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, single, master_key, group_uuid=group))
        return rows
    if data.type == "GAS_FEE":
        fee_row = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType.FEE,
            amount=data.amount,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, fee_row, master_key, group_uuid=group))
        return rows

    if data.type == "FIAT_WITHDRAW":
        withdraw = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            type=CryptoTransactionType.SPEND,
            amount=data.amount,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, withdraw, master_key, group_uuid=group))
        return rows

    if data.type == "NON_TAXABLE_EXIT":
        transfer = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType.TRANSFER,
            amount=data.amount,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, transfer, master_key, group_uuid=group))

        fee_sym = (data.fee_symbol or "").upper()
        fee_qty = data.fee_amount or Decimal("0")
        if fee_sym and fee_sym not in FIAT_SYMBOLS and fee_qty > 0:
            fee_row = CryptoTransactionCreate(
                account_id=data.account_id,
                symbol=fee_sym,
                type=CryptoTransactionType.FEE,
                amount=fee_qty,
                price_per_unit=Decimal("0"),
                executed_at=data.executed_at,
            )
            rows.append(create_crypto_transaction(session, fee_row, master_key, group_uuid=group))
        return rows
    eur_amount = data.eur_amount or Decimal("0")
    quote_sym = (data.quote_symbol or "").upper()
    quote_qty = data.quote_amount or Decimal("0")

    fee_sym = (data.fee_symbol or "").upper()
    fee_qty = data.fee_amount or Decimal("0")
    has_crypto_fee = bool(fee_sym and fee_sym not in FIAT_SYMBOLS and fee_qty > 0)

    if data.fee_included:
        extra_fee_eur = Decimal("0")
    else:
        if has_crypto_fee:
            if data.fee_eur and data.fee_eur > 0:
                extra_fee_eur = data.fee_eur
            elif data.fee_percentage and data.fee_percentage > 0:
                extra_fee_eur = eur_amount * data.fee_percentage / Decimal("100")
            else:
                extra_fee_eur = Decimal("0")
        else:
            extra_fee_eur = data.fee_eur or Decimal("0")

    total_cost_eur = eur_amount + extra_fee_eur

    primary = CryptoTransactionCreate(
        account_id=data.account_id,
        symbol=data.symbol,
        name=data.name,
        type=CryptoTransactionType.BUY,
        amount=data.amount,
        price_per_unit=Decimal("0"),
        executed_at=data.executed_at,
        tx_hash=data.tx_hash,
        notes=data.notes,
    )
    rows.append(create_crypto_transaction(session, primary, master_key, group_uuid=group))

    if quote_sym and quote_qty > 0:
        if quote_sym in FIAT_SYMBOLS:
            spend_amount = quote_qty if has_crypto_fee else (quote_qty + extra_fee_eur)
            spend_price = Decimal("1")
        else:
            spend_amount = quote_qty
            spend_price = Decimal("0")

        spend = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=quote_sym,
            type=CryptoTransactionType.SPEND,
            amount=spend_amount,
            price_per_unit=spend_price,
            executed_at=data.executed_at,
        )
        rows.append(create_crypto_transaction(session, spend, master_key, group_uuid=group))

    need_anchor = (
        (quote_sym and quote_sym not in FIAT_SYMBOLS)
        or (has_crypto_fee and not data.fee_included)
    )
    if need_anchor:
        anchor = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            type=CryptoTransactionType.FIAT_ANCHOR,
            amount=total_cost_eur,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
        )
        rows.append(create_crypto_transaction(session, anchor, master_key, group_uuid=group))

    if has_crypto_fee:
        fee_row = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=fee_sym,
            type=CryptoTransactionType.FEE,
            amount=fee_qty,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
        )
        rows.append(create_crypto_transaction(session, fee_row, master_key, group_uuid=group))

    return rows


def get_crypto_transaction(
    session: Session,
    transaction_uuid: str,
    master_key: str,
) -> Optional[TransactionResponse]:
    transaction = session.get(CryptoTransaction, transaction_uuid)
    if not transaction:
        return None
    return _decrypt_transaction(transaction, master_key)


def update_crypto_transaction(
    session: Session,
    transaction: CryptoTransaction,
    data: CryptoTransactionUpdate,
    master_key: str,
) -> TransactionResponse:
    if data.name and data.symbol:
        _upsert_market_cache(session, data.symbol, data.name)

    if data.symbol is not None:
        transaction.symbol_enc = encrypt_data(data.symbol.upper(), master_key)
    if data.type is not None:
        transaction.type_enc = encrypt_data(data.type.value, master_key)
    if data.amount is not None:
        transaction.amount_enc = encrypt_data(str(data.amount), master_key)
    if data.price_per_unit is not None:
        transaction.price_per_unit_enc = encrypt_data(str(data.price_per_unit), master_key)
    if data.executed_at is not None:
        transaction.executed_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    if data.notes is not None:
        transaction.notes_enc = encrypt_data(data.notes, master_key)
    if data.tx_hash is not None:
        transaction.tx_hash_enc = encrypt_data(data.tx_hash, master_key)

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    return _decrypt_transaction(transaction, master_key)


def delete_crypto_transaction(session: Session, transaction_uuid: str) -> bool:
    transaction = session.get(CryptoTransaction, transaction_uuid)
    if not transaction:
        return False
    session.delete(transaction)
    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> List[TransactionResponse]:
    account_bidx = hash_index(account_uuid, master_key)

    transactions = session.exec(
        select(CryptoTransaction).where(
            CryptoTransaction.account_id_bidx == account_bidx
        )
    ).all()

    decoded = [_decrypt_transaction(tx, master_key) for tx in transactions]

    symbols = {tx.symbol for tx in decoded if tx.symbol}
    market_map: dict = {}
    if symbols:
        market_prices = session.exec(
            select(MarketPrice).where(MarketPrice.isin.in_(symbols))
        ).all()
        for mp in market_prices:
            market_map[mp.isin] = {"name": mp.name, "current_price": mp.current_price}

    for tx in decoded:
        info = market_map.get(tx.symbol)
        if info:
            tx.name = info["name"]
            tx.current_price = info["current_price"]
            if tx.current_price and tx.amount:
                tx.current_value = tx.amount * tx.current_price
                if tx.total_cost and tx.total_cost > 0:
                    tx.profit_loss = tx.current_value - tx.total_cost
                    tx.profit_loss_percentage = (tx.profit_loss / tx.total_cost) * 100

    return decoded


def get_crypto_account_summary(
    session: Session,
    account: CryptoAccount,
    master_key: str,
) -> AccountSummaryResponse:
    acc_resp = _map_account_to_response(account, master_key)

    transactions = get_account_transactions(session, account.uuid, master_key)
    transactions.sort(key=lambda x: x.executed_at)

    buy_group_cost: dict[str, Decimal] = {}
    anchor_by_group: dict[str, Decimal] = {}
    fiat_spend_by_group: dict[str, Decimal] = {}
    for tx in transactions:
        if tx.group_uuid:
            if tx.type == "FIAT_ANCHOR":
                anchor_by_group.setdefault(tx.group_uuid, Decimal("0"))
                anchor_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit
            elif tx.type == "SPEND" and tx.symbol in FIAT_SYMBOLS:
                fiat_spend_by_group.setdefault(tx.group_uuid, Decimal("0"))
                fiat_spend_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit

    for tx in transactions:
        if tx.type == "BUY" and tx.group_uuid:
            if tx.group_uuid in anchor_by_group:
                buy_group_cost[tx.id] = anchor_by_group[tx.group_uuid]
            elif tx.group_uuid in fiat_spend_by_group:
                buy_group_cost[tx.id] = fiat_spend_by_group[tx.group_uuid]
            else:
                buy_group_cost[tx.id] = Decimal("0")

    positions_map: dict[str, dict] = {}

    for tx in transactions:
        symbol = tx.symbol
        if symbol not in positions_map:
            positions_map[symbol] = {
                "symbol": symbol,
                "total_amount": Decimal("0"),
                "cost_basis": Decimal("0"),
                "fees_eur": Decimal("0"),
            }
        pos = positions_map[symbol]
        tx_cost = tx.amount * tx.price_per_unit

        match tx.type:
            case "BUY":
                pos["total_amount"] += tx.amount
                pos["cost_basis"] += buy_group_cost.get(tx.id, tx_cost)
            case "REWARD" | "FIAT_DEPOSIT":
                pos["total_amount"] += tx.amount
            case "FIAT_ANCHOR":
                pass
            case "SPEND" | "TRANSFER" | "EXIT":
                if pos["total_amount"] > 0:
                    fraction = tx.amount / pos["total_amount"]
                    if fraction > Decimal("1"):
                        fraction = Decimal("1")
                    pos["cost_basis"] -= pos["cost_basis"] * fraction
                    pos["total_amount"] -= tx.amount
                    if pos["total_amount"] < 0:
                        pos["total_amount"] = Decimal("0")
                    if pos["cost_basis"] < 0:
                        pos["cost_basis"] = Decimal("0")
            case "FEE":
                pos["total_amount"] -= tx.amount
                if pos["total_amount"] < 0:
                    pos["total_amount"] = Decimal("0")
                pos["fees_eur"] += tx_cost
            case _:
                pass

    positions = []
    for symbol, data in positions_map.items():
        if data["total_amount"] <= Decimal("0"):
            continue

        total_invested = data["cost_basis"]
        fees_eur = data["fees_eur"]
        total_amount = data["total_amount"]
        avg_price = total_invested / total_amount if total_amount > 0 else Decimal("0")
        fees_pct = (fees_eur / total_invested * 100) if total_invested > 0 else Decimal("0")

        if symbol in FIAT_SYMBOLS:
            name = symbol
            current_price = Decimal("1")
        else:
            name, current_price = get_crypto_info(session, symbol)

        current_value = profit_loss = profit_loss_pct = None
        if current_price:
            current_value = total_amount * current_price
            if total_invested > 0:
                profit_loss = current_value - total_invested
                profit_loss_pct = (profit_loss / total_invested * 100)

        positions.append(
            PositionResponse(
                symbol=symbol,
                isin=symbol,
                name=name,
                total_amount=total_amount,
                average_buy_price=round(avg_price, 4),
                total_invested=round(total_invested, 2),
                total_fees=round(fees_eur, 2),
                fees_percentage=round(fees_pct, 2),
                currency="EUR",
                current_price=current_price,
                current_value=round(current_value, 2) if current_value else None,
                profit_loss=round(profit_loss, 2) if profit_loss else None,
                profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
            )
        )

    total_invested_acc = sum(p.total_invested for p in positions)
    total_fees_acc = sum(p.total_fees for p in positions)
    current_value_acc_list = [p.current_value for p in positions if p.current_value is not None]
    current_value_acc = sum(current_value_acc_list) if current_value_acc_list else None

    profit_loss_acc = profit_loss_pct_acc = None
    if current_value_acc is not None:
        profit_loss_acc = current_value_acc - total_invested_acc
        if total_invested_acc > 0:
            profit_loss_pct_acc = (profit_loss_acc / total_invested_acc * 100)

    return AccountSummaryResponse(
        account_id=acc_resp.id,
        account_name=acc_resp.name,
        account_type="CRYPTO",
        total_invested=round(total_invested_acc, 2),
        total_fees=round(total_fees_acc, 2),
        currency="EUR",
        current_value=round(current_value_acc, 2) if current_value_acc else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc else None,
        positions=positions,
    )