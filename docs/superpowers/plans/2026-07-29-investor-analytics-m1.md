# Analyse comportementale — Jalon M1 : l'écart investisseur

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Livrer la page `/analyse` avec une seule métrique en ligne — l'écart investisseur (MWR − TWR), chiffré en euros — plus toute la plomberie dont les jalons M2/M3 dépendent.

**Architecture:** Un package `services/analytics/` de fonctions pures (entrée : transactions déchiffrées + séries de prix ; sortie : nombres), un endpoint unique `GET /analytics/investor`, une page Vue dédiée. Le calcul du flux externe, aujourd'hui enfoui dans `account_history.py`, devient la source unique partagée entre les snapshots et l'analytics.

**Tech Stack:** FastAPI · SQLModel · Alembic · pytest (backend) — Vue 3 Composition API · Pinia · Tailwind v4 · vitest (frontend).

**Spec:** `capitalview-api/docs/superpowers/specs/2026-07-29-investor-behaviour-analytics-design.md`

## Global Constraints

- **Deux dépôts git distincts.** `capitalview-api` et `capitalview-web` sont des repos séparés. Créer une branche dans chacun **avant** la première tâche qui le touche : `git checkout -b feat/investor-analytics`. Ne jamais committer sur `main` directement.
- **Commits** : conventional commits en anglais, scope quand il est évident, **2-3 lignes maximum**, pas de liste à puces, pas de trailer, pas de co-author.
- **Commentaires en anglais**, peu nombreux, uniquement là où le *pourquoi* n'est pas évident. Respecter la densité du fichier existant.
- **Ne rien casser** : `Stock.vue`, les services stock existants et le calcul des 8 encarts ne sont pas modifiés. La suite de tests existante doit passer inchangée.
- **Pas de nouvelle dépendance en M1.** Le TWR et le XIRR sont du calcul pur. numpy sera ajouté explicitement à `pyproject.toml` au jalon M3 (ACP), pas avant — il n'est aujourd'hui présent que transitivement via yfinance/exchange-calendars, sur quoi on ne s'appuie pas.
- **Argent** : `Decimal` partout côté stockage et API. Le solveur XIRR travaille en `float` en interne (Decimal ne fait pas de puissance fractionnaire) et reconvertit en `Decimal` en sortie — c'est documenté dans la docstring.
- **Benchmark par défaut** : `IE00B4L5Y983` (iShares Core MSCI World UCITS ETF USD Acc — **capitalisant**, contrainte du §7.1 de la spec).
- **Tests backend** : `uv run pytest` nécessite le sandbox désactivé (cache uv bloqué).
- **Build frontend** : `node` n'est pas sur le PATH. Préfixer avec `export PATH="$(dirname $(head -1 $(which pnpm) | sed 's|^#!||'))':$PATH"` ou le chemin nix connu, puis `pnpm test` / `pnpm type-check`.

---

## Structure des fichiers

| Fichier | Responsabilité |
|---|---|
| `capitalview-api/services/analytics/reliability.py` | Le wrapper `Metric` et ses seuils. Aucune dépendance. |
| `capitalview-api/services/analytics/flows.py` | Flux externes EUR par jour. **Source unique**, consommée par l'analytics et par `account_history.py`. |
| `capitalview-api/services/analytics/returns.py` | TWR journalier chaîné, XIRR par bissection. |
| `capitalview-api/services/analytics/benchmark.py` | Résolution et lecture de la série benchmark. |
| `capitalview-api/services/analytics/report.py` | Assemblage du DTO, traduction en euros, rédaction des verdicts. |
| `capitalview-api/dtos/analytics.py` | DTOs de réponse. |
| `capitalview-api/routes/analytics.py` | `GET /analytics/investor`. |
| `capitalview-web/src/components/analytics/ReliabilityBadge.vue` | Matérialise les trois statuts. Réutilisé par tous les blocs M2/M3. |
| `capitalview-web/src/pages/Analysis.vue` | La page. |
| `capitalview-web/src/stores/analysis.ts` | Fetch + cache. |

---

## Task 1: Cadre de fiabilité

**Files:**
- Create: `capitalview-api/services/analytics/__init__.py` (vide)
- Create: `capitalview-api/services/analytics/reliability.py`
- Test: `capitalview-api/tests/services/analytics/test_reliability.py`

**Interfaces:**
- Consumes: rien.
- Produces: `Reliability` (enum : `SOLID="solide"`, `INDICATIVE="indicatif"`, `INSUFFICIENT="insuffisant"`), `Metric` (dataclass frozen : `value: Decimal | None`, `unit: str`, `sample_size: int`, `reliability: Reliability`, `caveat: str | None`), et le constructeur `Metric.gated(value, unit, sample_size, *, minimum, solid_at, caveat_insufficient, caveat_indicative=None) -> Metric`.

- [x] **Step 1: Créer la branche et le package**

```bash
cd capitalview-api && git checkout -b feat/investor-analytics
mkdir -p services/analytics tests/services/analytics
touch services/analytics/__init__.py
```

- [x] **Step 2: Écrire le test qui échoue**

Créer `capitalview-api/tests/services/analytics/test_reliability.py` :

```python
from decimal import Decimal

from services.analytics.reliability import Metric, Reliability


def _gated(sample_size: int) -> Metric:
    return Metric.gated(
        Decimal("0.14"),
        unit="ratio",
        sample_size=sample_size,
        minimum=10,
        solid_at=30,
        caveat_insufficient="Moins de 10 observations.",
        caveat_indicative="Entre 10 et 30 observations : tendance, pas preuve.",
    )


def test_below_minimum_drops_the_value_entirely():
    metric = _gated(9)
    assert metric.reliability is Reliability.INSUFFICIENT
    assert metric.value is None
    assert metric.caveat == "Moins de 10 observations."


def test_between_thresholds_keeps_value_but_flags_it():
    metric = _gated(15)
    assert metric.reliability is Reliability.INDICATIVE
    assert metric.value == Decimal("0.14")
    assert metric.caveat == "Entre 10 et 30 observations : tendance, pas preuve."


def test_at_solid_threshold_is_solid_without_caveat():
    metric = _gated(30)
    assert metric.reliability is Reliability.SOLID
    assert metric.value == Decimal("0.14")
    assert metric.caveat is None


def test_none_value_is_always_insufficient():
    metric = Metric.gated(
        None,
        unit="ratio",
        sample_size=999,
        minimum=10,
        solid_at=30,
        caveat_insufficient="Non calculable.",
    )
    assert metric.reliability is Reliability.INSUFFICIENT
    assert metric.value is None
```

- [x] **Step 3: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/services/analytics/test_reliability.py -v` (sandbox désactivé)
Expected: FAIL — `ModuleNotFoundError: No module named 'services.analytics.reliability'`

- [x] **Step 4: Écrire l'implémentation**

Créer `capitalview-api/services/analytics/reliability.py` :

```python
"""Reliability gating for investor analytics.

Two years of retail data is a small sample. The risk is not computing a number
wrongly, it is displaying a correct number with false confidence. Every metric
therefore carries how far it can be trusted, and a metric below its minimum
sample has its value dropped here rather than in the UI — an unreliable figure
that never reaches the client cannot be misread.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Reliability(str, Enum):
    SOLID = "solide"
    INDICATIVE = "indicatif"
    INSUFFICIENT = "insuffisant"


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    unit: str
    sample_size: int
    reliability: Reliability
    caveat: str | None = None

    @classmethod
    def gated(
        cls,
        value: Decimal | None,
        *,
        unit: str,
        sample_size: int,
        minimum: int,
        solid_at: int,
        caveat_insufficient: str,
        caveat_indicative: str | None = None,
    ) -> "Metric":
        if value is None or sample_size < minimum:
            return cls(None, unit, sample_size, Reliability.INSUFFICIENT, caveat_insufficient)
        if sample_size < solid_at:
            return cls(value, unit, sample_size, Reliability.INDICATIVE, caveat_indicative)
        return cls(value, unit, sample_size, Reliability.SOLID, None)
```

Note : le test appelle `Metric.gated(value, unit=..., ...)` avec `value` positionnel — garder `value` en positionnel et tout le reste en keyword-only.

- [x] **Step 5: Lancer les tests**

Run: `uv run pytest tests/services/analytics/test_reliability.py -v`
Expected: 4 PASSED

- [x] **Step 6: Commit**

```bash
git add services/analytics tests/services/analytics
git commit -m "feat(analytics): add reliability gating for investor metrics"
```

---

## Task 2: Flux externes — source unique (refactor R1)

**Files:**
- Create: `capitalview-api/services/analytics/flows.py`
- Modify: `capitalview-api/services/account_history.py:362-373` (branche STOCK de `_compute_daily_net_flow`)
- Test: `capitalview-api/tests/services/analytics/test_flows.py`

**Interfaces:**
- Consumes: rien.
- Produces:
  - `AUTO_PROVISION_NOTE: str = "Provision automatique"`
  - `is_auto_provision(tx) -> bool`
  - `stock_external_flow_for_day(transactions, day: date, *, include_auto_provisions: bool = True) -> Decimal`
  - `stock_external_flows(transactions, *, include_auto_provisions: bool = True) -> dict[date, Decimal]`

Les `transactions` sont des `dtos.transaction.TransactionResponse` (attributs `asset_key`, `type`, `amount`, `executed_at`, `notes`).

- [x] **Step 1: Écrire le test qui échoue**

Créer `capitalview-api/tests/services/analytics/test_flows.py` :

```python
from datetime import date, datetime
from decimal import Decimal

from services.analytics.flows import (
    is_auto_provision,
    stock_external_flow_for_day,
    stock_external_flows,
)


class _Tx:
    """Minimal stand-in for TransactionResponse — the flow helpers only read these fields."""

    def __init__(self, tx_type, asset_key, amount, day, notes=None):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = notes


D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)


def test_deposit_is_positive_and_withdraw_is_negative():
    txs = [
        _Tx("DEPOSIT", "EUR", 1000, D1),
        _Tx("WITHDRAW", "EUR", 250, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("750")


def test_buys_and_sells_are_not_external_flows():
    txs = [
        _Tx("BUY", "IE00B4L5Y983", 5, D1),
        _Tx("SELL", "IE00B4L5Y983", 2, D1),
        _Tx("DIVIDEND", "IE00B4L5Y983", 3, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("0")


def test_flows_are_grouped_by_day():
    txs = [
        _Tx("DEPOSIT", "EUR", 1000, D1),
        _Tx("DEPOSIT", "EUR", 400, D2),
        _Tx("WITHDRAW", "EUR", 100, D2),
    ]
    assert stock_external_flows(txs) == {D1: Decimal("1000"), D2: Decimal("300")}


def test_auto_provisions_are_detected():
    auto = _Tx("DEPOSIT", "EUR", 500, D1, notes="Provision automatique")
    manual = _Tx("DEPOSIT", "EUR", 500, D1, notes="Virement mensuel")
    assert is_auto_provision(auto) is True
    assert is_auto_provision(manual) is False


def test_auto_provisions_included_by_default_excluded_on_request():
    txs = [
        _Tx("DEPOSIT", "EUR", 500, D1, notes="Provision automatique"),
        _Tx("DEPOSIT", "EUR", 300, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("800")
    assert stock_external_flow_for_day(txs, D1, include_auto_provisions=False) == Decimal("300")
    assert stock_external_flows(txs, include_auto_provisions=False) == {D1: Decimal("300")}


def test_days_without_external_flow_are_absent_from_the_mapping():
    txs = [_Tx("BUY", "IE00B4L5Y983", 5, D1)]
    assert stock_external_flows(txs) == {}
```

- [x] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/services/analytics/test_flows.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.analytics.flows'`

- [x] **Step 3: Écrire l'implémentation**

Créer `capitalview-api/services/analytics/flows.py` :

```python
"""External EUR cash flows for a stock account — the single definition.

Both the daily snapshot rebuild and the investor analytics need to know how much
money entered or left an account on a given day. Two definitions would drift, and
TWR would silently stop matching the stored history.

Auto-provisions are rows the app writes itself one second before a BUY when cash
is short (see services/stock_transaction.py). They are bookkeeping, not decisions:
they are dated on the purchase, not on the real transfer. Snapshot rebuilding
keeps them because they move the account's cash; the analytics drops them.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable

AUTO_PROVISION_NOTE = "Provision automatique"

_ZERO = Decimal("0")


def _to_decimal(value: object) -> Decimal:
    """Best-effort conversion, mirroring account_history._to_decimal.

    Swallowing a bad conversion rather than raising is deliberate: this function
    replaces logic that already behaved that way, and a refactor that promises no
    behaviour change must not introduce a new failure mode.
    """
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx) -> date | None:
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def is_auto_provision(tx) -> bool:
    """True for a cash row the app generated itself to cover a BUY shortfall."""
    if _tx_type(tx) != "DEPOSIT":
        return False
    if str(getattr(tx, "asset_key", "") or "").upper() != "EUR":
        return False
    return (getattr(tx, "notes", None) or "").strip() == AUTO_PROVISION_NOTE


def _signed_flow(tx, include_auto_provisions: bool) -> Decimal:
    if str(getattr(tx, "asset_key", "") or "").upper() != "EUR":
        return _ZERO
    if not include_auto_provisions and is_auto_provision(tx):
        return _ZERO

    amount = _to_decimal(getattr(tx, "amount", _ZERO))
    match _tx_type(tx):
        case "DEPOSIT":
            return amount
        case "WITHDRAW":
            return -amount
        case _:
            return _ZERO


def stock_external_flow_for_day(
    transactions: Iterable[object],
    day: date,
    *,
    include_auto_provisions: bool = True,
) -> Decimal:
    """Signed external EUR flow for a single day. Positive = money entering."""
    total = _ZERO
    for tx in transactions or ():
        if _tx_day(tx) != day:
            continue
        total += _signed_flow(tx, include_auto_provisions)
    return total


def stock_external_flows(
    transactions: Iterable[object],
    *,
    include_auto_provisions: bool = True,
) -> dict[date, Decimal]:
    """Signed external EUR flow per day. Days with no net flow are omitted."""
    grouped: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    for tx in transactions or ():
        day = _tx_day(tx)
        if day is None:
            continue
        flow = _signed_flow(tx, include_auto_provisions)
        if flow != _ZERO:
            grouped[day] += flow
    return {day: total for day, total in grouped.items() if total != _ZERO}
```

- [x] **Step 4: Lancer les tests**

Run: `uv run pytest tests/services/analytics/test_flows.py -v`
Expected: 6 PASSED

- [x] **Step 5: Rebrancher `account_history.py` sur la source unique**

Dans `capitalview-api/services/account_history.py`, remplacer la branche STOCK de `_compute_daily_net_flow` (actuellement lignes 362-373) :

```python
    if account_snapshot.account_type == AccountCategory.STOCK:
        for tx in day_txs:
            asset_key = str(getattr(tx, "asset_key", "") or "").upper()
            if asset_key != "EUR":
                continue
            amount = _to_decimal(getattr(tx, "amount", _ZERO))
            match _type(tx):
                case "DEPOSIT":
                    net_flow += amount
                case "WITHDRAW":
                    net_flow -= amount
        return net_flow
```

par :

```python
    if account_snapshot.account_type == AccountCategory.STOCK:
        return stock_external_flow_for_day(day_txs, d)
```

et ajouter l'import en tête de fichier, avec les autres imports de services :

```python
from services.analytics.flows import stock_external_flow_for_day
```

`include_auto_provisions` reste à son défaut `True` : le comportement des snapshots est strictement inchangé.

- [x] **Step 6: Vérifier la non-régression**

Run: `uv run pytest tests/services/test_account_history.py tests/services/test_history_services.py -v`
Expected: tous PASSED, aucun test modifié.

- [x] **Step 7: Lancer la suite backend complète**

Run: `uv run pytest -q`
Expected: même nombre de succès qu'avant la tâche, 0 échec.

- [x] **Step 8: Commit**

```bash
git add services/analytics/flows.py services/account_history.py tests/services/analytics/test_flows.py
git commit -m "refactor(analytics): extract stock external flows as the single definition"
```

---

## Task 3: TWR et XIRR

**Files:**
- Create: `capitalview-api/services/analytics/returns.py`
- Test: `capitalview-api/tests/services/analytics/test_returns.py`

**Interfaces:**
- Consumes: rien.
- Produces:
  - `TwrResult` (dataclass frozen : `total_return: Decimal | None`, `days: int`, `skipped_days: int`)
  - `time_weighted_return(series: Sequence[tuple[date, Decimal]], flows: Mapping[date, Decimal]) -> TwrResult`
  - `xirr(cashflows: Sequence[tuple[date, Decimal]]) -> Decimal | None`
  - `annualize(total_return: Decimal, days: int) -> Decimal | None`

- [x] **Step 1: Écrire le test qui échoue**

Créer `capitalview-api/tests/services/analytics/test_returns.py` :

```python
from datetime import date, timedelta
from decimal import Decimal

import pytest

from services.analytics.returns import annualize, time_weighted_return, xirr

START = date(2026, 1, 1)


def _days(n: int) -> date:
    return START + timedelta(days=n)


def test_twr_without_flows_is_the_raw_value_change():
    series = [(_days(0), Decimal("1000")), (_days(1), Decimal("1100"))]
    result = time_weighted_return(series, {})
    assert result.total_return == pytest.approx(Decimal("0.10"), abs=1e-9)
    assert result.days == 1
    assert result.skipped_days == 0


def test_twr_neutralises_a_deposit():
    # Value goes 1000 -> 2100 but 1000 of that is a deposit, so the strategy made 10%.
    series = [(_days(0), Decimal("1000")), (_days(1), Decimal("2100"))]
    result = time_weighted_return(series, {_days(1): Decimal("1000")})
    assert result.total_return == pytest.approx(Decimal("0.05"), abs=1e-9)


def test_twr_chains_daily_returns():
    series = [
        (_days(0), Decimal("100")),
        (_days(1), Decimal("110")),
        (_days(2), Decimal("99")),
    ]
    result = time_weighted_return(series, {})
    # 1.10 * 0.90 - 1 = -0.01
    assert result.total_return == pytest.approx(Decimal("-0.01"), abs=1e-9)
    assert result.days == 2


def test_twr_skips_days_with_a_non_positive_base_instead_of_zeroing_them():
    series = [
        (_days(0), Decimal("0")),
        (_days(1), Decimal("500")),
        (_days(2), Decimal("550")),
    ]
    result = time_weighted_return(series, {_days(1): Decimal("500")})
    assert result.skipped_days == 1
    assert result.days == 1
    assert result.total_return == pytest.approx(Decimal("0.10"), abs=1e-9)


def test_xirr_on_a_simple_one_year_flow():
    flows = [(_days(0), Decimal("-1000")), (_days(365), Decimal("1100"))]
    assert xirr(flows) == pytest.approx(Decimal("0.10"), abs=1e-6)


def test_xirr_returns_none_without_a_sign_change():
    assert xirr([(_days(0), Decimal("-1000")), (_days(365), Decimal("-500"))]) is None


def test_mwr_equals_twr_when_there_are_no_intermediate_flows():
    series = [(_days(0), Decimal("1000")), (_days(365), Decimal("1200"))]
    twr = time_weighted_return(series, {})
    mwr = xirr([(_days(0), Decimal("-1000")), (_days(365), Decimal("1200"))])
    assert mwr == pytest.approx(twr.total_return, abs=1e-6)


def test_money_arriving_after_the_rise_drags_mwr_below_twr():
    # 1000 rides a +50% move; a second 1000 lands at the very end and rides nothing.
    series = [
        (_days(0), Decimal("1000")),
        (_days(180), Decimal("1500")),
        (_days(181), Decimal("2500")),
    ]
    twr = time_weighted_return(series, {_days(181): Decimal("1000")})
    mwr = xirr(
        [
            (_days(0), Decimal("-1000")),
            (_days(181), Decimal("-1000")),
            (_days(181), Decimal("2500")),
        ]
    )
    assert twr.total_return == pytest.approx(Decimal("0.50"), abs=1e-9)
    assert mwr < twr.total_return


def test_annualize_scales_a_two_year_return():
    # 21% over 730 days is 10% a year.
    assert annualize(Decimal("0.21"), 730) == pytest.approx(Decimal("0.10"), abs=1e-6)


def test_annualize_refuses_a_degenerate_window():
    assert annualize(Decimal("0.21"), 0) is None
```

- [x] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/services/analytics/test_returns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.analytics.returns'`

- [x] **Step 3: Écrire l'implémentation**

Créer `capitalview-api/services/analytics/returns.py` :

```python
"""Time-weighted and money-weighted returns.

These two answer different questions and their difference is the point: TWR is
what the strategy returned, MWR is what the investor actually earned. The gap
between them is the behavioural signal (Dichev 2007; Morningstar "Mind the Gap").

Decimal is the storage type, but the XIRR solver works in float: Decimal has no
fractional power, and a rate solved to 1e-9 is far past what two years of retail
data can support anyway.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

_ZERO = Decimal("0")
_DAYS_PER_YEAR = Decimal("365")

# Bracket for the bisection. Below -99% a portfolio is gone; above 1000% a year
# the flows are not a return series any more.
_RATE_LOW = -0.99
_RATE_HIGH = 10.0
_BISECTION_STEPS = 200


@dataclass(frozen=True)
class TwrResult:
    total_return: Decimal | None
    days: int
    skipped_days: int


def time_weighted_return(
    series: Sequence[tuple[date, Decimal]],
    flows: Mapping[date, Decimal],
) -> TwrResult:
    """Chain daily returns, neutralising external cash flows.

    Start-of-day convention (daily Modified Dietz): a flow is assumed to land
    before the day's performance, so it belongs in the denominator. Days whose
    base is not positive carry no measurable return and are skipped rather than
    counted as zero — zeroing would silently dilute the chain.
    """
    if len(series) < 2:
        return TwrResult(None, 0, 0)

    ordered = sorted(series, key=lambda point: point[0])
    growth = Decimal("1")
    days = 0
    skipped = 0

    for (_, previous_value), (day, value) in zip(ordered, ordered[1:]):
        flow = Decimal(str(flows.get(day, _ZERO)))
        base = previous_value + flow
        if base <= _ZERO:
            skipped += 1
            continue
        growth *= Decimal("1") + (value - previous_value - flow) / base
        days += 1

    if days == 0:
        return TwrResult(None, 0, skipped)
    return TwrResult(growth - Decimal("1"), days, skipped)


def _npv(rate: float, cashflows: Sequence[tuple[date, Decimal]], origin: date) -> float:
    total = 0.0
    for day, amount in cashflows:
        years = (day - origin).days / 365.0
        total += float(amount) / ((1.0 + rate) ** years)
    return total


def xirr(cashflows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Money-weighted return, solved by bisection.

    Bisection over a bracketed sign change always converges. Newton does not: the
    irregular flow patterns a retail ledger produces routinely send it outside the
    bracket. Returns None when no rate solves the flows.
    """
    if len(cashflows) < 2:
        return None

    origin = min(day for day, _ in cashflows)
    low_npv = _npv(_RATE_LOW, cashflows, origin)
    high_npv = _npv(_RATE_HIGH, cashflows, origin)
    if low_npv * high_npv > 0:
        return None

    low, high = _RATE_LOW, _RATE_HIGH
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2
        if _npv(low, cashflows, origin) * _npv(middle, cashflows, origin) <= 0:
            high = middle
        else:
            low = middle

    return Decimal(str((low + high) / 2))


def annualize(total_return: Decimal, days: int) -> Decimal | None:
    """Geometric annualisation. None for a window too short to mean anything.

    The caller is responsible for gating this: under three years an annualised
    figure is arithmetically valid and statistically weak, and the spec requires
    it be labelled as such rather than hidden.
    """
    if days <= 0 or total_return is None or total_return <= Decimal("-1"):
        return None
    years = Decimal(days) / _DAYS_PER_YEAR
    annual = (1.0 + float(total_return)) ** (1.0 / float(years)) - 1.0
    return Decimal(str(annual))
```

- [x] **Step 4: Lancer les tests**

Run: `uv run pytest tests/services/analytics/test_returns.py -v`
Expected: 10 PASSED

- [x] **Step 5: Commit**

```bash
git add services/analytics/returns.py tests/services/analytics/test_returns.py
git commit -m "feat(analytics): add time-weighted and money-weighted return engines"
```

---

## Task 4: Réglages — benchmark et plan cible

**Files:**
- Create: `capitalview-api/alembic/versions/a1b2c3d4e5f6_add_analytics_settings.py`
- Modify: `capitalview-api/models/user.py:107` (après `usd_eur_rate`)
- Modify: `capitalview-api/dtos/settings.py:41` et `:64` (`UserSettingsUpdate`, `UserSettingsResponse`)
- Modify: `capitalview-api/services/settings.py:72` et `:189`
- Test: `capitalview-api/tests/routes/test_settings.py` (ajout)

**Interfaces:**
- Consumes: rien.
- Produces: `UserSettings.benchmark_asset_key: str | None`, `UserSettings.investment_plan_enc: str | None`, exposés en API sous `benchmark_asset_key` et `investment_plan` (JSON déchiffré, `dict | None`).

- [x] **Step 1: Écrire le test qui échoue**

Ajouter à la fin de `capitalview-api/tests/routes/test_settings.py` :

```python
def test_benchmark_and_investment_plan_round_trip(session, master_key):
    client = TestClient(app)

    response = client.put(
        "/settings",
        json={
            "benchmark_asset_key": "IE00B4L5Y983",
            "investment_plan": {"monthly_target": "500", "allocation": {"IE00B4L5Y983": "100"}},
        },
    )
    assert response.status_code == 200

    body = client.get("/settings").json()
    assert body["benchmark_asset_key"] == "IE00B4L5Y983"
    assert body["investment_plan"]["monthly_target"] == "500"


def test_investment_plan_is_stored_encrypted(session, master_key):
    from sqlmodel import select

    from models.user import UserSettings

    client = TestClient(app)
    client.put("/settings", json={"investment_plan": {"monthly_target": "500"}})

    row = session.exec(select(UserSettings)).first()
    assert row.investment_plan_enc is not None
    # "500" alone could collide with base64 by chance; a 14-char key cannot.
    assert "monthly_target" not in row.investment_plan_enc
```

Reprendre en tête du fichier les imports déjà présents (`TestClient`, `app`) — ils y sont.

- [x] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/routes/test_settings.py -k "benchmark or investment_plan" -v`
Expected: FAIL — le champ est ignoré et absent de la réponse (`KeyError: 'benchmark_asset_key'`).

- [x] **Step 3: Ajouter les colonnes au modèle**

Dans `capitalview-api/models/user.py`, dans `UserSettings`, juste après le bloc `usd_eur_rate` (ligne 107) :

```python
    # Reference index for the investor analytics counterfactuals. Must be an
    # accumulating ETF: a distributing one would need a total-return series we
    # do not store (see the analytics design doc, §7.1).
    benchmark_asset_key: str | None = Field(default=None, sa_column=Column(TEXT, nullable=True))
    # Encrypted JSON: {"monthly_target": "...", "allocation": {asset_key: pct}}
    investment_plan_enc: str | None = Field(default=None, sa_column=Column(TEXT, nullable=True))
```

- [x] **Step 4: Écrire la migration**

Créer `capitalview-api/alembic/versions/a1b2c3d4e5f6_add_analytics_settings.py` :

```python
"""add benchmark_asset_key and investment_plan_enc to user_settings

Revision ID: a1b2c3d4e5f6
Revises: ff6a7b8c9d0e
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "ff6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("benchmark_asset_key", sa.TEXT(), nullable=True))
    op.add_column("user_settings", sa.Column("investment_plan_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_settings", "investment_plan_enc")
    op.drop_column("user_settings", "benchmark_asset_key")
```

- [x] **Step 5: Étendre les DTOs**

Dans `capitalview-api/dtos/settings.py`, ajouter à `UserSettingsUpdate` :

```python
    benchmark_asset_key: str | None = None
    investment_plan: dict | None = None
```

et à `UserSettingsResponse` :

```python
    benchmark_asset_key: str | None = None
    investment_plan: dict | None = None
```

- [x] **Step 6: Câbler le service**

Dans `capitalview-api/services/settings.py`, dans la fonction de mapping vers la réponse (autour de la ligne 72, où `bank_auto_sync_enabled` est passé), ajouter :

```python
        benchmark_asset_key=settings.benchmark_asset_key,
        investment_plan=(
            json.loads(decrypt_data(settings.investment_plan_enc, master_key))
            if settings.investment_plan_enc
            else None
        ),
```

et dans la fonction de mise à jour (autour de la ligne 189, où `bank_auto_sync_enabled` est traité) :

```python
    if data.benchmark_asset_key is not None:
        settings.benchmark_asset_key = data.benchmark_asset_key
    if data.investment_plan is not None:
        settings.investment_plan_enc = encrypt_data(json.dumps(data.investment_plan), master_key)
```

Vérifier que `json`, `encrypt_data` et `decrypt_data` sont importés en tête du fichier ; les ajouter sinon.

- [x] **Step 7: Lancer les tests**

Run: `uv run pytest tests/routes/test_settings.py -v`
Expected: tous PASSED, dont les deux nouveaux.

- [x] **Step 8: Commit**

```bash
git add models/user.py dtos/settings.py services/settings.py alembic/versions/a1b2c3d4e5f6_add_analytics_settings.py tests/routes/test_settings.py
git commit -m "feat(settings): add benchmark and encrypted investment plan fields"
```

---

## Task 5: Série benchmark

**Files:**
- Create: `capitalview-api/services/analytics/benchmark.py`
- Test: `capitalview-api/tests/services/analytics/test_benchmark.py`

**Interfaces:**
- Consumes: `services.market.ensure_price_history`, `models.market.MarketAsset`, `models.market.MarketPriceHistory`.
- Produces:
  - `DEFAULT_BENCHMARK_ASSET_KEY: str = "IE00B4L5Y983"`
  - `resolve_benchmark_key(settings) -> str`
  - `get_benchmark_series(session, asset_key: str, from_date: date, to_date: date) -> dict[date, Decimal]` — série journalière en EUR, **forward-fill** sur les jours non cotés, dict vide si l'actif est introuvable.

- [x] **Step 1: Écrire le test qui échoue**

Créer `capitalview-api/tests/services/analytics/test_benchmark.py` :

```python
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.analytics.benchmark import (
    DEFAULT_BENCHMARK_ASSET_KEY,
    get_benchmark_series,
    resolve_benchmark_key,
)


class _Settings:
    def __init__(self, key):
        self.benchmark_asset_key = key


def test_resolve_falls_back_to_the_default_msci_world():
    assert resolve_benchmark_key(None) == DEFAULT_BENCHMARK_ASSET_KEY
    assert resolve_benchmark_key(_Settings(None)) == DEFAULT_BENCHMARK_ASSET_KEY
    assert resolve_benchmark_key(_Settings("  ")) == DEFAULT_BENCHMARK_ASSET_KEY


def test_resolve_uses_the_configured_key():
    assert resolve_benchmark_key(_Settings("IE00BK1PV551")) == "IE00BK1PV551"


def _seed(session, prices: dict[date, str]) -> None:
    asset = MarketAsset(
        asset_key="IE00B4L5Y983", symbol="IWDA.AS", name="MSCI World", asset_type=AssetType.STOCK
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)
    for day, price in prices.items():
        session.add(
            MarketPriceHistory(market_asset_id=asset.id, price=Decimal(price), price_date=day)
        )
    session.commit()


@patch("services.analytics.benchmark.ensure_price_history")
def test_series_forward_fills_non_trading_days(_ensure, session):
    _seed(session, {date(2026, 1, 2): "100", date(2026, 1, 5): "102"})

    series = get_benchmark_series(session, "IE00B4L5Y983", date(2026, 1, 2), date(2026, 1, 6))

    assert series[date(2026, 1, 2)] == Decimal("100")
    assert series[date(2026, 1, 3)] == Decimal("100")
    assert series[date(2026, 1, 4)] == Decimal("100")
    assert series[date(2026, 1, 5)] == Decimal("102")
    assert series[date(2026, 1, 6)] == Decimal("102")


@patch("services.analytics.benchmark.ensure_price_history")
def test_unknown_asset_yields_an_empty_series(_ensure, session):
    assert get_benchmark_series(session, "NOPE", date(2026, 1, 2), date(2026, 1, 6)) == {}
```

- [x] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/services/analytics/test_benchmark.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.analytics.benchmark'`

- [x] **Step 3: Écrire l'implémentation**

Créer `capitalview-api/services/analytics/benchmark.py` :

```python
"""Benchmark price series for the counterfactual comparisons.

The default is an accumulating MSCI World ETF, and accumulating is a constraint
rather than a taste: it reinvests dividends internally, so its raw quoted price
is already a total-return series. A distributing benchmark would need dividend
data this app deliberately does not store per asset.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market import ensure_price_history

# iShares Core MSCI World UCITS ETF USD (Acc) — IWDA.
DEFAULT_BENCHMARK_ASSET_KEY = "IE00B4L5Y983"


def resolve_benchmark_key(settings) -> str:
    """The user's configured benchmark, or the default MSCI World."""
    key = getattr(settings, "benchmark_asset_key", None) if settings else None
    return key.strip() if key and key.strip() else DEFAULT_BENCHMARK_ASSET_KEY


def get_benchmark_series(
    session: Session,
    asset_key: str,
    from_date: date,
    to_date: date,
) -> dict[date, Decimal]:
    """Daily EUR price per calendar day, forward-filled across closed sessions.

    Forward-filling is what makes the series alignable with the portfolio's daily
    snapshots, which exist every calendar day including weekends.
    """
    ensure_price_history(session, asset_key, AssetType.STOCK, from_date)

    asset = session.exec(select(MarketAsset).where(MarketAsset.asset_key == asset_key)).first()
    if not asset:
        return {}

    rows = session.exec(
        select(MarketPriceHistory)
        .where(
            MarketPriceHistory.market_asset_id == asset.id,
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
        .order_by(MarketPriceHistory.price_date)
    ).all()
    if not rows:
        return {}

    quoted = {row.price_date: Decimal(str(row.price)) for row in rows}

    series: dict[date, Decimal] = {}
    last: Decimal | None = None
    day = from_date
    while day <= to_date:
        if day in quoted:
            last = quoted[day]
        if last is not None:
            series[day] = last
        day += timedelta(days=1)
    return series
```

- [x] **Step 4: Lancer les tests**

Run: `uv run pytest tests/services/analytics/test_benchmark.py -v`
Expected: 4 PASSED

- [x] **Step 5: Commit**

```bash
git add services/analytics/benchmark.py tests/services/analytics/test_benchmark.py
git commit -m "feat(analytics): add benchmark price series resolution"
```

---

## Task 6: Assemblage et endpoint

**Files:**
- Create: `capitalview-api/services/analytics/report.py`
- Create: `capitalview-api/dtos/analytics.py`
- Create: `capitalview-api/routes/analytics.py`
- Modify: `capitalview-api/dtos/__init__.py` (exports)
- Modify: `capitalview-api/routes/__init__.py` (import + `__all__`)
- Modify: `capitalview-api/main.py:20-36` (import) et `:113` (`include_router`)
- Test: `capitalview-api/tests/routes/test_analytics_routes.py`

**Interfaces:**
- Consumes: `Metric`/`Reliability` (T1), `stock_external_flows` (T2), `time_weighted_return`/`xirr`/`annualize` (T3), `resolve_benchmark_key` (T5).
- Produces: `GET /analytics/investor` → `InvestorAnalyticsResponse`.

Le contenu exact de `dtos/analytics.py` est donné au Step 3.

- [x] **Step 1: Écrire le test qui échoue**

Créer `capitalview-api/tests/routes/test_analytics_routes.py` :

```python
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_master_key] = _get_master_key
    yield
    app.dependency_overrides.clear()


def test_investor_analytics_is_empty_without_any_account(session, master_key):
    client = TestClient(app)
    body = client.get("/analytics/investor").json()

    assert body["investor_gap"] is None
    assert body["days"] == 0
    assert body["benchmark_asset_key"] == "IE00B4L5Y983"


@patch("services.analytics.benchmark.ensure_price_history")
def test_investor_analytics_reports_a_gap_for_a_funded_account(_ensure, session, master_key):
    from models.stock import StockAccount
    from services.encryption import encrypt_data, hash_index
    from services.stock_transaction import create_eur_deposit

    session.add(
        StockAccount(
            uuid="acc_1",
            user_uuid_bidx=hash_index("user_1", master_key),
            name_enc=encrypt_data("PEA", master_key),
            account_type_enc=encrypt_data("PEA", master_key),
        )
    )
    session.commit()
    create_eur_deposit(
        session, "acc_1", Decimal("1000"), datetime(2026, 1, 2, 10, tzinfo=timezone.utc), master_key
    )

    body = TestClient(app).get("/analytics/investor").json()

    # No daily snapshots exist in the test DB, so the gap must be withheld rather
    # than invented — that is the reliability gate doing its job.
    assert body["investor_gap"] is None or body["investor_gap"]["twr"]["value"] is None
```

- [x] **Step 2: Lancer le test pour vérifier qu'il échoue**

Run: `uv run pytest tests/routes/test_analytics_routes.py -v`
Expected: FAIL — 404, la route n'existe pas.

- [x] **Step 3: Écrire les DTOs**

Créer `capitalview-api/dtos/analytics.py` :

```python
"""Investor behaviour analytics schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class MetricOut(BaseModel):
    """A single number plus how far it can be trusted.

    `value` is None whenever `reliability` is "insuffisant": an unreliable figure
    never crosses the wire, so it cannot be misread on the client.
    """

    value: Decimal | None = None
    unit: str
    sample_size: int
    reliability: str
    caveat: str | None = None


class InvestorGapResponse(BaseModel):
    twr: MetricOut
    """Cumulative time-weighted return — the strategy's performance."""
    twr_annualised: MetricOut
    benchmark_annualised: MetricOut
    """Benchmark total return over the exact same window."""
    mwr: MetricOut
    """Money-weighted return — what the investor's euros actually earned."""
    gap: MetricOut
    """mwr - twr_annualised. Negative means the timing of the money hurt."""
    gap_eur: MetricOut
    """The gap applied to the average capital at work."""
    average_capital: Decimal
    auto_provision_share: Decimal
    """Share of deposits the app generated itself; bounds how much the gap means."""
    verdict: str


class InvestorAnalyticsResponse(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    days: int
    benchmark_asset_key: str
    investor_gap: InvestorGapResponse | None = None
```

puis les exporter depuis `capitalview-api/dtos/__init__.py` :

```python
from .analytics import (
    InvestorAnalyticsResponse,
    InvestorGapResponse,
    MetricOut,
)
```

et ajouter à `__all__`, à la suite du bloc Community :

```python
    # Analytics
    "MetricOut",
    "InvestorGapResponse",
    "InvestorAnalyticsResponse",
```

- [x] **Step 4: Écrire `report.py`**

Créer `capitalview-api/services/analytics/report.py` :

```python
"""Assembles the investor analytics payload.

One endpoint, one replay: every block shares the same daily flows, the same value
series and the same benchmark. Splitting the API per block would recompute all of
it several times per page load.
"""

from datetime import date
from decimal import Decimal

from sqlmodel import Session

from services.analytics.benchmark import get_benchmark_series, resolve_benchmark_key
from services.analytics.flows import is_auto_provision, stock_external_flows
from services.analytics.reliability import Metric, Reliability
from services.analytics.returns import annualize, time_weighted_return, xirr
from services.settings import get_or_create_settings
from services.stock_account import get_all_stock_accounts_history, get_user_stock_accounts
from services.stock_transaction import get_account_transactions

_ZERO = Decimal("0")

# Under a year, a chained daily series says nothing about behaviour. Three years
# is where an annualised figure stops being mostly noise.
_MIN_DAYS = 180
_SOLID_DAYS = 1095


def build_investor_analytics(session: Session, user_uuid: str, master_key: str) -> dict:
    settings = get_or_create_settings(session, user_uuid, master_key)
    benchmark_key = resolve_benchmark_key(settings)

    accounts = get_user_stock_accounts(session, user_uuid, master_key)
    transactions = []
    for account in accounts:
        transactions.extend(get_account_transactions(session, account.id, master_key))

    history = get_all_stock_accounts_history(session, user_uuid, master_key)
    series = [(snap.snapshot_date, Decimal(snap.total_value)) for snap in history]

    if len(series) < 2:
        return {
            "period_start": series[0][0] if series else None,
            "period_end": series[-1][0] if series else None,
            "days": 0,
            "benchmark_asset_key": benchmark_key,
            "investor_gap": None,
        }

    series.sort(key=lambda point: point[0])
    period_start, period_end = series[0][0], series[-1][0]
    span_days = (period_end - period_start).days

    flows_all = stock_external_flows(transactions)
    flows_real = stock_external_flows(transactions, include_auto_provisions=False)

    twr = time_weighted_return(series, flows_all)
    mwr = _money_weighted(flows_real, series[-1][1], period_end)

    average_capital = sum(value for _, value in series) / Decimal(len(series))
    auto_share = _auto_provision_share(transactions)

    gap = None
    gap_eur = None
    twr_annual = annualize(twr.total_return, span_days) if twr.total_return is not None else None
    if twr_annual is not None and mwr is not None:
        gap = mwr - twr_annual
        gap_eur = gap * average_capital

    benchmark_annual = _benchmark_annual_return(
        session, benchmark_key, period_start, period_end, span_days
    )

    def gated(value, unit):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=span_days,
                minimum=_MIN_DAYS,
                solid_at=_SOLID_DAYS,
                caveat_insufficient=(
                    f"Historique de {span_days} jours : trop court pour conclure."
                ),
                caveat_indicative=(
                    "Moins de trois ans d'historique — le signe est lisible, "
                    "la magnitude annualisée beaucoup moins."
                ),
            )
        )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days": span_days,
        "benchmark_asset_key": benchmark_key,
        "investor_gap": {
            "twr": gated(twr.total_return, "ratio"),
            "twr_annualised": gated(twr_annual, "ratio_annuel"),
            "benchmark_annualised": gated(benchmark_annual, "ratio_annuel"),
            "mwr": gated(mwr, "ratio_annuel"),
            "gap": gated(gap, "ratio_annuel"),
            "gap_eur": gated(gap_eur, "EUR"),
            "average_capital": round(average_capital, 2),
            "auto_provision_share": auto_share,
            "verdict": _verdict(gap, gap_eur, auto_share),
        },
    }


def _benchmark_annual_return(
    session: Session,
    benchmark_key: str,
    period_start: date,
    period_end: date,
    span_days: int,
):
    """Annualised total return of the benchmark over the exact same window.

    The benchmark is an accumulating ETF, so its quoted price already compounds
    dividends: first and last quote are all it takes.
    """
    series = get_benchmark_series(session, benchmark_key, period_start, period_end)
    start_price = series.get(period_start)
    end_price = series.get(period_end)
    if not start_price or not end_price or start_price <= _ZERO:
        return None
    return annualize(end_price / start_price - Decimal("1"), span_days)


def _money_weighted(flows: dict[date, Decimal], terminal_value: Decimal, terminal_day: date):
    """XIRR over real external flows plus the terminal liquidation value.

    Deposits are negative: they leave the investor's pocket. The terminal value is
    the positive counterpart — what getting out today would return.
    """
    cashflows = [(day, -amount) for day, amount in sorted(flows.items())]
    if not cashflows:
        return None
    cashflows.append((terminal_day, terminal_value))
    return xirr(cashflows)


def _auto_provision_share(transactions) -> Decimal:
    deposits = [
        tx
        for tx in transactions
        if str(getattr(tx.type, "value", tx.type)) == "DEPOSIT"
        and str(tx.asset_key or "").upper() == "EUR"
    ]
    if not deposits:
        return _ZERO
    auto = sum(Decimal(str(tx.amount)) for tx in deposits if is_auto_provision(tx))
    total = sum(Decimal(str(tx.amount)) for tx in deposits)
    return round(auto / total, 4) if total > _ZERO else _ZERO


def _as_metric(metric: Metric) -> dict:
    return {
        "value": metric.value,
        "unit": metric.unit,
        "sample_size": metric.sample_size,
        "reliability": metric.reliability.value,
        "caveat": metric.caveat,
    }


def _verdict(gap, gap_eur, auto_share: Decimal) -> str:
    if gap is None or gap_eur is None:
        return (
            "Pas encore assez d'historique pour séparer ta performance de celle de ta stratégie."
        )
    if auto_share > Decimal("0.30"):
        provision_note = (
            f" {int(auto_share * 100)} % de tes dépôts sont des provisions automatiques : "
            "la date réelle d'entrée de ton argent est inconnue, ce chiffre est à lire avec réserve."
        )
    else:
        provision_note = ""
    if gap < _ZERO:
        return (
            f"Ta stratégie fait mieux que toi. L'écart, sur ton capital moyen, représente "
            f"{round(gap_eur)} €. Il ne vient pas de tes choix d'actifs mais du moment où "
            f"tu mets l'argent.{provision_note}"
        )
    return (
        f"Le moment où tu investis t'a rapporté {round(gap_eur)} € par rapport à ta propre "
        f"stratégie. Sur cette durée, c'est autant de la chance que du talent.{provision_note}"
    )
```

- [x] **Step 5: Écrire la route**

Créer `capitalview-api/routes/analytics.py` :

```python
"""Investor behaviour analytics routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from dtos import InvestorAnalyticsResponse
from models import User
from services.analytics.report import build_investor_analytics
from services.auth import get_current_user, get_master_key

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/investor", response_model=InvestorAnalyticsResponse)
def get_investor_analytics(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Behavioural analysis of the user's stock investing over the full history."""
    return build_investor_analytics(session, current_user.uuid, master_key)
```

- [x] **Step 6: Enregistrer la route**

Dans `capitalview-api/routes/__init__.py`, ajouter `from .analytics import router as analytics_router` et `"analytics_router"` dans `__all__`. Dans `capitalview-api/main.py`, ajouter `analytics_router` à l'import depuis `routes` (ligne 20-36) et `app.include_router(analytics_router)` à la suite des autres (ligne 113).

- [x] **Step 7: Lancer les tests**

Run: `uv run pytest tests/routes/test_analytics_routes.py -v`
Expected: 2 PASSED

- [x] **Step 8: Lancer la suite backend complète**

Run: `uv run pytest -q`
Expected: 0 échec.

- [x] **Step 9: Commit**

```bash
git add services/analytics/report.py dtos/analytics.py dtos/__init__.py routes/analytics.py routes/__init__.py main.py tests/routes/test_analytics_routes.py
git commit -m "feat(analytics): expose GET /analytics/investor with the MWR-TWR gap"
```

---

## Task 7: Store et types frontend

**Files:**
- Create: `capitalview-web/src/stores/analysis.ts`
- Modify: `capitalview-web/src/types/index.ts` (ajout en fin de fichier)
- Test: `capitalview-web/src/stores/__tests__/analysis.spec.ts`

**Interfaces:**
- Consumes: `GET /analytics/investor` (T6), `apiClient`, `getOrFetchCached` de `@/services/cache`.
- Produces: types `MetricOut`, `InvestorGapResponse`, `InvestorAnalyticsResponse` ; store `useAnalysisStore` avec `{ data, isLoading, error, fetchAnalytics(force?), reset() }`.

- [x] **Step 1: Créer la branche frontend**

```bash
cd capitalview-web && git checkout -b feat/investor-analytics
```

- [x] **Step 2: Ajouter les types**

À la fin de `capitalview-web/src/types/index.ts` :

```ts
// ── Analytics ────────────────────────────────────────────────
export type Reliability = 'solide' | 'indicatif' | 'insuffisant'

export interface MetricOut {
  value: number | string | null
  unit: string
  sample_size: number
  reliability: Reliability
  caveat: string | null
}

export interface InvestorGapResponse {
  twr: MetricOut
  twr_annualised: MetricOut
  benchmark_annualised: MetricOut
  mwr: MetricOut
  gap: MetricOut
  gap_eur: MetricOut
  average_capital: number | string
  auto_provision_share: number | string
  verdict: string
}

export interface InvestorAnalyticsResponse {
  period_start: string | null
  period_end: string | null
  days: number
  benchmark_asset_key: string
  investor_gap: InvestorGapResponse | null
}
```

- [x] **Step 3: Écrire le test qui échoue**

Créer `capitalview-web/src/stores/__tests__/analysis.spec.ts` :

```ts
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAnalysisStore } from '@/stores/analysis'

vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

vi.mock('@/services/cache', () => ({
  getOrFetchCached: vi.fn((_key: string, fetcher: () => unknown) => fetcher()),
  invalidateCacheKey: vi.fn(),
}))

describe('useAnalysisStore', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('stores the payload returned by the API', async () => {
    const { apiClient } = await import('@/api/client')
    vi.mocked(apiClient.get).mockResolvedValue({
      period_start: '2026-01-01',
      period_end: '2026-07-29',
      days: 210,
      benchmark_asset_key: 'IE00B4L5Y983',
      investor_gap: null,
    })

    const store = useAnalysisStore()
    await store.fetchAnalytics()

    expect(apiClient.get).toHaveBeenCalledWith('/analytics/investor')
    expect(store.data?.days).toBe(210)
    expect(store.error).toBeNull()
  })

  it('surfaces the error message and leaves data untouched', async () => {
    const { apiClient } = await import('@/api/client')
    vi.mocked(apiClient.get).mockRejectedValue(new Error('boom'))

    const store = useAnalysisStore()
    await store.fetchAnalytics()

    expect(store.data).toBeNull()
    expect(store.error).toBe('boom')
    expect(store.isLoading).toBe(false)
  })
})
```

- [x] **Step 4: Lancer le test pour vérifier qu'il échoue**

Run: `pnpm test src/stores/__tests__/analysis.spec.ts`
Expected: FAIL — module `@/stores/analysis` introuvable.

- [x] **Step 5: Écrire le store**

Créer `capitalview-web/src/stores/analysis.ts` :

```ts
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '@/api/client'
import { getOrFetchCached, invalidateCacheKey } from '@/services/cache'
import type { InvestorAnalyticsResponse } from '@/types'

// Behavioural metrics move on the scale of weeks, not seconds.
const CACHE_TTL_MS = 60 * 60 * 1000
const CACHE_KEY = 'analysis:investor'

export const useAnalysisStore = defineStore('analysis', () => {
  const data = ref<InvestorAnalyticsResponse | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  async function fetchAnalytics(force = false): Promise<void> {
    isLoading.value = true
    error.value = null
    try {
      data.value = await getOrFetchCached<InvestorAnalyticsResponse>(
        CACHE_KEY,
        () => apiClient.get<InvestorAnalyticsResponse>('/analytics/investor'),
        CACHE_TTL_MS,
        force,
      )
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Erreur lors du chargement de l'analyse"
    } finally {
      isLoading.value = false
    }
  }

  function reset(): void {
    data.value = null
    error.value = null
    invalidateCacheKey(CACHE_KEY)
  }

  return { data, isLoading, error, fetchAnalytics, reset }
})
```

- [x] **Step 6: Lancer les tests**

Run: `pnpm test src/stores/__tests__/analysis.spec.ts`
Expected: 2 PASSED

- [x] **Step 7: Commit**

```bash
git add src/stores/analysis.ts src/stores/__tests__/analysis.spec.ts src/types/index.ts
git commit -m "feat(analysis): add investor analytics store and types"
```

---

## Task 8: Page `/analyse`

**Files:**
- Create: `capitalview-web/src/components/analytics/ReliabilityBadge.vue`
- Create: `capitalview-web/src/pages/Analysis.vue`
- Modify: `capitalview-web/src/router/index.ts:17` (lazy import) et `:102` (route)
- Modify: `capitalview-web/src/layouts/DefaultLayout.vue:6` (icône), `:39-80` (`BASE_NAV_ITEMS`), `:88-108` (`navItems`)
- Modify: `capitalview-web/src/services/sessionReset.ts` (reset du store)

**Interfaces:**
- Consumes: `useAnalysisStore` (T7), `MetricOut` (T7), `BaseCard`/`BaseAlert`/`BaseSpinner`/`BaseEmptyState` de `@/components`, `useFormatters`, `usePrivacyMode`.
- Produces: route nommée `analysis` sur `/analyse`.

- [x] **Step 1: Écrire `ReliabilityBadge.vue`**

Créer `capitalview-web/src/components/analytics/ReliabilityBadge.vue` :

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { Reliability } from '@/types'

const props = defineProps<{ reliability: Reliability; caveat?: string | null }>()

const label = computed(() => ({
  solide: 'Fiable',
  indicatif: 'Indicatif',
  insuffisant: 'Données insuffisantes',
}[props.reliability]))

const tone = computed(() => ({
  solide: 'bg-success/10 text-success',
  indicatif: 'bg-warning/10 text-warning',
  insuffisant: 'bg-surface-active text-text-muted dark:bg-surface-dark-active dark:text-text-dark-muted',
}[props.reliability]))
</script>

<template>
  <div class="flex flex-col gap-1">
    <span :class="['inline-flex w-fit items-center rounded-full px-2 py-0.5 text-[11px] font-medium', tone]">
      {{ label }}
    </span>
    <p v-if="caveat" class="text-xs text-text-muted dark:text-text-dark-muted">{{ caveat }}</p>
  </div>
</template>
```

Les tokens `bg-success/10 text-success` et `bg-warning/10 text-warning` sont ceux de `BaseBadge.vue:15-16` — déjà en place, rien à ajouter à la config Tailwind.

- [x] **Step 2: Écrire `Analysis.vue`**

Créer `capitalview-web/src/pages/Analysis.vue`. La page affiche : le `PageHeader`, un état de chargement, le verdict en tête, puis les quatre métriques (TWR annualisé, MWR, écart, écart en €) — **chacune ne rendant sa valeur que si `value !== null`**, sinon le `ReliabilityBadge` seul avec le caveat.

```vue
<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useAnalysisStore } from '@/stores/analysis'
import { useFormatters } from '@/composables/useFormatters'
import { usePrivacyMode } from '@/composables/usePrivacyMode'
import PageHeader from '@/components/PageHeader.vue'
import { BaseCard, BaseAlert, BaseSpinner, BaseEmptyState } from '@/components'
import ReliabilityBadge from '@/components/analytics/ReliabilityBadge.vue'
import type { MetricOut } from '@/types'

const analysis = useAnalysisStore()
const { formatCurrency, formatPercent, profitLossClass } = useFormatters()
const { maskValue } = usePrivacyMode()

const gap = computed(() => analysis.data?.investor_gap ?? null)

const cards = computed(() => {
  const g = gap.value
  if (!g) return []
  return [
    { key: 'twr', label: 'Performance de ta stratégie', metric: g.twr_annualised, kind: 'pct' as const },
    { key: 'benchmark', label: 'MSCI World sur la même période', metric: g.benchmark_annualised, kind: 'pct' as const },
    { key: 'mwr', label: 'Performance réelle de tes euros', metric: g.mwr, kind: 'pct' as const },
    { key: 'gap', label: 'Écart investisseur', metric: g.gap, kind: 'pct' as const, signed: true },
    { key: 'gap_eur', label: 'Ce que cet écart représente', metric: g.gap_eur, kind: 'eur' as const, signed: true },
  ]
})

function display(metric: MetricOut, kind: 'pct' | 'eur'): string {
  if (metric.value === null) return '—'
  const n = Number(metric.value)
  return kind === 'pct' ? formatPercent(n * 100) : maskValue(formatCurrency(n))
}

onMounted(() => analysis.fetchAnalytics())
</script>

<template>
  <div>
    <PageHeader
      title="Analyse"
      description="Ce que tes données disent de ton comportement d'investisseur"
    />

    <div v-if="analysis.isLoading && !analysis.data" class="flex justify-center py-20">
      <BaseSpinner size="lg" label="Analyse en cours..." />
    </div>

    <BaseAlert v-else-if="analysis.error" variant="danger" class="mb-6">
      {{ analysis.error }}
    </BaseAlert>

    <BaseEmptyState
      v-else-if="!gap"
      title="Pas encore assez d'historique"
      description="L'analyse comportementale demande plusieurs mois de transactions pour dire quoi que ce soit d'utile."
    />

    <template v-else>
      <BaseCard class="mb-6">
        <p class="text-sm leading-relaxed text-text-main dark:text-text-dark-main">{{ gap.verdict }}</p>
      </BaseCard>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <BaseCard v-for="card in cards" :key="card.key">
          <p class="mb-1 text-[11px] font-medium uppercase tracking-wider text-text-muted dark:text-text-dark-muted">
            {{ card.label }}
          </p>
          <p
            :class="[
              'mb-2 text-2xl font-bold tabular-nums',
              card.signed && card.metric.value !== null
                ? profitLossClass(Number(card.metric.value))
                : 'text-text-main dark:text-text-dark-main',
            ]"
          >
            {{ display(card.metric, card.kind) }}
          </p>
          <ReliabilityBadge :reliability="card.metric.reliability" :caveat="card.metric.caveat" />
        </BaseCard>
      </div>
    </template>
  </div>
</template>
```

- [x] **Step 3: Enregistrer la route**

Dans `capitalview-web/src/router/index.ts`, ajouter le lazy import à la suite des autres (ligne 17) :

```ts
const Analysis = () => import('@/pages/Analysis.vue')
```

et la route avant `/settings` :

```ts
  {
    path: '/analyse',
    name: 'analysis',
    component: Analysis,
    meta: { requiresAuth: true },
  },
```

- [x] **Step 4: Ajouter l'entrée de navigation**

Dans `capitalview-web/src/layouts/DefaultLayout.vue` : ajouter `Microscope` à l'import `lucide-vue-next` (ligne 6), ajouter à `BASE_NAV_ITEMS` juste après l'entrée Bourse :

```ts
  {
    label: 'Analyse',
    to: '/analyse',
    icon: Microscope,
  },
```

et dans `navItems` (ligne 97), juste après `items.push(byPath('/stock'))` :

```ts
  items.push(byPath('/analyse'))
```

- [x] **Step 5: Câbler le reset de session**

Dans `capitalview-web/src/services/sessionReset.ts`, trois ajouts dans `resetAllSessionState`. Dans le tableau déstructuré (ligne 15-26), après `{ useImportsStore }` :

```ts
    { useAnalysisStore },
```

dans le `Promise.all` (ligne 27-39), après `import('@/stores/imports')` :

```ts
    import('@/stores/analysis'),
```

et à la suite des appels (ligne 51), après `useImportsStore().reset()` :

```ts
  useAnalysisStore().reset()
```

L'ordre des deux listes doit rester aligné : la déstructuration positionnelle casse silencieusement sinon.

- [x] **Step 6: Vérifier types et tests**

Run: `pnpm type-check && pnpm test`
Expected: 0 erreur de type, tous les tests PASSED.

- [x] **Step 7: Vérifier dans le navigateur**

Lancer `pnpm dev`, se connecter, ouvrir `/analyse`. Vérifier : la page se charge, l'état vide s'affiche proprement si l'historique est court, et une métrique `insuffisant` affiche bien `—` et son caveat, **jamais un nombre**.

- [x] **Step 8: Commit**

```bash
git add src/pages/Analysis.vue src/components/analytics/ReliabilityBadge.vue src/router/index.ts src/layouts/DefaultLayout.vue src/services/sessionReset.ts
git commit -m "feat(analysis): add the investor analysis page with reliability gating"
```

---

## Vérification finale de M1

Toutes les tâches sont exécutées et poussées sur `feat/investor-analytics` dans les deux dépôts.

- [x] `cd capitalview-api && uv run pytest -q` — **616 passed, 0 failed** (609 avant M1)
- [x] `cd capitalview-web && pnpm type-check && pnpm test` — 0 erreur de type, 25 tests passed
- [x] Migration appliquée — chaîne complète rejouée sur une base PostgreSQL vierge : 33 migrations,
      une seule tête (`9c4f1ab73e20`), colonnes `benchmark_asset_key` et `investment_plan_enc`
      créées. `downgrade -1` puis `upgrade head` vérifiés aller-retour.
- [x] Les courbes de `/stock` sont inchangées par le refactor R1 — **vérifié par équivalence de code
      plutôt que visuellement** : `day_txs` est déjà filtré par `tx.executed_at.date() == d`, et
      `stock_external_flow_for_day` réapplique exactement le même prédicat. Le filtre ajouté est donc
      un no-op strict, et `include_auto_provisions` reste à `True` côté snapshots.
- [x] `/analyse` affiche un verdict cohérent, et aucune métrique `insuffisant` ne montre de valeur —
      parcours navigateur réel (login → nav → page) contre une API et une base live : entrée de nav
      présente, `GET /analytics/investor` → 200, état vide correct. Invariant de la gate vérifié sur
      un payload mixte : les deux métriques `insuffisant` rendent `—` avec leur caveat, sans qu'aucun
      chiffre ne fuite ; les trois autres rendent leur valeur formatée. Zéro erreur de page.

## Ce que M1 ne fait pas

Le pont contrefactuel (§1.2), le coût d'exécution (§1.3), la régularité (§2.1), le décalage dépôt→achat (§2.4), le conditionnement au marché (§2.2), les paris indépendants (§2.3), les frais (§3.1), l'effet de disposition (§3.2) et le bloc plan (§4) arrivent en M2 et M3. Le formulaire de saisie du plan cible n'est pas construit en M1 — seul le champ de stockage existe.
