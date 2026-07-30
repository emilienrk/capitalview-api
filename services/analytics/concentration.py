"""How many independent bets the portfolio actually holds.

Three numbers that diverge, and the divergence is the point: the number of lines
held, the effective number of positions (1/HHI on the weights, so a 2% line does
not count like a 40% one), and the effective number of *independent* bets — a
principal component analysis of the daily return covariance, followed by the
entropy of the variance contributions (Meucci 2009, "Managing Diversification").

**This is not a look-through.** The composition of an ETF is not stored, so this
cannot say "you hold Apple twice". It measures redundancy of *behaviour*: how
much the lines move together. That is a different measurement, and it is enough
for the verdict — adding a seventh world ETF changes nothing, only an
uncorrelated asset would.

Returns come from the sparse price matrix, never the forward-filled one. A
filled series repeats the previous close over weekends and holidays, so every
line would show a zero return on the same days and the correlations would be
inflated by the calendar rather than by the assets.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

# Spec section 2: a pair needs 250 overlapping daily returns before its
# correlation means anything.
MIN_OVERLAP = 250
MIN_ASSETS = 2


@dataclass(frozen=True)
class Concentration:
    lines: int
    effective_positions: Decimal | None
    """1/HHI on the weights."""
    independent_bets: Decimal | None
    """exp(-sum p ln p) over the variance contributions of the principal components."""
    weights: list[tuple[str, Decimal]]
    correlations: list[tuple[str, str, Decimal]]
    max_correlation: Decimal | None
    overlap: int
    """Common sessions used by the covariance, the binding sample size."""
    dropped: list[str]
    """Lines held but too thinly quoted to enter the analysis."""

    @property
    def is_measurable(self) -> bool:
        return self.independent_bets is not None


def portfolio_weights(holdings: dict[str, Decimal], prices: dict[str, Decimal]):
    """Value weights at the end of the window. Cash is not a bet, so it is out."""
    values = {
        key: quantity * prices[key]
        for key, quantity in holdings.items()
        if quantity > 0 and key in prices and prices[key] > 0
    }
    total = sum(values.values())
    if total <= 0:
        return []
    return sorted(
        ((key, value / total) for key, value in values.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )


def _common_sessions(quotes: dict[str, dict[date, Decimal]], keys: list[str]) -> list[date]:
    common: set[date] | None = None
    for key in keys:
        days = set(quotes.get(key) or {})
        common = days if common is None else common & days
    return sorted(common or [])


def _returns_matrix(quotes, keys: list[str], sessions: list[date]) -> np.ndarray:
    prices = np.array(
        [[float(quotes[key][day]) for day in sessions] for key in keys], dtype=np.float64
    )
    return np.diff(prices, axis=1) / prices[:, :-1]


def analyse_concentration(
    holdings: dict[str, Decimal],
    prices: dict[str, Decimal],
    quotes: dict[str, dict[date, Decimal]],
) -> Concentration | None:
    """Lines, effective positions, and independent bets."""
    weights = portfolio_weights(holdings, prices)
    if not weights:
        return None

    keys = [key for key, _ in weights]
    hhi = sum(weight**2 for _, weight in weights)
    effective = Decimal("1") / hhi if hhi > 0 else None

    usable = [key for key in keys if len(quotes.get(key) or {}) >= MIN_OVERLAP + 1]
    dropped = [key for key in keys if key not in usable]
    sessions = _common_sessions(quotes, usable) if len(usable) >= MIN_ASSETS else []
    overlap = max(len(sessions) - 1, 0)

    if len(usable) < MIN_ASSETS or overlap < MIN_OVERLAP:
        return Concentration(
            lines=len(weights),
            effective_positions=effective,
            independent_bets=None,
            weights=weights,
            correlations=[],
            max_correlation=None,
            overlap=overlap,
            dropped=dropped,
        )

    returns = _returns_matrix(quotes, usable, sessions)
    covariance = np.cov(returns)
    weight_vector = np.array(
        [float(dict(weights)[key]) for key in usable], dtype=np.float64
    )
    # Renormalised over the usable lines only, so the exposures still sum to one.
    total = weight_vector.sum()
    if total <= 0:
        return None
    weight_vector = weight_vector / total

    # A line that never moved has no correlation to report; computing one would
    # divide by a zero standard deviation and yield a NaN nobody can read.
    with np.errstate(invalid="ignore", divide="ignore"):
        correlation = np.corrcoef(returns)
    pairs: list[tuple[str, str, Decimal]] = []
    for i, left in enumerate(usable):
        for j, right in enumerate(usable):
            if j <= i:
                continue
            value = correlation[i, j]
            if np.isfinite(value):
                pairs.append((left, right, Decimal(str(round(float(value), 4)))))

    independent = _independent_bets(covariance, weight_vector)

    return Concentration(
        lines=len(weights),
        effective_positions=effective,
        independent_bets=independent,
        weights=weights,
        correlations=pairs,
        max_correlation=max((value for _, _, value in pairs), default=None),
        overlap=overlap,
        dropped=dropped,
    )


def _independent_bets(covariance: np.ndarray, weights: np.ndarray) -> Decimal | None:
    """Entropy of the portfolio's variance contributions across principal components.

    Uncorrelated equal bets put the same variance on each component and the
    entropy returns their count; perfectly correlated lines load one component and
    it returns one, whatever the number of lines held.
    """
    covariance = np.atleast_2d(covariance)
    if covariance.shape[0] != weights.size:
        return None
    try:
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    except np.linalg.LinAlgError:
        return None

    exposures = eigenvectors.T @ weights
    contributions = (exposures**2) * np.clip(eigenvalues, 0.0, None)
    total = contributions.sum()
    if not np.isfinite(total) or total <= 0:
        return None

    shares = contributions / total
    shares = shares[shares > 0]
    if shares.size == 0:
        return None
    entropy = -np.sum(shares * np.log(shares))
    return Decimal(str(round(float(np.exp(entropy)), 4)))


def holdings_from_transactions(transactions) -> dict[str, Decimal]:
    """Quantities still held per asset. Cash rows are not positions."""
    held: dict[str, Decimal] = {}
    for tx in transactions or ():
        raw = getattr(tx, "type", None)
        tx_type = str(getattr(raw, "value", raw) or "")
        key = str(getattr(tx, "asset_key", "") or "").upper()
        if not key or key == "EUR":
            continue
        amount = Decimal(str(getattr(tx, "amount", 0) or 0))
        if tx_type == "BUY":
            held[key] = held.get(key, Decimal("0")) + amount
        elif tx_type == "SELL":
            held[key] = held.get(key, Decimal("0")) - amount
    return {key: quantity for key, quantity in held.items() if quantity > 0}
