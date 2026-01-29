"""Portfolio calculation service."""

from decimal import Decimal
from sqlmodel import Session, select

from models import (
    StockAccount, StockTransaction,
    CryptoAccount, CryptoTransaction,
    MarketPrice
)
from schemas import (
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
    PortfolioResponse
)


def get_market_price(session: Session, symbol: str) -> Decimal | None:
    """Get current price for a symbol."""
    price = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()
    return price.current_price if price else None


def get_market_info(session: Session, symbol: str) -> tuple[str | None, Decimal | None]:
    """Get name and current price for a symbol."""
    market = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()
    if market:
        return market.name, market.current_price
    return None, None


def calculate_transaction(tx, current_price: Decimal | None) -> TransactionResponse:
    """Calculate fields for a single transaction."""
    total_cost = tx.amount * tx.price_per_unit + tx.fees
    fees_pct = (tx.fees / total_cost * 100) if total_cost > 0 else Decimal("0")
    
    current_value = None
    profit_loss = None
    profit_loss_pct = None
    
    if current_price:
        current_value = tx.amount * current_price
        profit_loss = current_value - total_cost
        profit_loss_pct = (profit_loss / total_cost * 100) if total_cost > 0 else Decimal("0")
    
    return TransactionResponse(
        id=tx.id,
        ticker=tx.ticker,
        type=tx.type.value if hasattr(tx.type, 'value') else str(tx.type),
        amount=tx.amount,
        price_per_unit=tx.price_per_unit,
        fees=tx.fees,
        executed_at=tx.executed_at,
        total_cost=total_cost,
        fees_percentage=round(fees_pct, 2),
        current_price=current_price,
        current_value=current_value,
        profit_loss=profit_loss,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None
    )


def aggregate_positions(transactions: list, session: Session) -> list[PositionResponse]:
    """Aggregate transactions into positions by ticker."""
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
        
        # BUY adds, SELL subtracts
        is_buy = tx.type.value in ("BUY", "STAKING") if hasattr(tx.type, 'value') else tx.type in ("BUY", "STAKING")
        
        if is_buy:
            positions_map[ticker]["total_amount"] += tx.amount
            positions_map[ticker]["total_cost"] += tx.amount * tx.price_per_unit
        else:
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
        
        # Get name and price from MarketPrice
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
    """Get summary for a stock account."""
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id == account.id)
    ).all()
    
    positions = aggregate_positions(transactions, session)
    
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


def get_crypto_account_summary(session: Session, account: CryptoAccount) -> AccountSummaryResponse:
    """Get summary for a crypto account."""
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id == account.id)
    ).all()
    
    positions = aggregate_positions(transactions, session)
    
    total_invested = sum(p.total_invested for p in positions)
    total_fees = sum(p.total_fees for p in positions)
    current_value = sum(p.current_value for p in positions if p.current_value)
    profit_loss = current_value - total_invested if current_value else None
    profit_loss_pct = (profit_loss / total_invested * 100) if profit_loss and total_invested > 0 else None
    
    return AccountSummaryResponse(
        account_id=account.id,
        account_name=account.name,
        account_type="CRYPTO",
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        positions=positions
    )


def get_user_portfolio(session: Session, user_id: int) -> PortfolioResponse:
    """Get complete portfolio for a user."""
    accounts = []
    
    # Stock accounts
    stock_accounts = session.exec(
        select(StockAccount).where(StockAccount.user_id == user_id)
    ).all()
    for acc in stock_accounts:
        accounts.append(get_stock_account_summary(session, acc))
    
    # Crypto accounts
    crypto_accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_id == user_id)
    ).all()
    for acc in crypto_accounts:
        accounts.append(get_crypto_account_summary(session, acc))
    
    # Aggregate totals
    total_invested = sum(a.total_invested for a in accounts)
    total_fees = sum(a.total_fees for a in accounts)
    current_value = sum(a.current_value for a in accounts if a.current_value)
    profit_loss = current_value - total_invested if current_value else None
    profit_loss_pct = (profit_loss / total_invested * 100) if profit_loss and total_invested > 0 else None
    
    return PortfolioResponse(
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        accounts=accounts
    )
