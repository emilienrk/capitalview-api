"""Crypto accounts and transactions CRUD routes."""

from typing import Annotated

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User, CryptoAccount, CryptoTransaction
from services.auth import get_current_user, get_master_key
from models.enums import CryptoCompositeTransactionType, CryptoTransactionType, AssetType
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
    PortfolioAccountSummaryResponse,
    AccountHistorySnapshotResponse,
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
    get_crypto_account_history,
    get_all_crypto_accounts_history,
)
from services.settings import get_or_create_settings
from services.encryption import decrypt_data, hash_index
from services.account_history import trigger_post_transaction_updates
from services.market import search_assets, get_assets_bulk_info
from services.crypto_transaction import (
    create_composite_crypto_transaction,
    create_cross_account_transfer,
    create_crypto_transaction,
    get_symbol_balance,
    get_crypto_transaction,
    get_account_transactions,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_crypto_account_summary,
    compute_balance_warning,
)
from dtos.crypto import FIAT_SYMBOLS

router = APIRouter(prefix="/crypto", tags=["Crypto"])


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


@router.get("/accounts/default", response_model=PortfolioAccountSummaryResponse)
def get_default_account(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Get (or transparently create) the single default account for SINGLE-mode users.
    Returns the full account summary with positions and calculated values.
    """
    account_model = get_or_create_default_account(session, current_user.uuid, master_key)

    transactions = get_account_transactions(session, account_model.uuid, master_key)
    summary = get_crypto_account_summary(session, transactions)
    
    # Decrypt account name
    account_name = decrypt_data(account_model.name_enc, master_key)
    
    # Create PortfolioAccountSummaryResponse with account metadata
    return PortfolioAccountSummaryResponse(
        account_id=account_model.uuid,
        account_name=account_name,
        account_type="CRYPTO",
        **summary.model_dump()
    )


@router.get("/history", response_model=list[AccountHistorySnapshotResponse])
def get_all_history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get aggregated historical snapshots across all crypto accounts."""
    return get_all_crypto_accounts_history(session, current_user.uuid, master_key)


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    db_only: bool = False,
):
    """Get a crypto account with positions and calculated values."""
    account_basic = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not account_basic:
        raise HTTPException(status_code=404, detail="Account not found")

    account_model = session.get(CryptoAccount, account_id)

    transactions = get_account_transactions(session, account_model.uuid, master_key)
    summary = get_crypto_account_summary(session, transactions, db_only=db_only)
    return summary


@router.get("/accounts/{account_id}/history", response_model=list[AccountHistorySnapshotResponse])
def get_account_history_route(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get historical daily snapshots for a crypto account."""
    if not get_crypto_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    return get_crypto_account_history(session, account_id, master_key)


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
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a single atomic crypto transaction."""
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    resp = create_crypto_transaction(session, data, master_key)

    executed_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=[executed_date],
        affected_assets=[resp.symbol]
    )

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
    background_tasks: BackgroundTasks,
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

    info: str | None = None
    composite_type = CryptoCompositeTransactionType.normalize(data.type)
    if composite_type == CryptoCompositeTransactionType.FIAT_DEPOSIT:
        eur_balance_before = get_symbol_balance(session, data.account_id, "EUR", master_key)
        if eur_balance_before < 0:
            negative_eur = abs(eur_balance_before).quantize(Decimal("0.01"))
            info = (
                "Info: votre solde EUR était négatif avant ce dépôt "
                f"({negative_eur} EUR)."
            )

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

    warning = compute_balance_warning(session, data.account_id, created, master_key)
    
    executed_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=[executed_date],
        affected_assets=[tx.symbol for tx in created]
    )

    return CryptoCompositeTransactionResponse(rows=rows, warning=warning, info=info)


@router.post("/transactions/cross-account-transfer", response_model=CryptoCompositeTransactionResponse, status_code=201)
def create_cross_account_transfer_route(
    data: CrossAccountTransferCreate,
    background_tasks: BackgroundTasks,
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
    warning = compute_balance_warning(
        session,
        data.from_account_id,
        created,
        master_key,
        extra_account_for_symbols={data.symbol.upper(): data.from_account_id},
    )

    executed_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
    transfer_symbols = [data.symbol] if data.symbol not in FIAT_SYMBOLS else []
    
    # Update for source account
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.from_account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=[executed_date],
        affected_assets=transfer_symbols
    )
    
    # Update for destination account (community positions and dates are handled idempotently if called sequentially, but to be clean we should probably only call it once for community positions, but that's fine)
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.to_account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=[executed_date],
        affected_assets=transfer_symbols
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
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a crypto transaction."""
    tx_model = session.get(CryptoTransaction, transaction_id)
    if not tx_model:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Capture old date before the update so we can rebuild from the earliest affected date.
    try:
        old_date = date.fromisoformat(decrypt_data(tx_model.executed_at_enc, master_key)[:10])
    except Exception:
        old_date = None

    try:
        resp = update_crypto_transaction(session, tx_model, data, master_key)

        new_date = data.executed_at.date() if data.executed_at and hasattr(data.executed_at, "date") else (
            data.executed_at if data.executed_at else None
        )
        
        trigger_post_transaction_updates(
            session=session,
            background_tasks=background_tasks,
            user_uuid=current_user.uuid,
            master_key=master_key,
            account_id_bidx=tx_model.account_id_bidx,
            asset_type=AssetType.CRYPTO,
            affected_dates=[old_date, new_date],
            affected_assets=[resp.symbol] if resp.symbol not in FIAT_SYMBOLS else []
        )

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
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a crypto transaction."""
    tx = get_crypto_transaction(session, transaction_id, master_key)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Capture impacted account/date/assets before deleting.
    tx_model = session.get(CryptoTransaction, transaction_id)
    account_id_bidx = tx_model.account_id_bidx if tx_model else None

    group_models: list[CryptoTransaction] = []
    if tx_model and tx_model.group_uuid:
        group_models = session.exec(
            select(CryptoTransaction).where(
                CryptoTransaction.group_uuid == tx_model.group_uuid,
            )
        ).all()
    elif tx_model:
        group_models = [tx_model]

    affected_dates: list[date] = []
    affected_assets: set[str] = set()
    for grouped_tx in group_models:
        try:
            executed_at = datetime.fromisoformat(
                decrypt_data(grouped_tx.executed_at_enc, master_key).replace("Z", "+00:00")
            )
            affected_dates.append(executed_at.date())
        except Exception:
            affected_dates.append(grouped_tx.created_at.date())

        try:
            symbol = decrypt_data(grouped_tx.symbol_enc, master_key)
            if symbol not in FIAT_SYMBOLS:
                affected_assets.add(symbol)
        except Exception:
            pass

    delete_crypto_transaction(session, transaction_id)
    
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id_bidx=account_id_bidx,
        asset_type=AssetType.CRYPTO,
        affected_dates=list(dict.fromkeys(affected_dates)) if affected_dates else [],
        affected_assets=sorted(affected_assets)
    )
    
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
    background_tasks: BackgroundTasks,
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

    past_dates = [
        item.executed_at.date() if hasattr(item.executed_at, "date") else item.executed_at
        for item in data.transactions
    ]
    
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=past_dates,
        affected_assets=[item.symbol for item in data.transactions if item.symbol not in FIAT_SYMBOLS]
    )

    return CryptoBulkImportResponse(
        imported_count=len(created_responses),
        transactions=created_responses
    )


@router.post("/transactions/bulk-composite", response_model=CryptoBulkCompositeImportResponse, status_code=201)
def bulk_composite_import_transactions(
    data: CryptoBulkCompositeImportRequest,
    background_tasks: BackgroundTasks,
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

    past_dates = [
        item.executed_at.date() if hasattr(item.executed_at, "date") else item.executed_at
        for item in data.transactions
    ]
    
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=past_dates,
        affected_assets=[item.symbol for item in data.transactions if item.symbol not in FIAT_SYMBOLS]
    )

    return CryptoBulkCompositeImportResponse(
        imported_count=total_atomic_rows,
        groups_count=len(data.transactions),
    )


@router.get("/market/search", response_model=list[AssetSearchResult])
def search_crypto_assets(
    q: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Search for crypto assets by name or symbol."""
    if not q:
        return []
    results = search_assets(q, AssetType.CRYPTO)

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
def get_crypto_assets_info(
    symbols: list[str],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Get live data for multiple crypto assets."""
    if not symbols:
        return []

    data = get_assets_bulk_info(session, symbols, AssetType.CRYPTO)

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
    background_tasks: BackgroundTasks,
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

    result = execute_import(session, data.account_id, data.groups, master_key)
    
    # Extract affected dates and symbols
    past_dates = []
    affected_assets = set()
    for g in data.groups:
        try:
            executed_date = date.fromisoformat(g.timestamp[:10])
            past_dates.append(executed_date)
        except Exception:
            pass
        for row in g.rows:
            if row.mapped_symbol and row.mapped_symbol not in FIAT_SYMBOLS:
                affected_assets.add(row.mapped_symbol)
                
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=data.account_id,
        asset_type=AssetType.CRYPTO,
        affected_dates=past_dates,
        affected_assets=list(affected_assets)
    )
    
    return result
