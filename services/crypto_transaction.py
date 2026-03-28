"""Crypto transaction services — atomic ledger model."""

from decimal import Decimal
from datetime import datetime, date
from uuid import uuid4

from sqlmodel import Session, select

from models import CryptoTransaction
from models.market import MarketAsset
from models.enums import CryptoCompositeTransactionType, CryptoTransactionType, AssetType
from dtos import (
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from dtos.crypto import CryptoCompositeTransactionCreate, CrossAccountTransferCreate, FIAT_SYMBOLS
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_crypto_info

def _upsert_market_cache(session: Session, symbol: str, name: str | None) -> None:
    market_asset = session.exec(
        select(MarketAsset).where(MarketAsset.isin == symbol.upper())
    ).first()
    if market_asset:
        if name and not market_asset.name:
            market_asset.name = name
            session.add(market_asset)
    else:
        market_asset = MarketAsset(
            isin=symbol.upper(),
            symbol=symbol.upper(),
            name=name or symbol.upper(),
            asset_type=AssetType.CRYPTO,
        )
        session.add(market_asset)


def _decrypt_transaction(
    tx: CryptoTransaction, master_key: str
) -> TransactionResponse:
    symbol = decrypt_data(tx.symbol_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str.replace("Z", "+00:00"))
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
    group_uuid: str | None = None,
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
) -> list[TransactionResponse]:
    group = str(uuid4())
    rows: list[TransactionResponse] = []
    composite_type = CryptoCompositeTransactionType.normalize(data.type)

    if composite_type == CryptoCompositeTransactionType.CRYPTO_DEPOSIT:
        eur_amount = data.eur_amount or Decimal("0")

        fiat_deposit = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            type=CryptoTransactionType.DEPOSIT,
            amount=eur_amount,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, fiat_deposit, master_key, group_uuid=group))

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

        spend_eur = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            type=CryptoTransactionType.SPEND,
            amount=eur_amount,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, spend_eur, master_key, group_uuid=group))
        return rows

    if composite_type in (
        CryptoCompositeTransactionType.FIAT_WITHDRAW, 
        CryptoCompositeTransactionType.FIAT_DEPOSIT
    ):
        withdraw = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol="EUR",
            name=data.name,
            type=CryptoTransactionType.WITHDRAW if composite_type == CryptoCompositeTransactionType.FIAT_WITHDRAW else CryptoTransactionType.DEPOSIT,
            amount=data.amount,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, withdraw, master_key, group_uuid=group))
        return rows
    

    if composite_type in (
        CryptoCompositeTransactionType.REWARD,
        CryptoCompositeTransactionType.TRANSFER,
    ):
        single = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=data.symbol,
            name=data.name,
            type=CryptoTransactionType.REWARD if composite_type == CryptoCompositeTransactionType.REWARD else CryptoTransactionType.TRANSFER,
            amount=data.amount,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, single, master_key, group_uuid=group))
        return rows

    if composite_type == CryptoCompositeTransactionType.SELL_TO_FIAT:
        fiat_symbol = (data.quote_symbol or "EUR").upper()
        fiat_amount = data.eur_amount or data.quote_amount or Decimal("0")

        spend_crypto = CryptoTransactionCreate(
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
        rows.append(create_crypto_transaction(session, spend_crypto, master_key, group_uuid=group))

        if fiat_amount > 0:
            deposit_fiat = CryptoTransactionCreate(
                account_id=data.account_id,
                symbol=fiat_symbol,
                type=CryptoTransactionType.DEPOSIT,
                amount=fiat_amount,
                price_per_unit=Decimal("1"),
                executed_at=data.executed_at,
                tx_hash=data.tx_hash,
                notes=data.notes,
            )
            rows.append(create_crypto_transaction(session, deposit_fiat, master_key, group_uuid=group))

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
                tx_hash=data.tx_hash,
                notes=data.notes,
            )
            rows.append(create_crypto_transaction(session, fee_row, master_key, group_uuid=group))

        return rows

    if composite_type == CryptoCompositeTransactionType.FEE:
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

    if composite_type == CryptoCompositeTransactionType.NON_TAXABLE_EXIT:
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
            type=CryptoTransactionType.ANCHOR,
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


def _compute_symbol_pru(
    session: Session,
    account_uuid: str,
    symbol: str,
    master_key: str,
) -> Decimal:
    """
    Compute the PRU (average cost basis per unit) for a single symbol in an
    account by replaying all transactions chronologically.
    Returns 0 if the position doesn't exist or has no cost basis.
    """
    symbol_up = symbol.upper()
    account_bidx = hash_index(account_uuid, master_key)

    raw_txs = session.exec(
        select(CryptoTransaction).where(
            CryptoTransaction.account_id_bidx == account_bidx
        )
    ).all()
    transactions = [_decrypt_transaction(tx, master_key) for tx in raw_txs]
    transactions.sort(key=lambda x: x.executed_at)

    # Build group-level anchor costs (ANCHOR + fiat SPEND) to attribute
    # to BUY rows — same logic as get_crypto_account_summary.
    anchor_by_group: dict[str, Decimal] = {}
    fiat_spend_by_group: dict[str, Decimal] = {}
    for tx in transactions:
        if tx.group_uuid:
            if tx.type == CryptoTransactionType.ANCHOR.value:
                anchor_by_group.setdefault(tx.group_uuid, Decimal("0"))
                anchor_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit
            elif tx.type == CryptoTransactionType.SPEND.value and tx.symbol in FIAT_SYMBOLS:
                fiat_spend_by_group.setdefault(tx.group_uuid, Decimal("0"))
                fiat_spend_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit

    buy_group_cost: dict[str, Decimal] = {}
    for tx in transactions:
        if tx.type == CryptoTransactionType.BUY.value and tx.group_uuid:
            if tx.group_uuid in anchor_by_group:
                buy_group_cost[tx.id] = anchor_by_group[tx.group_uuid]
            elif tx.group_uuid in fiat_spend_by_group:
                buy_group_cost[tx.id] = fiat_spend_by_group[tx.group_uuid]
            else:
                buy_group_cost[tx.id] = Decimal("0")

    total_amount = Decimal("0")
    cost_basis = Decimal("0")

    for tx in transactions:
        if tx.symbol != symbol_up:
            continue
        match tx.type:
            case CryptoTransactionType.BUY.value:
                group_cost = buy_group_cost.get(tx.id, tx.amount * tx.price_per_unit)
                prev = total_amount
                total_amount += tx.amount
                if prev < 0 and tx.amount > 0:
                    surviving = max(total_amount, Decimal("0"))
                    cost_basis += group_cost * (surviving / tx.amount)
                else:
                    cost_basis += group_cost
            case CryptoTransactionType.REWARD.value | CryptoTransactionType.DEPOSIT.value:
                total_amount += tx.amount
            case CryptoTransactionType.SPEND.value | CryptoTransactionType.TRANSFER.value:
                if total_amount > 0:
                    fraction = min(tx.amount / total_amount, Decimal("1"))
                    cost_basis -= cost_basis * fraction
                    if cost_basis < 0:
                        cost_basis = Decimal("0")
                # Always subtract quantity — allows negative balance when SPEND
                # precedes BUY, so subsequent BUY correctly nets the position.
                total_amount -= tx.amount
            case CryptoTransactionType.FEE.value:
                total_amount -= tx.amount

    if total_amount <= 0:
        return Decimal("0")
    return cost_basis / total_amount


def create_cross_account_transfer(
    session: Session,
    data: "CrossAccountTransferCreate",
    user_uuid: str,
    master_key: str,
) -> list[TransactionResponse]:
    """
    Create a cross-account crypto transfer.
    Emits:
      - TRANSFER row (outbound, neutral) in the source account.
      - ANCHOR + BUY rows in the destination account (= CRYPTO_DEPOSIT
        pattern), anchored to the book value leaving the source account
        (quantity × PRU of source), so cost basis carries over correctly.
      - Optional FEE row in the source account for on-chain / gas fees.
    All rows share the same group_uuid for traceability.
    """
    from services.crypto_account import get_crypto_account as _get_account

    src = _get_account(session, data.from_account_id, user_uuid, master_key)
    if not src:
        raise ValueError("Compte source introuvable ou acces refuse")

    dst = _get_account(session, data.to_account_id, user_uuid, master_key)
    if not dst:
        raise ValueError("Compte destination introuvable ou acces refuse")

    if data.from_account_id == data.to_account_id:
        raise ValueError("Les comptes source et destination doivent etre differents")

    pru = _compute_symbol_pru(session, data.from_account_id, data.symbol, master_key)
    book_value = (data.amount * pru).quantize(Decimal("0.01"))

    group = str(uuid4())
    rows: list[TransactionResponse] = []

    # 1. Outbound TRANSFER in source account
    tx_out = CryptoTransactionCreate(
        account_id=data.from_account_id,
        symbol=data.symbol,
        name=data.name,
        type=CryptoTransactionType.TRANSFER,
        amount=data.amount,
        price_per_unit=Decimal("0"),
        executed_at=data.executed_at,
        tx_hash=data.tx_hash,
        notes=data.notes,
    )
    rows.append(create_crypto_transaction(session, tx_out, master_key, group_uuid=group))

    # 2a. ANCHOR in destination — establishes cost basis equal to the
    #     book value that left the source account (quantity × PRU_source).
    if book_value > 0:
        anchor_in = CryptoTransactionCreate(
            account_id=data.to_account_id,
            symbol="EUR",
            type=CryptoTransactionType.ANCHOR,
            amount=book_value,
            price_per_unit=Decimal("1"),
            executed_at=data.executed_at,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, anchor_in, master_key, group_uuid=group))

    # 2b. Inbound BUY (price=0) in destination — quantity row paired with anchor
    tx_in = CryptoTransactionCreate(
        account_id=data.to_account_id,
        symbol=data.symbol,
        name=data.name,
        type=CryptoTransactionType.BUY,
        amount=data.amount,
        price_per_unit=Decimal("0"),
        executed_at=data.executed_at,
        tx_hash=data.tx_hash,
        notes=data.notes,
    )
    rows.append(create_crypto_transaction(session, tx_in, master_key, group_uuid=group))

    # 3. Optional on-chain fee row in source account
    fee_sym = (data.fee_symbol or "").upper()
    fee_qty = data.fee_amount or Decimal("0")
    if fee_sym and fee_qty > 0:
        fee_row = CryptoTransactionCreate(
            account_id=data.from_account_id,
            symbol=fee_sym,
            type=CryptoTransactionType.FEE,
            amount=fee_qty,
            price_per_unit=Decimal("0"),
            executed_at=data.executed_at,
            tx_hash=data.tx_hash,
            notes=data.notes,
        )
        rows.append(create_crypto_transaction(session, fee_row, master_key, group_uuid=group))

    return rows


def get_crypto_transaction(
    session: Session,
    transaction_uuid: str,
    master_key: str,
) -> TransactionResponse | None:
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
        # Propagate the new date to all sibling transactions in the same group
        if transaction.group_uuid:
            siblings = session.exec(
                select(CryptoTransaction).where(
                    CryptoTransaction.group_uuid == transaction.group_uuid,
                    CryptoTransaction.uuid != transaction.uuid,
                )
            ).all()
            new_date_enc = encrypt_data(data.executed_at.isoformat(), master_key)
            for sibling in siblings:
                sibling.executed_at_enc = new_date_enc
                session.add(sibling)
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

    if transaction.group_uuid:
        grouped_transactions = session.exec(
            select(CryptoTransaction).where(
                CryptoTransaction.group_uuid == transaction.group_uuid,
            )
        ).all()
        for grouped_transaction in grouped_transactions:
            session.delete(grouped_transaction)
    else:
        session.delete(transaction)

    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> list[TransactionResponse]:
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
        market_assets = session.exec(
            select(MarketAsset).where(MarketAsset.isin.in_(symbols))
        ).all()
        from services.market import get_latest_price
        for ma in market_assets:
            latest = get_latest_price(session, ma.id)
            market_map[ma.isin] = {"name": ma.name, "current_price": latest}

    for tx in decoded:
        info = market_map.get(tx.symbol)
        if info and tx.type != "ANCHOR":
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
    transactions: list[TransactionResponse],
    as_of: date = None,
    db_only: bool = False,
    preloaded_prices: dict[str, Decimal] = None,
) -> AccountSummaryResponse:
    if as_of is None:
        as_of = date.today()

    transactions.sort(key=lambda x: x.executed_at)
    transactions = [tx for tx in transactions if tx.executed_at.date() <= as_of]

    buy_group_cost: dict[str, Decimal] = {}
    anchor_by_group: dict[str, Decimal] = {}
    fiat_spend_by_group: dict[str, Decimal] = {}
    groups_with_crypto_spend: set[str] = set()
    groups_with_crypto_buy: set[str] = set()
    for tx in transactions:
        if tx.group_uuid:
            if tx.type == "ANCHOR":
                anchor_by_group.setdefault(tx.group_uuid, Decimal("0"))
                anchor_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit
            elif tx.type == "SPEND" and tx.symbol in FIAT_SYMBOLS:
                fiat_spend_by_group.setdefault(tx.group_uuid, Decimal("0"))
                fiat_spend_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit
            elif tx.type == "SPEND" and tx.symbol not in FIAT_SYMBOLS:
                groups_with_crypto_spend.add(tx.group_uuid)
            elif tx.type == "BUY" and tx.symbol not in FIAT_SYMBOLS:
                groups_with_crypto_buy.add(tx.group_uuid)

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
                group_cost = buy_group_cost.get(tx.id, tx_cost)
                prev_amount = pos["total_amount"]
                pos["total_amount"] += tx.amount
                if prev_amount < 0 and tx.amount > 0:
                    surviving = max(pos["total_amount"], Decimal("0"))
                    pos["cost_basis"] += group_cost * (surviving / tx.amount)
                else:
                    pos["cost_basis"] += group_cost
            case "REWARD" | "DEPOSIT":
                pos["total_amount"] += tx.amount
            case "ANCHOR":
                pass
            case "SPEND" | "TRANSFER":
                if pos["total_amount"] > 0:
                    # Normal case: reduce cost basis proportionally
                    fraction = tx.amount / pos["total_amount"]
                    if fraction > Decimal("1"):
                        fraction = Decimal("1")
                    pos["cost_basis"] -= pos["cost_basis"] * fraction
                    if pos["cost_basis"] < 0:
                        pos["cost_basis"] = Decimal("0")
                pos["total_amount"] -= tx.amount
            case "FEE":
                pos["total_amount"] -= tx.amount
                pos["fees_eur"] += tx_cost
            case "WITHDRAW":
                if tx.symbol in FIAT_SYMBOLS:
                    # Fiat withdrawal (exchange -> bank): no crypto cost-basis impact.
                    pos["total_amount"] -= tx.amount
                else:
                    # Taxable outbound crypto: remove cost basis proportionally.
                    if pos["total_amount"] > 0:
                        fraction = tx.amount / pos["total_amount"]
                        if fraction > Decimal("1"):
                            fraction = Decimal("1")
                        pos["cost_basis"] -= pos["cost_basis"] * fraction
                        if pos["cost_basis"] < 0:
                            pos["cost_basis"] = Decimal("0")
                    pos["total_amount"] -= tx.amount
            case _:
                pass

    positions = []
    for symbol, data in positions_map.items():
        if data["total_amount"] == Decimal("0"):
            continue
        if symbol not in FIAT_SYMBOLS and data["total_amount"] < Decimal("0"):
            # Keep fiat cash lines, but drop overspent crypto symbols from summary positions.
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
            if preloaded_prices is not None:
                current_price = preloaded_prices.get(symbol)
                name = symbol
            else:
                name, current_price = get_crypto_info(session, symbol, as_of=as_of, db_only=db_only)

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
                current_value=round(current_value, 2) if current_value is not None else None,
                profit_loss=round(profit_loss, 2) if profit_loss is not None else None,
                profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct is not None else None,
            )
        )

    net_external_deposits = Decimal("0")
    has_explicit_external_flow = False
    has_fiat_buy_legs = False
    has_crypto_activity = False
    for tx in transactions:
        if tx.symbol not in FIAT_SYMBOLS and tx.type != "ANCHOR":
            has_crypto_activity = True
        if tx.type == "DEPOSIT" and tx.symbol in FIAT_SYMBOLS:
            # External wire IN — only if NOT part of a crypto-sale (SELL_TO_FIAT) group
            if not tx.group_uuid or tx.group_uuid not in groups_with_crypto_spend:
                net_external_deposits += tx.amount * tx.price_per_unit
                has_explicit_external_flow = True
        elif tx.type == "SPEND" and tx.symbol in FIAT_SYMBOLS:
            if tx.group_uuid and tx.group_uuid in groups_with_crypto_buy:
                has_fiat_buy_legs = True
            # External withdrawal OUT — only if NOT part of a crypto-buy group
            if not tx.group_uuid or tx.group_uuid not in groups_with_crypto_buy:
                net_external_deposits -= tx.amount * tx.price_per_unit
                has_explicit_external_flow = True
        elif tx.type == "WITHDRAW" and tx.symbol in FIAT_SYMBOLS:
            # Direct withdrawal of fiat — always reduces net deposits
            net_external_deposits -= tx.amount * tx.price_per_unit
            has_explicit_external_flow = True

    crypto_positions = [p for p in positions if p.symbol not in FIAT_SYMBOLS]

    total_invested_acc = sum(p.total_invested for p in crypto_positions)
    total_fees_acc = sum(p.total_fees for p in crypto_positions)
    current_value_acc_list = [p.current_value for p in positions if p.current_value is not None]
    current_value_acc = sum(current_value_acc_list) if current_value_acc_list else None

    # Account-level invested follows external fiat flows when explicitly tracked.
    # For legacy ledgers with only BUY+SPEND fiat legs (no external deposit rows),
    # keep the historical fallback to crypto cost basis.
    if not has_crypto_activity:
        total_invested_account = Decimal("0")
    elif has_explicit_external_flow:
        total_invested_account = net_external_deposits
    elif has_fiat_buy_legs:
        total_invested_account = total_invested_acc
    else:
        total_invested_account = Decimal("0")

    profit_loss_acc = profit_loss_pct_acc = None
    if has_crypto_activity and current_value_acc is not None:
        profit_loss_acc = current_value_acc - total_invested_account
        if total_invested_account > 0:
            profit_loss_pct_acc = (profit_loss_acc / total_invested_account * 100)

    return AccountSummaryResponse(
        total_invested=round(total_invested_account, 2),
        total_deposits=round(net_external_deposits, 2),
        total_fees=round(total_fees_acc, 2),
        currency="EUR",
        current_value=round(current_value_acc, 2) if current_value_acc is not None else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc is not None else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc is not None else None,
        positions=positions,
    )


# ── Balance helpers ──────────────────────────────────────────────────────────


def get_symbol_balance(
    session: Session,
    account_uuid: str,
    symbol: str,
    master_key: str,
    transactions: list[CryptoTransaction] | None = None,
) -> Decimal:
    """
    Compute the current net holding of *symbol* in the given account.

    Credits (BUY, REWARD, DEPOSIT) add to the balance.
    Debits  (SPEND, TRANSFER, WITHDRAW, FEE)
    subtract from the balance.
    ANCHOR rows are skipped (accounting reference, not a real cash flow).
    """
    account_bidx = hash_index(account_uuid, master_key)
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
    ).all()

    balance = Decimal("0")
    sym_upper = symbol.upper()
    for tx in transactions:
        tx_sym = decrypt_data(tx.symbol_enc, master_key).upper()
        if tx_sym != sym_upper:
            continue
        type_str = decrypt_data(tx.type_enc, master_key)
        amount = Decimal(decrypt_data(tx.amount_enc, master_key))
        if type_str in CryptoTransactionType.credit_types():
            balance += amount
        elif type_str in CryptoTransactionType.debit_types():
            balance -= amount
        # ANCHOR and unknown types: ignored

    return balance


def compute_balance_warning(
    session: Session,
    account_uuid: str,
    created_rows,
    master_key: str,
    extra_account_for_symbols: dict[str, str] | None = None,
) -> str | None:
    """
    Check whether any debited crypto symbol has gone negative after an operation.
    Returns a human-readable warning string (French) or None.

    extra_account_for_symbols maps symbol → account_uuid to support
    cross-account checks (e.g. source account for a TRANSFER).
    """
    to_check: dict[str, str] = {}
    for row in created_rows:
        type_str = row.type if isinstance(row.type, str) else row.type.value
        if type_str not in CryptoTransactionType.debit_types():
            continue
        sym = (row.symbol or "").upper()
        if not sym or sym in FIAT_SYMBOLS:
            continue
        acc = (
            extra_account_for_symbols.get(sym, account_uuid)
            if extra_account_for_symbols
            else account_uuid
        )
        to_check[sym] = acc

    if not to_check:
        return None

    negative: list[str] = []
    for sym, acc in sorted(to_check.items()):
        balance = get_symbol_balance(session, acc, sym, master_key)
        if balance < 0:
            negative.append(f"{sym} (solde\u00a0: {balance:+.8g})")

    if not negative:
        return None
    return "Solde insuffisant après cette opération\u00a0— " + ", ".join(negative)
