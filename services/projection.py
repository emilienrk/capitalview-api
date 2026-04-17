from datetime import date
from typing import Optional
from dateutil.relativedelta import relativedelta
from decimal import Decimal

from sqlmodel import Session, select

from models.user import User
from models.stock import StockAccount
from models.crypto import CryptoAccount
from models.enums import AccountCategory
from dtos.projection import (
    ProjectionAssetParametersUsed,
    ProjectionDataPoint,
    ProjectionParameters,
    ProjectionParametersUsed,
    ProjectionResponse,
)

from services.encryption import hash_index
from services.stock_transaction import (
    get_stock_account_summary,
    get_account_transactions as get_stock_transactions,
)
from services.crypto_transaction import (
    get_crypto_account_summary,
    get_account_transactions as get_crypto_transactions,
)


PROJECTED_CATEGORIES: tuple[AccountCategory, ...] = (
    AccountCategory.BANK,
    AccountCategory.STOCK,
    AccountCategory.CRYPTO,
)

ZERO_DECIMAL = Decimal("0")
ONE_DECIMAL = Decimal("1")
MONEY_PRECISION = Decimal("0.01")


def _to_decimal(value: float | Decimal | None) -> Decimal:
    if value is None:
        return ZERO_DECIMAL
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _round_money(value: Decimal) -> float:
    return float(value.quantize(MONEY_PRECISION))


def _get_history_stats(
    session: Session,
    user_bidx: str,
    master_key: str,
    category: AccountCategory,
) -> tuple[Decimal, Decimal, int]:
    """
    Get current value, invested amount, and days since first transaction.
    """
    current_value = ZERO_DECIMAL
    total_invested = ZERO_DECIMAL
    first_date: Optional[date] = None

    if category == AccountCategory.STOCK:
        models = session.exec(select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)).all()
    elif category == AccountCategory.CRYPTO:
        models = session.exec(select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)).all()
    else:
        return ZERO_DECIMAL, ZERO_DECIMAL, 0

    for acc in models:
        if category == AccountCategory.STOCK:
            transactions = get_stock_transactions(session, acc.uuid, master_key)
            summary = get_stock_account_summary(session, transactions)
        else:
            transactions = get_crypto_transactions(session, acc.uuid, master_key)
            summary = get_crypto_account_summary(session, transactions)

        if summary.current_value:
            current_value += summary.current_value

        total_invested += summary.total_invested

        if transactions:
            acc_first_date = min(tx.executed_at.date() for tx in transactions)
            if first_date is None or acc_first_date < first_date:
                first_date = acc_first_date

    days_elapsed = 0
    if first_date:
        days_elapsed = (date.today() - first_date).days

    return current_value, total_invested, days_elapsed


def _compute_defaults(value: Decimal, invested: Decimal, days: int) -> tuple[Decimal, float]:
    default_injection = ZERO_DECIMAL
    default_rate = 0.0

    if days > 0:
        months_elapsed = Decimal(str(max(days / 30.41, 1.0)))
        default_injection = invested / months_elapsed

        if invested > ZERO_DECIMAL and value > ZERO_DECIMAL:
            years_elapsed = max(days / 365.25, 0.1)
            rate = (float(value / invested)) ** (1.0 / years_elapsed) - 1.0
            default_rate = max(min(rate, 2.0), -0.99)

    return default_injection, float(default_rate)


def _to_monthly_rate(annual_rate: float) -> float:
    if annual_rate <= -1.0:
        return 0.0
    return (1.0 + annual_rate) ** (1.0 / 12.0) - 1.0


def _resolve_asset_params(
    params: ProjectionParameters,
    category: AccountCategory,
    default_injection: Decimal,
    default_rate: float,
) -> tuple[Decimal, float]:
    injection = default_injection
    rate = default_rate

    asset_params = params.assets.get(category)
    if asset_params is not None:
        if asset_params.monthly_injection is not None:
            injection = _to_decimal(asset_params.monthly_injection)
        if asset_params.return_rate is not None:
            rate = float(asset_params.return_rate)

    return injection, rate


def generate_wealth_projection(
    session: Session,
    user: User,
    master_key: str,
    params: ProjectionParameters
) -> ProjectionResponse:
    """
    Generate wealth projection based on historical data or given parameters.
    Returns empty data if the long-term PnL is negative.
    """
    user_bidx = hash_index(user.uuid, master_key)

    # 1. Current stats from existing investment accounts.
    history_stats: dict[AccountCategory, tuple[Decimal, Decimal, int]] = {
        AccountCategory.STOCK: _get_history_stats(session, user_bidx, master_key, AccountCategory.STOCK),
        AccountCategory.CRYPTO: _get_history_stats(session, user_bidx, master_key, AccountCategory.CRYPTO),
        AccountCategory.BANK: (ZERO_DECIMAL, ZERO_DECIMAL, 0),
    }

    # 2. Resolve effective params by category.
    used_injections: dict[AccountCategory, Decimal] = {}
    used_rates: dict[AccountCategory, float] = {}

    for category in PROJECTED_CATEGORIES:
        if category == AccountCategory.BANK:
            default_injection, default_rate = ZERO_DECIMAL, 0.02
        else:
            value, invested, days = history_stats[category]
            default_injection, default_rate = _compute_defaults(value, invested, days)

        used_injection, used_rate = _resolve_asset_params(
            params=params,
            category=category,
            default_injection=default_injection,
            default_rate=default_rate,
        )
        used_injections[category] = used_injection
        used_rates[category] = used_rate

    # 3. Projection loop.
    data_points: list[ProjectionDataPoint] = []

    current_values: dict[AccountCategory, Decimal] = {
        AccountCategory.BANK: ZERO_DECIMAL,
        AccountCategory.STOCK: history_stats[AccountCategory.STOCK][0],
        AccountCategory.CRYPTO: history_stats[AccountCategory.CRYPTO][0],
    }
    current_date = date.today()

    monthly_rates: dict[AccountCategory, Decimal] = {
        category: _to_decimal(_to_monthly_rate(rate))
        for category, rate in used_rates.items()
    }

    start_total = sum(current_values.values(), ZERO_DECIMAL)
    total_injected_during_projection = ZERO_DECIMAL
    final_total_before_round = ZERO_DECIMAL

    for _month in range(params.months_to_project + 1):
        total_value = sum(current_values.values(), ZERO_DECIMAL)
        final_total_before_round = total_value

        rounded_asset_values = {
            category: _round_money(current_values[category])
            for category in PROJECTED_CATEGORIES
        }

        data_points.append(ProjectionDataPoint(
            date=current_date,
            asset_values=rounded_asset_values,
            total_value=_round_money(total_value)
        ))

        current_date += relativedelta(months=1)

        # Apply rate then injection by category.
        for category in PROJECTED_CATEGORIES:
            current_values[category] = (
                current_values[category] * (ONE_DECIMAL + monthly_rates[category])
            ) + used_injections[category]
            total_injected_during_projection += used_injections[category]

    # 4. Check if negative long-term PnL
    final_total = final_total_before_round if data_points else ZERO_DECIMAL
    expected_minimum = start_total + total_injected_during_projection

    if final_total < expected_minimum:
        data_points = []  # Return empty array if projection yields loss

    parameters_used = ProjectionParametersUsed(
        months_to_project=params.months_to_project,
        assets={
            category: ProjectionAssetParametersUsed(
                monthly_injection=_round_money(used_injections[category]),
                return_rate=round(used_rates[category], 4),
            )
            for category in PROJECTED_CATEGORIES
        },
    )

    return ProjectionResponse(parameters_used=parameters_used, data=data_points)
