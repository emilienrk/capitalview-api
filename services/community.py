"""
Community service — business logic for sharing portfolio PnL.

Responsibilities:
1. Reading / writing CommunityProfile and CommunityPosition rows.
2. Computing PRU from the user's encrypted transactions (requires master_key).
3. Re-encrypting symbol + PRU with the COMMUNITY_ENCRYPTION_KEY.
4. Building the public profile response (only PnL %).
5. Listing available (positive) positions for checkbox selection.
"""

from decimal import Decimal
from typing import List, Optional

from sqlmodel import Session, select

from models.community import CommunityProfile, CommunityPosition
from models.user import User
from models.enums import AssetType
from dtos.community import (
    AvailablePosition,
    AvailablePositionsResponse,
    CommunityPositionResponse,
    CommunityProfileListItem,
    CommunityProfileResponse,
    CommunitySettingsResponse,
    CommunitySettingsUpdate,
)
from services.community_encryption import community_decrypt, community_encrypt
from services.market import get_stock_info, get_crypto_info


def _get_or_create_profile(session: Session, user_id: str) -> CommunityProfile:
    """Return the community profile for *user_id*, creating one if needed.

    The profile PK is user_id itself (one-to-one with users).
    """
    profile = session.exec(
        select(CommunityProfile).where(CommunityProfile.user_id == user_id)
    ).first()
    if not profile:
        profile = CommunityProfile(user_id=user_id, is_active=False)
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


def _compute_stock_pru_for_isins(
    session: Session,
    user_uuid: str,
    master_key: str,
    isins: List[str],
) -> dict[str, Decimal]:
    """Compute PRU for a list of stock ISINs using the user's encrypted transactions.

    Returns {isin: pru} for ISINs with a positive position.
    """
    from models import StockAccount, StockTransaction
    from services.encryption import decrypt_data, hash_index

    user_bidx = hash_index(user_uuid, master_key)

    # Get all stock accounts for the user
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    isin_set = {i.upper() for i in isins}
    # Aggregate across all accounts: {isin: {amount, cost}}
    agg: dict[str, dict] = {}

    for account in accounts:
        account_bidx = hash_index(account.uuid, master_key)
        txs = session.exec(
            select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
        ).all()

        for tx in sorted(txs, key=lambda t: t.created_at):
            isin = decrypt_data(tx.isin_enc, master_key).upper()
            if isin not in isin_set:
                continue

            tx_type = decrypt_data(tx.type_enc, master_key)
            amount = Decimal(decrypt_data(tx.amount_enc, master_key))
            price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
            fees = Decimal(decrypt_data(tx.fees_enc, master_key))

            if isin not in agg:
                agg[isin] = {"amount": Decimal("0"), "cost": Decimal("0")}

            pos = agg[isin]
            if tx_type in ("BUY", "DIVIDEND", "DEPOSIT"):
                pos["amount"] += amount
                pos["cost"] += (amount * price) + fees
            elif tx_type == "SELL" and pos["amount"] > 0:
                fraction = min(amount / pos["amount"], Decimal("1"))
                pos["amount"] -= amount
                pos["cost"] -= pos["cost"] * fraction
                pos["amount"] = max(pos["amount"], Decimal("0"))
                pos["cost"] = max(pos["cost"], Decimal("0"))

    result: dict[str, Decimal] = {}
    for isin, data in agg.items():
        if data["amount"] > 0:
            result[isin] = data["cost"] / data["amount"]
    return result


def _compute_crypto_pru_for_symbols(
    session: Session,
    user_uuid: str,
    master_key: str,
    symbols: List[str],
) -> dict[str, Decimal]:
    """Compute PRU for a list of crypto symbols using the user's encrypted transactions.

    Returns {symbol: pru} for symbols with a positive position.
    Reuses the same accounting logic as the main crypto_transaction module.
    """
    from models import CryptoAccount, CryptoTransaction
    from dtos.crypto import FIAT_SYMBOLS
    from services.encryption import decrypt_data, hash_index

    user_bidx = hash_index(user_uuid, master_key)
    symbol_set = {s.upper() for s in symbols}

    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    # We need to replay ALL transactions because PRU depends on group logic
    # (FIAT_ANCHOR / SPEND in EUR determine the cost basis of BUY rows).
    all_decrypted: list[dict] = []

    for account in accounts:
        account_bidx = hash_index(account.uuid, master_key)
        raw_txs = session.exec(
            select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
        ).all()

        for tx in raw_txs:
            symbol = decrypt_data(tx.symbol_enc, master_key).upper()
            tx_type = decrypt_data(tx.type_enc, master_key)
            amount = Decimal(decrypt_data(tx.amount_enc, master_key))
            price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))

            all_decrypted.append({
                "id": tx.uuid,
                "symbol": symbol,
                "type": tx_type,
                "amount": amount,
                "price": price,
                "group_uuid": tx.group_uuid,
                "created_at": tx.created_at,
            })

    all_decrypted.sort(key=lambda t: t["created_at"])

    # Build group-level anchor costs (same logic as get_crypto_account_summary)
    anchor_by_group: dict[str, Decimal] = {}
    fiat_spend_by_group: dict[str, Decimal] = {}
    for tx in all_decrypted:
        g = tx["group_uuid"]
        if g:
            if tx["type"] == "FIAT_ANCHOR":
                anchor_by_group.setdefault(g, Decimal("0"))
                anchor_by_group[g] += tx["amount"] * tx["price"]
            elif tx["type"] == "SPEND" and tx["symbol"] in FIAT_SYMBOLS:
                fiat_spend_by_group.setdefault(g, Decimal("0"))
                fiat_spend_by_group[g] += tx["amount"] * tx["price"]

    buy_group_cost: dict[str, Decimal] = {}
    for tx in all_decrypted:
        if tx["type"] == "BUY" and tx["group_uuid"]:
            g = tx["group_uuid"]
            if g in anchor_by_group:
                buy_group_cost[tx["id"]] = anchor_by_group[g]
            elif g in fiat_spend_by_group:
                buy_group_cost[tx["id"]] = fiat_spend_by_group[g]
            else:
                buy_group_cost[tx["id"]] = Decimal("0")

    # Replay per symbol
    positions: dict[str, dict] = {}  # symbol → {amount, cost_basis}
    for tx in all_decrypted:
        sym = tx["symbol"]
        if sym not in symbol_set:
            continue

        if sym not in positions:
            positions[sym] = {"amount": Decimal("0"), "cost_basis": Decimal("0")}
        pos = positions[sym]
        tx_cost = tx["amount"] * tx["price"]

        match tx["type"]:
            case "BUY":
                group_cost = buy_group_cost.get(tx["id"], tx_cost)
                prev = pos["amount"]
                pos["amount"] += tx["amount"]
                if prev < 0 and tx["amount"] > 0:
                    surviving = max(pos["amount"], Decimal("0"))
                    pos["cost_basis"] += group_cost * (surviving / tx["amount"])
                else:
                    pos["cost_basis"] += group_cost
            case "REWARD" | "FIAT_DEPOSIT":
                pos["amount"] += tx["amount"]
            case "SPEND" | "TRANSFER" | "EXIT":
                if pos["amount"] > 0:
                    fraction = min(tx["amount"] / pos["amount"], Decimal("1"))
                    pos["cost_basis"] -= pos["cost_basis"] * fraction
                    if pos["cost_basis"] < 0:
                        pos["cost_basis"] = Decimal("0")
                pos["amount"] -= tx["amount"]
            case "FEE":
                pos["amount"] -= tx["amount"]

    result: dict[str, Decimal] = {}
    for sym, data in positions.items():
        if data["amount"] > 0 and data["cost_basis"] > 0:
            result[sym] = data["cost_basis"] / data["amount"]
    return result


def update_community_settings(
    session: Session,
    user_uuid: str,
    master_key: str,
    data: CommunitySettingsUpdate,
) -> CommunitySettingsResponse:
    """Create / update the community profile and re-compute shared positions.

    Steps:
    1. Ensure a CommunityProfile row exists.
    2. Set is_active, display_name, bio.
    3. Compute PRU for selected stock ISINs and crypto symbols.
    4. Delete existing CommunityPosition rows and insert new ones,
       encrypting symbol + PRU with COMMUNITY_ENCRYPTION_KEY.
    """
    profile = _get_or_create_profile(session, user_uuid)
    profile.is_active = data.is_active
    profile.display_name = data.display_name
    profile.bio = data.bio
    session.add(profile)
    session.flush()

    # Delete old positions
    old_positions = session.exec(
        select(CommunityPosition).where(CommunityPosition.profile_user_id == profile.user_id)
    ).all()
    for pos in old_positions:
        session.delete(pos)
    session.flush()

    # Compute PRU and create new positions
    stock_pru = _compute_stock_pru_for_isins(
        session, user_uuid, master_key, data.shared_stock_isins
    )
    crypto_pru = _compute_crypto_pru_for_symbols(
        session, user_uuid, master_key, data.shared_crypto_symbols
    )

    created_count = 0

    for isin, pru in stock_pru.items():
        pos = CommunityPosition(
            profile_user_id=profile.user_id,
            asset_type=AssetType.STOCK.value,
            symbol_encrypted=community_encrypt(isin),
            pru_encrypted=community_encrypt(str(pru)),
        )
        session.add(pos)
        created_count += 1

    for symbol, pru in crypto_pru.items():
        pos = CommunityPosition(
            profile_user_id=profile.user_id,
            asset_type=AssetType.CRYPTO.value,
            symbol_encrypted=community_encrypt(symbol),
            pru_encrypted=community_encrypt(str(pru)),
        )
        session.add(pos)
        created_count += 1

    session.commit()

    return CommunitySettingsResponse(
        is_active=profile.is_active,
        display_name=profile.display_name,
        bio=profile.bio,
        shared_stock_isins=list(stock_pru.keys()),
        shared_crypto_symbols=list(crypto_pru.keys()),
        positions_count=created_count,
    )


def refresh_community_positions(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> None:
    """Re-compute and update community positions if the user's profile is active.

    Called as a side-effect on login or transaction mutation.
    Does nothing if the user has no active community profile.
    """
    profile = session.exec(
        select(CommunityProfile).where(
            CommunityProfile.user_id == user_uuid,
            CommunityProfile.is_active == True,  # noqa: E712
        )
    ).first()
    if not profile:
        return

    # Read existing positions to know which symbols/ISINs to re-compute
    existing = session.exec(
        select(CommunityPosition).where(CommunityPosition.profile_user_id == profile.user_id)
    ).all()

    stock_isins: list[str] = []
    crypto_symbols: list[str] = []
    for pos in existing:
        symbol = community_decrypt(pos.symbol_encrypted)
        if pos.asset_type == AssetType.STOCK.value:
            stock_isins.append(symbol)
        else:
            crypto_symbols.append(symbol)

    if not stock_isins and not crypto_symbols:
        return

    # Delete old rows
    for pos in existing:
        session.delete(pos)
    session.flush()

    # Re-compute
    stock_pru = _compute_stock_pru_for_isins(session, user_uuid, master_key, stock_isins)
    crypto_pru = _compute_crypto_pru_for_symbols(session, user_uuid, master_key, crypto_symbols)

    for isin, pru in stock_pru.items():
        session.add(CommunityPosition(
            profile_user_id=profile.user_id,
            asset_type=AssetType.STOCK.value,
            symbol_encrypted=community_encrypt(isin),
            pru_encrypted=community_encrypt(str(pru)),
        ))

    for symbol, pru in crypto_pru.items():
        session.add(CommunityPosition(
            profile_user_id=profile.user_id,
            asset_type=AssetType.CRYPTO.value,
            symbol_encrypted=community_encrypt(symbol),
            pru_encrypted=community_encrypt(str(pru)),
        ))

    session.commit()


def list_active_profiles(session: Session) -> List[CommunityProfileListItem]:
    """Return all active community profiles (usernames + display info)."""
    rows = session.exec(
        select(User.username, CommunityProfile.display_name, CommunityProfile.bio)
        .join(CommunityProfile, CommunityProfile.user_id == User.uuid)
        .where(CommunityProfile.is_active == True)  # noqa: E712
    ).all()

    return [
        CommunityProfileListItem(username=row[0], display_name=row[1], bio=row[2])
        for row in rows
    ]


def get_public_profile(
    session: Session,
    username: str,
) -> Optional[CommunityProfileResponse]:
    """Build the public profile for *username*.

    Decrypts positions with COMMUNITY_ENCRYPTION_KEY, fetches current market
    prices, computes PnL %, and returns only safe data.
    """
    user = session.exec(select(User).where(User.username == username)).first()
    if not user:
        return None

    profile = session.exec(
        select(CommunityProfile).where(
            CommunityProfile.user_id == user.uuid,
            CommunityProfile.is_active == True,  # noqa: E712
        )
    ).first()
    if not profile:
        return None

    positions = session.exec(
        select(CommunityPosition).where(CommunityPosition.profile_user_id == profile.user_id)
    ).all()

    response_positions: list[CommunityPositionResponse] = []
    # For weighted-average global PnL
    total_weight = Decimal("0")  # sum of |PRU| per position (proxy for investment weight)
    weighted_pnl_sum = Decimal("0")

    for pos in positions:
        symbol = community_decrypt(pos.symbol_encrypted)
        pru = Decimal(community_decrypt(pos.pru_encrypted))
        asset_type = pos.asset_type

        # Fetch current price
        current_price: Optional[Decimal] = None
        if asset_type == AssetType.STOCK.value:
            _, current_price = get_stock_info(session, symbol)
        elif asset_type == AssetType.CRYPTO.value:
            _, current_price = get_crypto_info(session, symbol)

        pnl_pct: Optional[float] = None
        if current_price is not None and pru > 0:
            pnl_pct = float(((current_price - pru) / pru) * 100)
            pnl_pct = round(pnl_pct, 2)

            # Weight by PRU (proxy — we don't expose absolute amounts)
            total_weight += pru
            weighted_pnl_sum += pru * Decimal(str(pnl_pct))

        response_positions.append(CommunityPositionResponse(
            symbol=symbol,
            asset_type=asset_type,
            pnl_percentage=pnl_pct,
        ))

    global_pnl: Optional[float] = None
    if total_weight > 0:
        global_pnl = round(float(weighted_pnl_sum / total_weight), 2)

    return CommunityProfileResponse(
        username=username,
        display_name=profile.display_name,
        bio=profile.bio,
        positions=response_positions,
        global_pnl_percentage=global_pnl,
    )


def get_community_settings(
    session: Session,
    user_uuid: str,
) -> CommunitySettingsResponse:
    """Return current community settings for the authenticated user."""
    profile = session.exec(
        select(CommunityProfile).where(CommunityProfile.user_id == user_uuid)
    ).first()

    if not profile:
        return CommunitySettingsResponse(
            is_active=False,
            display_name=None,
            bio=None,
            shared_stock_isins=[],
            shared_crypto_symbols=[],
            positions_count=0,
        )

    positions = session.exec(
        select(CommunityPosition).where(CommunityPosition.profile_user_id == profile.user_id)
    ).all()

    stock_isins: list[str] = []
    crypto_symbols: list[str] = []
    for pos in positions:
        symbol = community_decrypt(pos.symbol_encrypted)
        if pos.asset_type == AssetType.STOCK.value:
            stock_isins.append(symbol)
        else:
            crypto_symbols.append(symbol)

    return CommunitySettingsResponse(
        is_active=profile.is_active,
        display_name=profile.display_name,
        bio=profile.bio,
        shared_stock_isins=stock_isins,
        shared_crypto_symbols=crypto_symbols,
        positions_count=len(positions),
    )


def get_available_positions(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> AvailablePositionsResponse:
    """Return all shareable positions for the authenticated user.

    Decrypts all transactions, computes current holdings, and returns only
    positions with a strictly positive amount.
    Crypto positions with negative amounts are excluded (no short sharing).
    """
    from models import StockAccount, StockTransaction, CryptoAccount, CryptoTransaction
    from dtos.crypto import FIAT_SYMBOLS
    from services.encryption import decrypt_data, hash_index

    user_bidx = hash_index(user_uuid, master_key)

    # Compute net stock amount per ISIN
    stock_accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    stock_agg: dict[str, Decimal] = {}  # isin → net amount
    for account in stock_accounts:
        account_bidx = hash_index(account.uuid, master_key)
        txs = session.exec(
            select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
        ).all()
        for tx in txs:
            isin = decrypt_data(tx.isin_enc, master_key).upper()
            tx_type = decrypt_data(tx.type_enc, master_key)
            amount = Decimal(decrypt_data(tx.amount_enc, master_key))
            if isin not in stock_agg:
                stock_agg[isin] = Decimal("0")
            if tx_type in ("BUY", "DIVIDEND", "DEPOSIT"):
                stock_agg[isin] += amount
            elif tx_type == "SELL":
                stock_agg[isin] -= amount

    stocks = [
        AvailablePosition(symbol=isin, asset_type=AssetType.STOCK.value)
        for isin, amt in sorted(stock_agg.items())
        if amt > 0
    ]

    # Compute net crypto amount per symbol
    crypto_accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    crypto_agg: dict[str, Decimal] = {}
    for account in crypto_accounts:
        account_bidx = hash_index(account.uuid, master_key)
        txs = session.exec(
            select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
        ).all()
        for tx in txs:
            symbol = decrypt_data(tx.symbol_enc, master_key).upper()
            tx_type = decrypt_data(tx.type_enc, master_key)
            amount = Decimal(decrypt_data(tx.amount_enc, master_key))
            # Skip fiat and anchor rows
            if symbol in FIAT_SYMBOLS or tx_type == "FIAT_ANCHOR":
                continue
            if symbol not in crypto_agg:
                crypto_agg[symbol] = Decimal("0")
            if tx_type in ("BUY", "REWARD", "FIAT_DEPOSIT"):
                crypto_agg[symbol] += amount
            elif tx_type in ("SPEND", "TRANSFER", "EXIT", "FEE"):
                crypto_agg[symbol] -= amount

    # Only positive positions — no negative crypto sharing
    crypto = [
        AvailablePosition(symbol=sym, asset_type=AssetType.CRYPTO.value)
        for sym, amt in sorted(crypto_agg.items())
        if amt > 0
    ]

    return AvailablePositionsResponse(stocks=stocks, crypto=crypto)
