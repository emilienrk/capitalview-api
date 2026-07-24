# Realized P/L (crypto + stock) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose distinct P/L latent / réalisé / total per account (crypto + stock), make the "P/L cumulé" history curve continuous through sells, and let the web P/L card toggle between the three views.

**Architecture:** Accumulate realized P/L statelessly inside the existing transaction fold of each summary service (no stored state). Add two additive DTO fields (`realized_profit_loss`, `total_profit_loss`); existing `profit_loss` keeps its latent/cost-basis meaning. Redefine snapshot `cumulative_pnl` to total P/L at the 3 build sites, and bump the calc version so stored history rebuilds once on next login. Web: one clickable P/L stat cycling latent → réalisé → total.

**Tech Stack:** Python (FastAPI, SQLModel, pytest), Vue 3 + TypeScript (Vite).

## Global Constraints

- **No new stored state / no migration.** Realized is recomputed from transactions each call. History realignment happens only via the `CURRENT_CALC_VERSION` bump (`account_history.py:55`).
- **Additive DTO only.** `profit_loss` / `profit_loss_percentage` keep their meaning (latent, cost basis). Never rename/remove them.
- **Scope already done, do NOT redo:** stock cost-basis switch (`stock_transaction.py:657` already `current_value_acc - total_invested_acc`); cash-vs-holdings split. Stock is already `calc_version=1`.
- **Comments in English**, sparse, only where the *why* is non-obvious. Match surrounding style.
- Money as `Decimal`; round to 2 at the DTO boundary like existing code.

---

### Task 1: Crypto — accumulate realized P/L in the fold

**Files:**
- Modify: `services/crypto_transaction.py` (`get_crypto_account_summary`, ~710-906)
- Test: `tests/services/test_crypto_transaction.py`

**Interfaces:**
- Produces: `AccountSummaryResponse.realized_profit_loss` / `.total_profit_loss` populated for crypto. `realized = Σ_groups (proceeds − cost_removed)`; `proceeds` = fiat DEPOSIT value of the sale group, or `anchor_by_group[group]` for a swap; `cost_removed` = cost basis removed by the crypto SPEND(s) of that group.
- Consumes: DTO fields from Task 4 (add Task 4's DTO fields first if executing strictly in order, or accept that this task's test needs them — **do Task 4 before this**).

- [ ] **Step 1: Write the failing test** (reuses the existing sell-to-fiat + rebuy fixture pattern)

```python
@patch("services.crypto_transaction.get_crypto_info")
def test_crypto_realized_pnl_sell_to_fiat_then_hold(mock_info, session: Session, master_key: str):
    """Sell BTC to fiat at a gain, keep cash: latent 0, realized = gain, total = gain."""
    mock_info.side_effect = lambda s, symbol, db_only=False, as_of=None: {
        "BTC": ("Bitcoin", Decimal("40000.0")),
    }.get(symbol, ("Unknown", Decimal("0")))
    _make_crypto_account(session, "acc_realized", "user_realized", master_key)
    # BUY 1 BTC for 30000 (external deposit then spend).
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_realized", asset_key="EUR", type=CryptoTransactionType.DEPOSIT,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 1)), master_key)
    g_buy = "grp-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_realized", asset_key="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)), master_key, group_uuid=g_buy)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_realized", asset_key="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 2)), master_key, group_uuid=g_buy)
    # SELL 1 BTC for 50000: SPEND BTC + DEPOSIT EUR proceeds (same group).
    g_sell = "grp-sell"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_realized", asset_key="BTC", type=CryptoTransactionType.SPEND,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 3)), master_key, group_uuid=g_sell)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_realized", asset_key="EUR", type=CryptoTransactionType.DEPOSIT,
        amount=Decimal("50000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 3)), master_key, group_uuid=g_sell)

    summary = _crypto_summary(session, "acc_realized", master_key)
    assert summary.profit_loss == Decimal("0")          # no crypto held → latent 0
    assert summary.realized_profit_loss == Decimal("20000")  # 50000 proceeds − 30000 cost
    assert summary.total_profit_loss == Decimal("20000")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd capitalview-api && uv run pytest tests/services/test_crypto_transaction.py::test_crypto_realized_pnl_sell_to_fiat_then_hold -v`
Expected: FAIL — `realized_profit_loss` is None / attribute default, not 20000.

- [ ] **Step 3: Implement realized accumulation**

In the pre-pass loop (currently ~727-736), inside `if tx.group_uuid:`, add fiat-deposit-by-group collection:

```python
elif tx.type == "DEPOSIT" and tx.asset_key in FIAT_ASSET_KEYS:
    fiat_deposit_by_group.setdefault(tx.group_uuid, Decimal("0"))
    fiat_deposit_by_group[tx.group_uuid] += tx.amount * tx.price_per_unit
```

Declare `fiat_deposit_by_group: dict[str, Decimal] = {}` and `cost_removed_by_group: dict[str, Decimal] = {}` next to the other maps (~724-726).

In the fold's `case "SPEND" | "TRANSFER":` branch, capture removed cost and attribute it to the group **only for SPEND** (TRANSFER is not a disposal):

```python
case "SPEND" | "TRANSFER":
    if pos["total_amount"] > 0:
        fraction = tx.amount / pos["total_amount"]
        if fraction > Decimal("1"):
            fraction = Decimal("1")
        cost_removed = pos["cost_basis"] * fraction
        pos["cost_basis"] -= cost_removed
        if pos["cost_basis"] < 0:
            pos["cost_basis"] = Decimal("0")
        if tx.type == "SPEND" and tx.group_uuid and tx.asset_key not in FIAT_ASSET_KEYS:
            cost_removed_by_group.setdefault(tx.group_uuid, Decimal("0"))
            cost_removed_by_group[tx.group_uuid] += cost_removed
    pos["total_amount"] -= tx.amount
```

After positions are built (just before assembling the return, ~893), compute realized:

```python
# Realized P/L: for each disposal group, proceeds − cost removed. Proceeds is the
# fiat received (sell-to-fiat) or the EUR anchor (crypto→crypto swap). Transfers and
# outbound WITHDRAW carry no proceeds → not counted here.
realized_acc = Decimal("0")
for group in groups_with_crypto_spend:
    if group in fiat_deposit_by_group:
        proceeds = fiat_deposit_by_group[group]
    elif group in anchor_by_group:
        proceeds = anchor_by_group[group]
    else:
        proceeds = Decimal("0")
    realized_acc += proceeds - cost_removed_by_group.get(group, Decimal("0"))

if profit_loss_acc is not None:
    total_profit_loss_acc = profit_loss_acc + realized_acc
else:
    total_profit_loss_acc = realized_acc
```

Add to the `AccountSummaryResponse(...)` return:

```python
        realized_profit_loss=round(realized_acc, 2),
        total_profit_loss=round(total_profit_loss_acc, 2),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd capitalview-api && uv run pytest tests/services/test_crypto_transaction.py::test_crypto_realized_pnl_sell_to_fiat_then_hold -v`
Expected: PASS.

- [ ] **Step 5: Add swap + withdraw regression tests**

```python
@patch("services.crypto_transaction.get_crypto_info")
def test_crypto_realized_pnl_swap(mock_info, session: Session, master_key: str):
    """Crypto→crypto swap: realized = anchor (market value) − cost removed."""
    mock_info.side_effect = lambda s, symbol, db_only=False, as_of=None: {
        "BTC": ("Bitcoin", Decimal("0")), "ETH": ("Ether", Decimal("2000")),
    }.get(symbol, ("Unknown", Decimal("0")))
    _make_crypto_account(session, "acc_swap", "user_swap", master_key)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="EUR", type=CryptoTransactionType.DEPOSIT,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 1)), master_key)
    g_buy = "swap-buy"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="BTC", type=CryptoTransactionType.BUY,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)), master_key, group_uuid=g_buy)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="EUR", type=CryptoTransactionType.SPEND,
        amount=Decimal("30000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 2)), master_key, group_uuid=g_buy)
    # Swap 1 BTC → 20 ETH, EUR anchor 40000.
    g_swap = "swap-grp"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="BTC", type=CryptoTransactionType.SPEND,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 3)), master_key, group_uuid=g_swap)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="EUR", type=CryptoTransactionType.ANCHOR,
        amount=Decimal("40000"), price_per_unit=Decimal("1"), executed_at=datetime(2023, 1, 3)), master_key, group_uuid=g_swap)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_swap", asset_key="ETH", type=CryptoTransactionType.BUY,
        amount=Decimal("20"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 3)), master_key, group_uuid=g_swap)

    summary = _crypto_summary(session, "acc_swap", master_key)
    # Realized on the BTC leg: anchor 40000 − cost removed 30000 = 10000.
    assert summary.realized_profit_loss == Decimal("10000")


@patch("services.crypto_transaction.get_crypto_info")
def test_crypto_outbound_withdraw_no_realized(mock_info, session: Session, master_key: str):
    """Outbound crypto WITHDRAW is a transfer, not a sale → realized stays 0."""
    mock_info.return_value = ("Bitcoin", Decimal("40000"))
    _make_crypto_account(session, "acc_wd", "user_wd", master_key)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_wd", asset_key="BTC", type=CryptoTransactionType.DEPOSIT,
        amount=Decimal("2"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 1)), master_key)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_wd", asset_key="BTC", type=CryptoTransactionType.WITHDRAW,
        amount=Decimal("1"), price_per_unit=Decimal("0"), executed_at=datetime(2023, 1, 2)), master_key)
    summary = _crypto_summary(session, "acc_wd", master_key)
    assert summary.realized_profit_loss == Decimal("0")
```

Run: `cd capitalview-api && uv run pytest tests/services/test_crypto_transaction.py -k realized -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add capitalview-api/services/crypto_transaction.py capitalview-api/tests/services/test_crypto_transaction.py
git commit -m "feat(crypto): accumulate realized P/L in account summary fold"
```

---

### Task 4 (DTO) — do BEFORE Task 1/2: add realized + total fields

**Files:**
- Modify: `dtos/transaction.py` (`AccountSummaryResponse`, ~52-64)

**Interfaces:**
- Produces: `AccountSummaryResponse.realized_profit_loss: Decimal | None`, `.total_profit_loss: Decimal | None` (default None). Consumed by Tasks 1, 2, 3 and the web types.

- [ ] **Step 1: Add the fields**

```python
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None
    realized_profit_loss: Decimal | None = None
    total_profit_loss: Decimal | None = None
    positions: list[PositionResponse]
```

- [ ] **Step 2: Verify import / model builds**

Run: `cd capitalview-api && uv run python -c "from dtos.transaction import AccountSummaryResponse; print(AccountSummaryResponse.model_fields.keys())"`
Expected: keys include `realized_profit_loss`, `total_profit_loss`.

- [ ] **Step 3: Commit**

```bash
git add capitalview-api/dtos/transaction.py
git commit -m "feat(dto): add realized_profit_loss and total_profit_loss to AccountSummaryResponse"
```

---

### Task 2: Stock — accumulate realized P/L (+ keep dividends distinct)

**Files:**
- Modify: `services/stock_transaction.py` (`get_stock_account_summary`, ~484-676)
- Test: `tests/services/test_stock_transaction.py`

**Interfaces:**
- Produces: crypto-identical `realized_profit_loss` / `total_profit_loss` for stock, where `total = latent + realized + total_dividends`. Dividends counted once via `total_dividends`, never folded into realized.

- [ ] **Step 1: Write the failing test**

```python
def test_stock_realized_pnl_sell_at_gain(session: Session, master_key: str):
    """SELL at a gain: realized captured; latent = open positions vs cost basis."""
    # (mirror existing stock summary tests' fixture helpers in this file)
    ...
    summary = get_stock_account_summary(session, txs)
    assert summary.realized_profit_loss == Decimal("<proceeds − cost_removed>")
    assert summary.total_profit_loss == (
        (summary.profit_loss or Decimal("0")) + summary.realized_profit_loss + summary.total_dividends
    )
```
(Fill the fixture with the same helpers the neighbouring tests in `test_stock_transaction.py` use — a BUY then partial SELL at a higher price; compute expected realized = `proceeds − total_cost*fraction`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd capitalview-api && uv run pytest tests/services/test_stock_transaction.py::test_stock_realized_pnl_sell_at_gain -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Declare `realized_acc = Decimal("0")` before the fold loop (~517). In the `SELL` branch (561-567), compute removed cost before reducing and accumulate:

```python
        elif tx.type == "SELL" and pos["total_amount"] > 0:
            fraction = min(tx.amount / pos["total_amount"], Decimal("1"))
            proceeds = (tx.amount * tx.price_per_unit) - tx.fees
            cost_removed = pos["total_cost"] * fraction
            realized_acc += proceeds - cost_removed
            pos["total_amount"] = max(pos["total_amount"] - tx.amount, Decimal("0"))
            pos["total_cost"] = max(pos["total_cost"] * (Decimal("1") - fraction), Decimal("0"))
            pos["total_fees"] += tx.fees
            positions_map["EUR"]["total_amount"] += proceeds
```

After `total_dividends_acc` is computed (~661-663), compute total:

```python
if profit_loss_acc is not None:
    total_profit_loss_acc = profit_loss_acc + realized_acc + total_dividends_acc
else:
    total_profit_loss_acc = realized_acc + total_dividends_acc
```

Add to the return:

```python
        realized_profit_loss=round(realized_acc, 2),
        total_profit_loss=round(total_profit_loss_acc, 2),
```

- [ ] **Step 4: Run tests**

Run: `cd capitalview-api && uv run pytest tests/services/test_stock_transaction.py -k "realized or dividend" -v`
Expected: PASS (add a DIVIDEND test asserting dividends land in `total` once, not in `realized`).

- [ ] **Step 5: Commit**

```bash
git add capitalview-api/services/stock_transaction.py capitalview-api/tests/services/test_stock_transaction.py
git commit -m "feat(stock): accumulate realized P/L; total = latent + realized + dividends"
```

---

### Task 3: History curve = total P/L + calc_version bump

**Files:**
- Modify: `services/account_history.py` (`_build_positions_from_summary` ~489; `CURRENT_CALC_VERSION` ~55)
- Modify: `services/crypto_account.py` (~259-262 live snapshot)
- Modify: `services/stock_account.py` (~234-236 live snapshot)
- Test: `tests/services/test_history_services.py`

**Interfaces:**
- Consumes: `summary.total_profit_loss` from Tasks 1/2.
- Produces: `cumulative_pnl` = total P/L at all 3 build sites; crypto 1→2, stock 1→2.

- [ ] **Step 1: Write the failing test** — snapshot `cumulative_pnl` equals `total_profit_loss`, not `profit_loss`.

```python
def test_snapshot_cumulative_pnl_is_total_pnl():
    class _Summary:
        positions = []
        current_value = Decimal("100"); cash_balance = Decimal("0")
        total_invested = Decimal("80"); total_deposits = Decimal("80")
        total_withdrawals = Decimal("0"); total_fees = Decimal("0")
        total_dividends = Decimal("0")
        profit_loss = Decimal("20"); total_profit_loss = Decimal("35")  # 15 realized
    payload = _build_positions_from_summary(_Summary())
    assert payload["cumulative_pnl"] == Decimal("35")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd capitalview-api && uv run pytest tests/services/test_history_services.py::test_snapshot_cumulative_pnl_is_total_pnl -v`
Expected: FAIL (returns 20).

- [ ] **Step 3: Implement**

`account_history.py:489` — read total P/L, fall back to latent then to deposits math:

```python
    cumulative_pnl_raw = getattr(summary, "total_profit_loss", None)
    if cumulative_pnl_raw is None:
        cumulative_pnl_raw = getattr(summary, "profit_loss", None)
    if cumulative_pnl_raw is not None:
        cumulative_pnl = _to_decimal(cumulative_pnl_raw)
    else:
        cumulative_pnl = total_value - current_deposits + current_withdrawals
```

`crypto_account.py:259-262` and `stock_account.py:234-236` — swap `summary.profit_loss` for `summary.total_profit_loss` in both `cumulative_pnl=round(...)` blocks (keep the same None fallback).

`account_history.py:55` — bump:

```python
    AccountCategory.CRYPTO: 2,   # bumped: cumulative_pnl now total P/L (latent + realized)
    AccountCategory.STOCK: 2,    # bumped: cumulative_pnl now total P/L (latent + realized + dividends)
```

- [ ] **Step 4: Run tests**

Run: `cd capitalview-api && uv run pytest tests/services/test_history_services.py tests/services/test_crypto_account.py tests/services/test_stock_account.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add capitalview-api/services/account_history.py capitalview-api/services/crypto_account.py capitalview-api/services/stock_account.py capitalview-api/tests/services/test_history_services.py
git commit -m "feat(history): cumulative_pnl = total P/L; bump crypto/stock calc_version"
```

---

### Task 5: Web — types + clickable P/L card (latent → réalisé → total)

**Files:**
- Modify: `capitalview-web/src/types/index.ts` (`AccountSummaryResponse` ~724-738)
- Modify: `capitalview-web/src/composables/useStatsPager.ts` (`SummaryStatItem`)
- Modify: `capitalview-web/src/pages/Crypto.vue` (stat computed ~1185, template ~1709)
- Modify: `capitalview-web/src/pages/Stock.vue` (equivalent stat computed + template)

**Interfaces:**
- Consumes: DTO fields `realized_profit_loss`, `total_profit_loss`.
- Produces: `SummaryStatItem.onSelect?: () => void` (card is a button when set) + `hint?: string`.

- [ ] **Step 1: Extend TS type**

`types/index.ts`, in `AccountSummaryResponse`:

```ts
  profit_loss: number | null
  profit_loss_percentage: number | null
  realized_profit_loss: number | null
  total_profit_loss: number | null
```

- [ ] **Step 2: Extend `SummaryStatItem`**

`useStatsPager.ts`:

```ts
export interface SummaryStatItem {
  key: string
  label: string
  value: string
  valueClass?: string
  hint?: string
  onSelect?: () => void
}
```

- [ ] **Step 3: Crypto.vue — cycling P/L stat**

Add a view ref near the other refs:

```ts
const PNL_VIEWS = ['latent', 'realized', 'total'] as const
const pnlView = ref<(typeof PNL_VIEWS)[number]>('latent')
function cyclePnlView() {
  const i = PNL_VIEWS.indexOf(pnlView.value)
  pnlView.value = PNL_VIEWS[(i + 1) % PNL_VIEWS.length]
}
```

Replace the `profit_loss` stat object (~1200-1205) with a computed view:

```ts
    (() => {
      const map = {
        latent: { label: 'P/L latent', value: summary.profit_loss },
        realized: { label: 'P/L réalisé', value: summary.realized_profit_loss },
        total: { label: 'P/L total', value: summary.total_profit_loss },
      } as const
      const v = map[pnlView.value]
      return {
        key: 'profit_loss',
        label: v.label,
        value: maskAmount(v.value),
        valueClass: profitLossClass(v.value),
        hint: 'Appuyez pour changer',
        onSelect: cyclePnlView,
      }
    })(),
```

- [ ] **Step 4: Crypto.vue — make the card clickable**

In the stat card `v-for` (~1709-1718), render a button when `onSelect` is present:

```html
            <component
              :is="stat.onSelect ? 'button' : 'div'"
              v-for="stat in activeCryptoSummaryStats"
              :key="stat.key"
              :type="stat.onSelect ? 'button' : undefined"
              class="rounded-secondary bg-surface dark:bg-surface-dark border border-surface-border dark:border-surface-dark-border p-4 text-left w-full"
              :class="stat.onSelect ? 'cursor-pointer hover:border-primary/60 transition-colors' : ''"
              @click="stat.onSelect?.()"
            >
              <p class="text-[11px] font-medium uppercase tracking-wider text-text-muted dark:text-text-dark-muted mb-1.5">{{ stat.label }}</p>
              <p :class="['text-xl font-bold tabular-nums', stat.valueClass ?? 'text-text-main dark:text-text-dark-main']">
                {{ stat.value }}
              </p>
              <p v-if="stat.hint" class="mt-1 text-[10px] text-text-muted dark:text-text-dark-muted">{{ stat.hint }}</p>
            </component>
```

- [ ] **Step 5: Stock.vue — same cycling stat + clickable card**

Apply Steps 3–4 to `Stock.vue`'s stat computed and template. Keep the existing **Dividendes** stat as its own separate card (unchanged).

- [ ] **Step 6: Typecheck + build**

Run: `cd capitalview-web && pnpm type-check && pnpm build`
Expected: no type errors; build succeeds. (Node via nix — see the build-node-path memory if `node` is not on PATH.)

- [ ] **Step 7: Commit**

```bash
git add capitalview-web/src/types/index.ts capitalview-web/src/composables/useStatsPager.ts capitalview-web/src/pages/Crypto.vue capitalview-web/src/pages/Stock.vue
git commit -m "feat(web): clickable P/L card cycling latent/réalisé/total (crypto + stock)"
```

---

## Self-Review

- **Spec coverage:** realized in fold (Tasks 1–2), DTO fields (Task 4), history=total + calc_version bump (Task 3), web display (Task 5). Stock cost-basis switch = already shipped (out of scope, noted). Non-goals (tax lots, transfer realized=0) respected: only SPEND-with-proceeds and SELL accumulate.
- **Edge cases:** latent None → total = realized (+ dividends) (Tasks 1/2 `if profit_loss_acc is not None` branch). Swap without anchor → proceeds 0. Outbound WITHDRAW → 0 (Task 1 Step 5 test). Dividends counted once (Task 2).
- **Type consistency:** `realized_profit_loss` / `total_profit_loss` identical names across DTO, TS type, all consumers. `onSelect` / `hint` names match between `SummaryStatItem` and both pages.
