"""Stock accounts and transactions CRUD routes."""

from typing import Annotated

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User, StockAccount, StockTransaction
from services.auth import get_current_user, get_master_key
from models.enums import AssetType
from dtos import (
    StockAccountCreate,
    StockAccountUpdate,
    StockAccountBasicResponse,
    StockBulkImportRequest,
    StockBulkImportResponse,
    StockTransactionCreate,
    StockTransactionUpdate,
    StockTransactionBasicResponse,
    EurDepositCreate,
    AccountSummaryResponse,
    AccountHistorySnapshotResponse,
    TransactionResponse,
    AssetSearchResult,
    AssetInfoResponse,
)
from services.stock_account import (
    create_stock_account,
    get_stock_account,
    get_user_stock_accounts,
    update_stock_account,
    delete_stock_account,
    get_stock_account_history,
    get_all_stock_accounts_history,
)
from services.stock_transaction import (
    create_stock_transaction,
    create_eur_deposit,
    get_stock_transaction,
    get_account_transactions,
    update_stock_transaction,
    delete_stock_transaction,
    get_stock_account_summary
)
from services.market import search_assets as _search_assets_svc, get_assets_bulk_info
from services.account_history import trigger_post_transaction_updates
from services.encryption import decrypt_data, hash_index

router = APIRouter(prefix="/stocks", tags=["Stocks"])


def _bulk_tx_order_key(item_with_index: tuple[int, object]) -> tuple:
    """Deterministic processing order for bulk stock imports.

    We replay transactions chronologically so SELL validation sees prior BUY rows.
    For identical timestamps, we process cash/funding first, then inventory changes.
    """
    index, item = item_with_index
    type_priority = {
        "DEPOSIT": 0,
        "DIVIDEND": 1,
        "BUY": 2,
        "SELL": 3,
    }
    raw_executed_at = getattr(item, "executed_at", None)
    if isinstance(raw_executed_at, datetime):
        executed_at = (
            raw_executed_at.replace(tzinfo=timezone.utc)
            if raw_executed_at.tzinfo is None
            else raw_executed_at.astimezone(timezone.utc)
        ).isoformat()
        date_rank = 0
    else:
        executed_at = ""
        date_rank = 1

    item_type = item.type.value if hasattr(item.type, "value") else str(item.type)
    return (date_rank, executed_at, type_priority.get(item_type, 99), index)


# ============== ACCOUNTS ==============

@router.post("/accounts", response_model=StockAccountBasicResponse, status_code=201)
def create_account(
    data: StockAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new stock account."""
    user_accounts = get_user_stock_accounts(session, current_user.uuid, master_key)
    
    unique_types = {"PEA", "PEA_PME"}
    
    if data.account_type.value in unique_types:
        for acc in user_accounts:
            if acc.account_type.value == data.account_type.value:
                raise HTTPException(
                    status_code=400,
                    detail=f"You already have a {data.account_type.value} account."
                )
    
    return create_stock_account(session, data, current_user.uuid, master_key)


@router.get("/accounts", response_model=list[StockAccountBasicResponse])
def list_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all stock accounts."""
    return get_user_stock_accounts(session, current_user.uuid, master_key)


@router.get("/history", response_model=list[AccountHistorySnapshotResponse])
def get_all_history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get aggregated historical snapshots across all stock accounts."""
    return get_all_stock_accounts_history(session, current_user.uuid, master_key)


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    db_only: bool = False,
):
    """Get a stock account with positions and calculated values."""
    account_basic = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not account_basic:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account_model = session.get(StockAccount, account_id)

    #acc_resp = _map_account_to_response(account_model, master_key)

    transactions = get_account_transactions(session, account_model.uuid, master_key)
    
    return get_stock_account_summary(session, transactions, db_only=db_only)


@router.get("/accounts/{account_id}/history", response_model=list[AccountHistorySnapshotResponse])
def get_account_history_route(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get historical daily snapshots for a stock account."""
    if not get_stock_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    return get_stock_account_history(session, account_id, master_key)


@router.put("/accounts/{account_id}", response_model=StockAccountBasicResponse)
def update_account(
    account_id: str,
    data: StockAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a stock account."""
    existing = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account_model = session.get(StockAccount, account_id)
    return update_stock_account(session, account_model, data, master_key)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a stock account and all its transactions."""
    existing = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    delete_stock_account(session, account_id, master_key)
    return None


@router.post("/accounts/{account_id}/deposit", response_model=TransactionResponse, status_code=201)
def create_account_deposit(
    account_id: str,
    data: EurDepositCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Deposit EUR cash into a stock account."""
    account = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or access denied")

    result = create_eur_deposit(
        session, account_id, data.amount, data.executed_at, master_key, data.notes, data.fees
    )

    executed_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id=account_id,
        asset_type=AssetType.STOCK,
        affected_dates=[executed_date],
        affected_assets=["EUR"],
    )

    return result


# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=TransactionResponse, status_code=201)
def create_transaction(
    data: StockTransactionCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new stock transaction."""
    account = get_stock_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or access denied")
    
    try:
        result = create_stock_transaction(session, data, master_key)
        
        executed_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
        trigger_post_transaction_updates(
            session=session,
            background_tasks=background_tasks,
            user_uuid=current_user.uuid,
            master_key=master_key,
            account_id=data.account_id,
            asset_type=AssetType.STOCK,
            affected_dates=[executed_date],
            affected_assets=[data.isin]
        )
        
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/transactions", response_model=list[TransactionResponse])
def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all stock transactions for current user (history)."""
    accounts = get_user_stock_accounts(session, current_user.uuid, master_key)
    
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
    """Get a specific stock transaction."""
    transaction = get_stock_transaction(session, transaction_id, master_key)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    return transaction


@router.put("/transactions/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    transaction_id: str,
    data: StockTransactionUpdate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a stock transaction."""
    tx_model = session.get(StockTransaction, transaction_id)
    if not tx_model:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Capture old executed_at before the update so we can rebuild from the earliest affected date.
    try:
        old_date = date.fromisoformat(decrypt_data(tx_model.executed_at_enc, master_key)[:10])
    except Exception:
        old_date = None

    try:
        result = update_stock_transaction(session, tx_model, data, master_key)
        
        new_date = data.executed_at.date() if hasattr(data.executed_at, "date") else data.executed_at
        
        trigger_post_transaction_updates(
            session=session,
            background_tasks=background_tasks,
            user_uuid=current_user.uuid,
            master_key=master_key,
            account_id_bidx=tx_model.account_id_bidx,
            asset_type=AssetType.STOCK,
            affected_dates=[old_date, new_date],
            affected_assets=[result.isin]
        )
        
        return result
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied (Decryption failed)")


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: str,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a stock transaction."""
    tx = get_stock_transaction(session, transaction_id, master_key)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Capture account and date before deleting.
    tx_model = session.get(StockTransaction, transaction_id)
    account_id_bidx = tx_model.account_id_bidx if tx_model else None
    executed_date = tx.executed_at.date() if tx.executed_at and hasattr(tx.executed_at, "date") else None

    delete_stock_transaction(session, transaction_id)
    
    trigger_post_transaction_updates(
        session=session,
        background_tasks=background_tasks,
        user_uuid=current_user.uuid,
        master_key=master_key,
        account_id_bidx=account_id_bidx,
        asset_type=AssetType.STOCK,
        affected_dates=[executed_date],
        affected_assets=[tx.isin]
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
    account = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return get_account_transactions(session, account_id, master_key)


@router.post("/transactions/bulk", response_model=StockBulkImportResponse, status_code=201)
def bulk_import_transactions(
    data: StockBulkImportRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Bulk import multiple stock transactions."""
    account = get_stock_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    created_responses = []
    ordered_transactions = [
        item for _, item in sorted(enumerate(data.transactions), key=_bulk_tx_order_key)
    ]

    for item in ordered_transactions:
        try:
            if item.type.value == "DEPOSIT":
                resp = create_eur_deposit(
                    session=session,
                    account_uuid=data.account_id,
                    amount=item.amount,
                    executed_at=item.executed_at,
                    master_key=master_key,
                    notes=item.notes,
                )
            else:
                create_dto = StockTransactionCreate(
                    account_id=data.account_id,
                    symbol=None,
                    isin=item.isin,
                    name=None,
                    exchange=None,
                    type=item.type,
                    amount=item.amount,
                    price_per_unit=item.price_per_unit,
                    fees=item.fees,
                    executed_at=item.executed_at,
                    notes=item.notes
                )
                resp = create_stock_transaction(session, create_dto, master_key)
            
            basic = StockTransactionBasicResponse(
                id=resp.id,
                account_id=data.account_id,
                symbol=resp.symbol,
                isin=resp.isin,
                name=resp.name,
                exchange=resp.exchange,
                type=resp.type,
                amount=resp.amount,
                price_per_unit=resp.price_per_unit,
                fees=resp.fees,
                executed_at=resp.executed_at,
                notes=None 
            )
            created_responses.append(basic)
        except ValueError:
            continue

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
        asset_type=AssetType.STOCK,
        affected_dates=past_dates,
        affected_assets=[item.isin for item in data.transactions if item.isin]
    )

    return StockBulkImportResponse(
        imported_count=len(created_responses),
        transactions=created_responses
    )


@router.get("/market/search", response_model=list[AssetSearchResult])
def search_market_assets(
    q: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Search for assets (stocks, ETFs, etc.) by name or symbol."""
    if not q:
        return []
    results = _search_assets_svc(q, AssetType.STOCK)

    return [
        AssetSearchResult(
            symbol=r["symbol"],
            isin=r.get("isin"),
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
    session: Session = Depends(get_session),
):
    """Get live data for multiple assets."""
    if not symbols:
        return []

    data = get_assets_bulk_info(session, symbols, AssetType.STOCK)

    response = []
    for symbol, info in data.items():
        response.append(AssetInfoResponse(
            symbol=symbol,
            isin=info.get("isin"),
            name=info.get("name"),
            price=info.get("price"),
            currency=info.get("currency"),
            exchange=info.get("exchange"),
            type=None
        ))
    return response
