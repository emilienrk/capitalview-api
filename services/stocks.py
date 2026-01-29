"""Stock account and transaction services."""

from decimal import Decimal

from sqlmodel import Session, select

from models import StockAccount, StockTransaction
from schemas import (
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from services.market import get_market_price, get_market_info


def calculate_stock_transaction(tx: StockTransaction) -> TransactionResponse:
    """Calculate fields for a single stock transaction (history only)."""
    total_cost = tx.amount * tx.price_per_unit + tx.fees
    fees_pct = (tx.fees / total_cost * 100) if total_cost > 0 else Decimal("0")
    
    return TransactionResponse(
        id=tx.id,
        ticker=tx.ticker,
        type=tx.type.value,
        amount=tx.amount,
        price_per_unit=tx.price_per_unit,
        fees=tx.fees,
        executed_at=tx.executed_at,
        total_cost=total_cost,
        fees_percentage=round(fees_pct, 2),
    )


def aggregate_stock_positions(transactions: list[StockTransaction], session: Session) -> list[PositionResponse]:
    """Aggregate stock transactions into positions by ticker."""
    positions_map: dict[str, dict] = {}
    
    for tx in transactions:
        ticker = tx.ticker
        if ticker not in positions_map:
            positions_map[ticker] = {
                "ticker": ticker,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "total_fees": Decimal("0"),
            }
        
        # BUY/DIVIDEND adds, SELL subtracts
        if tx.type.value in ("BUY", "DIVIDEND", "DEPOSIT"):
            positions_map[ticker]["total_amount"] += tx.amount
            positions_map[ticker]["total_cost"] += tx.amount * tx.price_per_unit
        else:  # SELL
            positions_map[ticker]["total_amount"] -= tx.amount
            positions_map[ticker]["total_cost"] -= tx.amount * tx.price_per_unit
        
        positions_map[ticker]["total_fees"] += tx.fees
    
    positions = []
    for ticker, data in positions_map.items():
        if data["total_amount"] <= 0:
            continue
            
        total_invested = data["total_cost"] + data["total_fees"]
        avg_price = data["total_cost"] / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        fees_pct = (data["total_fees"] / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        name, current_price = get_market_info(session, ticker)
        current_value = None
        profit_loss = None
        profit_loss_pct = None
        
        if current_price:
            current_value = data["total_amount"] * current_price
            profit_loss = current_value - total_invested
            profit_loss_pct = (profit_loss / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        positions.append(PositionResponse(
            ticker=ticker,
            name=name,
            total_amount=data["total_amount"],
            average_buy_price=round(avg_price, 4),
            total_invested=round(total_invested, 2),
            total_fees=round(data["total_fees"], 2),
            fees_percentage=round(fees_pct, 2),
            current_price=current_price,
            current_value=round(current_value, 2) if current_value else None,
            profit_loss=round(profit_loss, 2) if profit_loss else None,
            profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None
        ))
    
    return positions


def get_stock_account_summary(session: Session, account: StockAccount) -> AccountSummaryResponse:
    """Get summary for a stock account with positions."""
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id == account.id)
    ).all()
    
    positions = aggregate_stock_positions(transactions, session)
    
    total_invested = sum(p.total_invested for p in positions)
    total_fees = sum(p.total_fees for p in positions)
    current_value = sum(p.current_value for p in positions if p.current_value)
    profit_loss = current_value - total_invested if current_value else None
    profit_loss_pct = (profit_loss / total_invested * 100) if profit_loss and total_invested > 0 else None
    
    return AccountSummaryResponse(
        account_id=account.id,
        account_name=account.name,
        account_type=account.account_type.value,
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        positions=positions
    )


def get_user_stock_summary(session: Session, user_id: int) -> list[AccountSummaryResponse]:
    """Get all stock account summaries for a user."""
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_id == user_id)
    ).all()
    return [get_stock_account_summary(session, acc) for acc in accounts]
