"""Stock accounts and transactions CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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
    AccountSummaryResponse,
    TransactionResponse,
    AssetSearchResult,
    AssetInfoResponse,
)
from services.stock_account import (
    create_stock_account,
    get_stock_account,
    get_user_stock_accounts,
    update_stock_account,
    delete_stock_account
)
from services.stock_transaction import (
    create_stock_transaction,
    get_stock_transaction,
    get_account_transactions,
    update_stock_transaction,
    delete_stock_transaction,
    get_stock_account_summary
)
from services.market_data.manager import market_data_manager

router = APIRouter(prefix="/stocks", tags=["Stocks"])


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


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a stock account with positions and calculated values."""
    account_basic = get_stock_account(session, account_id, current_user.uuid, master_key)
    if not account_basic:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account_model = session.get(StockAccount, account_id)
    
    return get_stock_account_summary(session, account_model, master_key)


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


# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=TransactionResponse, status_code=201)
def create_transaction(
    data: StockTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new stock transaction."""
    account = get_stock_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or access denied")
    
    try:
        return create_stock_transaction(session, data, master_key)
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
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a stock transaction."""
    tx_model = session.get(StockTransaction, transaction_id)
    if not tx_model:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    try:
        return update_stock_transaction(session, tx_model, data, master_key)
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied (Decryption failed)")


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a stock transaction."""
    tx = get_stock_transaction(session, transaction_id, master_key)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    delete_stock_transaction(session, transaction_id)
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
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Bulk import multiple stock transactions."""
    account = get_stock_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    created_responses = []
    
    for item in data.transactions:
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
        
        try:
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

    return StockBulkImportResponse(
        imported_count=len(created_responses),
        transactions=created_responses
    )


@router.get("/market/search", response_model=list[AssetSearchResult])
def search_assets(
    q: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Search for assets (stocks, ETFs, etc.) by name or symbol."""
    if not q:
        return []
    results = market_data_manager.search(q, AssetType.STOCK)

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
):
    """Get live data for multiple assets."""
    if not symbols:
        return []

    data = market_data_manager.get_bulk_info(symbols, AssetType.STOCK)

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
