"""Crypto accounts and transactions CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User, CryptoAccount, CryptoTransaction
from services.auth import get_current_user, get_master_key
from models.enums import CryptoTransactionType, AssetType
from dtos import (
    CryptoAccountCreate,
    CryptoAccountUpdate,
    CryptoAccountBasicResponse,
    CryptoBulkImportRequest,
    CryptoBulkImportResponse,
    CryptoBulkCompositeImportRequest,
    CryptoBulkCompositeImportResponse,
    CryptoCompositeTransactionCreate,
    CryptoCompositeTransactionResponse,
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
    CryptoTransactionBasicResponse,
    AccountSummaryResponse,
    TransactionResponse,
    AssetSearchResult,
    AssetInfoResponse,
    CrossAccountTransferCreate,
    BinanceImportPreviewRequest,
    BinanceImportPreviewResponse,
    BinanceImportConfirmRequest,
    BinanceImportConfirmResponse,
)
from services.imports.binance import generate_preview, execute_import
from services.crypto_account import (
    create_crypto_account,
    get_crypto_account,
    get_or_create_default_account,
    get_user_crypto_accounts,
    update_crypto_account,
    delete_crypto_account,
)
from services.settings import get_or_create_settings
from services.encryption import hash_index
from services.exchange_rate import get_effective_usd_eur_rate, convert_crypto_prices_to_eur
from dtos.crypto import FIAT_SYMBOLS
from services.crypto_transaction import (
    create_composite_crypto_transaction,
    create_cross_account_transfer,
    create_crypto_transaction,
    get_crypto_transaction,
    get_account_transactions,
    get_symbol_balance,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_crypto_account_summary,
    _DEBIT_TYPES,
)
from services.market_data.manager import market_data_manager

router = APIRouter(prefix="/crypto", tags=["Crypto"])


def _compute_balance_warning(
    session,
    account_uuid: str,
    created_rows,
    master_key: str,
    extra_account_for_symbols: dict[str, str] | None = None,
) -> str | None:
    """
    Check whether any debited crypto symbol has gone negative after the
    operation.  Returns a human-readable warning string or None.

    ``extra_account_for_symbols`` maps symbol → account_uuid to support
    cross-account checks (e.g., source account for a TRANSFER).
    """
    # Collect (symbol, account) pairs that were debited
    to_check: dict[str, str] = {}  # symbol → account_uuid
    for row in created_rows:
        type_str = row.type if isinstance(row.type, str) else row.type.value
        if type_str not in _DEBIT_TYPES:
            continue
        sym = (row.symbol or "").upper()
        if not sym or sym in FIAT_SYMBOLS:
            continue
        # Prefer the extra mapping when provided (cross-account TRANSFER row)
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
            negative.append(f"{sym} (solde : {balance:+.8g})")

    if not negative:
        return None
    return "Solde insuffisant après cette opération — " + ", ".join(negative)


# ============== ACCOUNTS ==============

@router.post("/accounts", response_model=CryptoAccountBasicResponse, status_code=201)
def create_account(
    data: CryptoAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new crypto account/wallet."""
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    if settings.crypto_module_enabled and settings.crypto_mode == "SINGLE":
        # In SINGLE mode only one account is allowed
        user_bidx = hash_index(current_user.uuid, master_key)
        existing = session.exec(
            select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
        ).first()
        if existing:
            raise HTTPException(
                status_code=403,
                detail="En mode portefeuille unique, un seul compte est autorisé.",
            )
    return create_crypto_account(session, data, current_user.uuid, master_key)


@router.get("/accounts", response_model=list[CryptoAccountBasicResponse])
def list_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all crypto accounts for current user."""
    return get_user_crypto_accounts(session, current_user.uuid, master_key)


@router.get("/accounts/default", response_model=AccountSummaryResponse)
def get_default_account(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Get (or transparently create) the single default account for SINGLE-mode users.
    Returns the full account summary with positions and calculated values.
    """
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    rate = get_effective_usd_eur_rate(
        float(settings.usd_eur_rate) if settings.usd_eur_rate is not None else None
    )
    account_model = get_or_create_default_account(session, current_user.uuid, master_key)
    summary = get_crypto_account_summary(session, account_model, master_key, settings.crypto_show_negative_positions)
    return convert_crypto_prices_to_eur(summary, rate)


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a crypto account with positions and calculated values."""
    account_basic = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not account_basic:
        raise HTTPException(status_code=404, detail="Account not found")

    account_model = session.get(CryptoAccount, account_id)
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    rate = get_effective_usd_eur_rate(
        float(settings.usd_eur_rate) if settings.usd_eur_rate is not None else None
    )
    summary = get_crypto_account_summary(session, account_model, master_key, settings.crypto_show_negative_positions)
    return convert_crypto_prices_to_eur(summary, rate)


@router.put("/accounts/{account_id}", response_model=CryptoAccountBasicResponse)
def update_account(
    account_id: str,
    data: CryptoAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a crypto account."""
    existing = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account_model = session.get(CryptoAccount, account_id)
    return update_crypto_account(session, account_model, data, master_key)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a crypto account and all its transactions."""
    existing = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    delete_crypto_account(session, account_id, master_key)
    return None


# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=CryptoTransactionBasicResponse, status_code=201)
def create_transaction(
    data: CryptoTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a single atomic crypto transaction."""
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    resp = create_crypto_transaction(session, data, master_key)

    return CryptoTransactionBasicResponse(
        id=resp.id,
        account_id=data.account_id,
        symbol=resp.symbol,
        type=data.type,
        amount=resp.amount,
        price_per_unit=resp.price_per_unit,
        executed_at=resp.executed_at,
        notes=data.notes,
        tx_hash=data.tx_hash,
    )


@router.post("/transactions/composite", response_model=CryptoCompositeTransactionResponse, status_code=201)
def create_composite_transaction(
    data: CryptoCompositeTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Create 1-3 atomic transactions from a single user form submission.

    This endpoint accepts the composite DTO (primary asset + optional quote leg
    + optional crypto fee leg) and decomposes it server-side into atomic rows
    that all share the same group_uuid.

    Use this endpoint from the frontend "Add transaction" modal.
    Use POST /transactions for direct atomic inserts (CSV import, etc.).
    """
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    created = create_composite_crypto_transaction(session, data, master_key)

    rows = [
        CryptoTransactionBasicResponse(
            id=tx.id,
            account_id=data.account_id,
            group_uuid=tx.group_uuid,
            symbol=tx.symbol,
            type=CryptoTransactionType(tx.type),
            amount=tx.amount,
            price_per_unit=tx.price_per_unit,
            executed_at=tx.executed_at,
            notes=tx.notes if hasattr(tx, "notes") else None,
            tx_hash=None,
        )
        for tx in created
    ]

    warning = _compute_balance_warning(session, data.account_id, created, master_key)
    return CryptoCompositeTransactionResponse(rows=rows, warning=warning)


@router.post("/transactions/cross-account-transfer", response_model=CryptoCompositeTransactionResponse, status_code=201)
def create_cross_account_transfer_route(
    data: CrossAccountTransferCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Transfer crypto between two accounts owned by the same user.
    Creates a TRANSFER row in the source and a BUY (price=0) row in the
    destination, plus an optional FEE row in the source.
    """
    try:
        created = create_cross_account_transfer(session, data, current_user.uuid, master_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rows = [
        CryptoTransactionBasicResponse(
            id=tx.id,
            account_id=data.from_account_id,
            group_uuid=tx.group_uuid,
            symbol=tx.symbol,
            type=CryptoTransactionType(tx.type),
            amount=tx.amount,
            price_per_unit=tx.price_per_unit,
            executed_at=tx.executed_at,
            notes=None,
            tx_hash=data.tx_hash,
        )
        for tx in created
    ]

    # For cross-account transfers the symbol is always debited from the source account
    warning = _compute_balance_warning(
        session,
        data.from_account_id,
        created,
        master_key,
        extra_account_for_symbols={data.symbol.upper(): data.from_account_id},
    )
    return CryptoCompositeTransactionResponse(rows=rows, warning=warning)


@router.get("/transactions", response_model=list[TransactionResponse])
def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all crypto transactions for current user (history)."""
    accounts = get_user_crypto_accounts(session, current_user.uuid, master_key)
    
    all_transactions = []
    for acc in accounts:
        txs = get_account_transactions(session, acc.id, master_key)
        all_transactions.extend(txs)
        
    all_transactions.sort(key=lambda x: x.executed_at, reverse=True)
    
    return all_transactions


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a specific crypto transaction."""
    transaction = get_crypto_transaction(session, transaction_id, master_key)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    return transaction


@router.put("/transactions/{transaction_id}", response_model=CryptoTransactionBasicResponse)
def update_transaction(
    transaction_id: str,
    data: CryptoTransactionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a crypto transaction."""
    tx_model = session.get(CryptoTransaction, transaction_id)
    if not tx_model:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    try:
        resp = update_crypto_transaction(session, tx_model, data, master_key)

        return CryptoTransactionBasicResponse(
            id=resp.id,
            account_id="unknown",  # account_id not stored on tx; caller knows it
            symbol=resp.symbol,
            type=CryptoTransactionType(resp.type),
            amount=resp.amount,
            price_per_unit=resp.price_per_unit,
            executed_at=resp.executed_at,
            notes=None,
            tx_hash=None,
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied")


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a crypto transaction."""
    tx = get_crypto_transaction(session, transaction_id, master_key)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    delete_crypto_transaction(session, transaction_id)
    return None


@router.get("/transactions/account/{account_id}", response_model=list[TransactionResponse])
def get_transactions_by_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all transactions for a specific account."""
    account = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return get_account_transactions(session, account_id, master_key)


@router.post("/transactions/bulk", response_model=CryptoBulkImportResponse, status_code=201)
def bulk_import_transactions(
    data: CryptoBulkImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Bulk import multiple crypto transactions."""
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    created_responses = []
    
    for item in data.transactions:
        create_dto = CryptoTransactionCreate(
            account_id=data.account_id,
            symbol=item.symbol,
            type=item.type,
            amount=item.amount,
            price_per_unit=item.price_per_unit,
            executed_at=item.executed_at,
            notes=item.notes,
            tx_hash=item.tx_hash,
        )

        resp = create_crypto_transaction(session, create_dto, master_key, group_uuid=item.group_uuid)

        basic = CryptoTransactionBasicResponse(
            id=resp.id,
            account_id=data.account_id,
            group_uuid=item.group_uuid,
            symbol=resp.symbol,
            type=item.type,
            amount=resp.amount,
            price_per_unit=resp.price_per_unit,
            executed_at=resp.executed_at,
            notes=item.notes,
            tx_hash=item.tx_hash,
        )
        created_responses.append(basic)

    return CryptoBulkImportResponse(
        imported_count=len(created_responses),
        transactions=created_responses
    )


@router.post("/transactions/bulk-composite", response_model=CryptoBulkCompositeImportResponse, status_code=201)
def bulk_composite_import_transactions(
    data: CryptoBulkCompositeImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Bulk import composite operations from a CSV file.

    Each item in `transactions` represents one user-facing operation
    (e.g. "Buy 0.1 BTC for 3000 EUR + 0.01 BNB fee") and is decomposed
    server-side into 1-4 atomic rows sharing a group_uuid, exactly like
    POST /transactions/composite.
    """
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    total_atomic_rows = 0

    for item in data.transactions:
        composite_dto = CryptoCompositeTransactionCreate(
            account_id=data.account_id,
            type=item.type,
            symbol=item.symbol,
            name=item.name,
            amount=item.amount,
            quote_symbol=item.quote_symbol,
            quote_amount=item.quote_amount,
            eur_amount=item.eur_amount,
            fee_included=item.fee_included,
            fee_symbol=item.fee_symbol,
            fee_amount=item.fee_amount,
            executed_at=item.executed_at,
            tx_hash=item.tx_hash,
            notes=item.notes,
        )
        created = create_composite_crypto_transaction(session, composite_dto, master_key)
        total_atomic_rows += len(created)

    return CryptoBulkCompositeImportResponse(
        imported_count=total_atomic_rows,
        groups_count=len(data.transactions),
    )


@router.get("/market/search", response_model=list[AssetSearchResult])
def search_assets(
    q: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Search for crypto assets by name or symbol."""
    if not q:
        return []
    results = market_data_manager.search(q, AssetType.CRYPTO)

    return [
        AssetSearchResult(
            symbol=r["symbol"],
            name=r.get("name"),
            exchange=r.get("exchange"),
            type=r.get("type"),
            currency=r.get("currency")
        ) for r in results
    ]


@router.post("/market/info", response_model=list[AssetInfoResponse])
def get_assets_info(
    symbols: list[str],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Get live data for multiple crypto assets."""
    if not symbols:
        return []

    data = market_data_manager.get_bulk_info(symbols, AssetType.CRYPTO)

    response = []
    for symbol, info in data.items():
        response.append(AssetInfoResponse(
            symbol=symbol,
            name=info.get("name"),
            price=info.get("price"),
            currency=info.get("currency"),
            exchange=info.get("exchange"),
            type=None
        ))
    return response


# ============== BINANCE IMPORT ==============

@router.post("/import/binance/preview", response_model=BinanceImportPreviewResponse)
def preview_binance_import(
    data: BinanceImportPreviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Parse a Binance CSV and return a preview of grouped transactions.

    Groups sharing the same UTC second receive a common group.
    Groups that need a manual EUR anchor are flagged with
    ``needs_eur_input = true``.
    """
    return generate_preview(data.csv_content)


@router.post("/import/binance/confirm", response_model=BinanceImportConfirmResponse, status_code=201)
def confirm_binance_import(
    data: BinanceImportConfirmRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Create all transactions from a validated Binance import preview.

    The frontend sends back the preview groups (possibly with
    ``eur_amount`` filled in) and the target ``account_id``.
    """
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Validate: every group that needs EUR must have eur_amount
    for g in data.groups:
        if g.needs_eur_input and (g.eur_amount is None or g.eur_amount < 0):
            raise HTTPException(
                status_code=422,
                detail=f"Le groupe #{g.group_index + 1} nécessite un montant EUR.",
            )

    return execute_import(session, data.account_id, data.groups, master_key)
