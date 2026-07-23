# Crypto & Stock: realized P/L (+ dividends)

**Date:** 2026-07-23
**Status:** Approved design, ready for implementation plan
**Scope:** `capitalview-api` (calc, crypto + stock) + `capitalview-web` (display)
**Depends on:** `2026-07-22-account-history-calc-version-rebuild-design` (history recompute)

## Problem

Both account summaries only know *unrealized* P/L on currently-open positions
(`current_value − total_invested`, cost basis / PRU). On a disposal the fold reduces
`cost_basis`/`total_cost` proportionally but **discards** the gain/loss actually realized
(`proceeds − cost_removed`). Nothing accumulates it.

- **Crypto** (`get_crypto_account_summary`): realized on sell-to-fiat / crypto→crypto swap
  is discarded.
- **Stock** (`get_stock_account_summary`): realized on `SELL` (line ~561-567) is discarded;
  additionally stock has **dividends** — a distinct income stream, tracked as
  `total_dividends` and paid into EUR cash, that is not part of latent P/L.

Consequences: selling a winner makes its gain vanish from the displayed P/L; an account
fully exited to cash shows P/L = 0 after a real gain/loss; the "P/L cumulé" curve collapses
toward 0 as positions are sold.

### Interaction with the cost-basis switch

Crypto already moved its headline P/L to cost basis (PRU). Stock has **not** — it still
computes account P/L on net external deposits (`stock_transaction.py:646-652`), which
*implicitly* captures realized + dividends because they flow into EUR cash. Therefore the
stock cost-basis switch (the equivalent of the crypto fix) must land **together with** this
realized/dividends tracking, otherwise stock users would see realized gains and dividends
disappear from the headline. This spec bundles them for stock.

## Goal

Expose, per account, distinct and well-defined figures:

- **P/L latent** = `current_value − total_invested` (unrealized, open positions).
- **P/L réalisé** = Σ over disposals of `proceeds − cost_removed`.
- **Dividendes** (stock only) = Σ dividend income (already computed as `total_dividends`).
- **P/L total** = latent + réalisé (+ dividendes for stock).

## Non-goals

- Tax-lot accounting (FIFO/LIFO/specific-id) or tax reporting. Keep average-cost (PRU),
  removed proportionally, as today.
- Realized P/L on outbound **transfers** (crypto WITHDRAW to an external wallet): a transfer
  is not a sale → 0 realized.
- Changing the headline P/L card semantics (stays *latent*, see Display).

## Design

### 1. Accumulate realized P/L in the existing fold (stateless, both services)

No new stored state; recompute from transactions on each call.

**Crypto** — at every disposal that yields proceeds, accumulate
`realized += proceeds − cost_removed`, with:
- Sell-to-fiat: `proceeds` = the group's fiat DEPOSIT value. Add a `fiat_proceeds_by_group`
  map (DEPOSIT fiat rows whose group ∈ `groups_with_crypto_spend`), mirroring the existing
  `fiat_spend_by_group`.
- Crypto→crypto swap: `proceeds` = the trade's EUR anchor (`anchor_by_group[group]`).
- `cost_removed` = `cost_basis * fraction` (already computed when reducing cost basis).

**Stock** — on each `SELL` (line ~561-567):
- `proceeds` = `amount * price_per_unit − fees` (already computed there).
- `cost_removed` = `total_cost * fraction` (the amount removed from cost basis on that line).
- `realized += proceeds − cost_removed`.

### 2. Stock cost-basis P/L switch (bundled)

Change `stock_transaction.py` account P/L to the same basis as crypto:
`profit_loss_acc = current_value_acc − total_invested_acc` (equivalently Σ stock positions'
P/L), `profit_loss_percentage = profit_loss_acc / total_invested_acc × 100`. Stop using
`net_external_deposits` for account P/L. (`total_deposits`/`total_withdrawals` stay reported
as their own tiles.)

### 3. Summary DTO

`AccountSummaryResponse` gains:
- `realized_profit_loss: Decimal | None`
- `total_profit_loss: Decimal | None` = latent `profit_loss` + `realized_profit_loss`
  (+ `total_dividends` for stock).

`profit_loss` / `profit_loss_percentage` keep their meaning (latent, cost basis).
`total_dividends` already exists for stock.

### 4. History curve = total P/L (both types)

`cumulative_pnl` (snapshot value feeding "P/L cumulé") is redefined as **total P/L**
(latent + réalisé + dividends). Continuous through sells; the real fix for "sold out → 0".
`_build_positions_from_summary` reads `summary.total_profit_loss` instead of
`summary.profit_loss`, for both crypto and stock snapshots.

Bump `CURRENT_CALC_VERSION` for the affected types so stored history rebuilds once on next
login (crypto: 1→2; stock: 0→1, this bump also carries the cost-basis switch). No migration
— that is the payoff of the calc_version design.

### 5. Display (web, crypto + stock)

- Headline **P/L** and **Performance** stay *latent* (they reconcile with `VALEUR − INVESTI`
  and the positions table).
- Add tiles: **P/L réalisé**, **P/L total**. Stock keeps its **Dividendes** figure.
- "P/L cumulé" chart plots total P/L via the redefined `cumulative_pnl` (crypto + stock).

## Data flow

```
get_*_account_summary:
  fold: on disposal with proceeds → realized += proceeds − cost_removed
  latent   = current_value_acc − total_invested_acc     # stock: now cost-basis too
  realized = realized_acc
  total    = latent + realized (+ total_dividends for stock)
  → AccountSummaryResponse(profit_loss=latent, realized_profit_loss=realized,
                           total_profit_loss=total, ...)

snapshot: cumulative_pnl = summary.total_profit_loss
CURRENT_CALC_VERSION: crypto 1→2, stock 0→1 → history rebuilds once
```

## Edge cases / error handling

- **No disposals** → realized 0, total = latent (+ dividends). Tiles show 0 €.
- **Fully sold out** → latent 0, realized = lifetime result, total = realized (+ dividends);
  curve keeps the value instead of dropping to 0.
- **Latent is None** (no current price) → `total_profit_loss = realized (+ dividends)`;
  keep `profit_loss` (latent) None. Realized/dividends do not need current prices.
- **Swap without anchor** (shouldn't happen for grouped trades) → proceeds 0, realized 0 for
  that leg; never raise.
- **Stock dividends** must not be double-counted: they are income (added once via
  `total_dividends`), never folded into latent or realized.

## Testing

- Crypto: sell-to-fiat at a gain then hold cash → latent 0, realized = gain, total = gain.
- Crypto: buy → sell (gain) → rebuy → realized = first-leg gain; total = latent + realized.
- Crypto: crypto→crypto swap → realized = market value − cost removed.
- Stock: `SELL` at a gain → realized captured; latent = open positions vs cost basis.
- Stock: `DIVIDEND` → dividends component increases; latent unchanged; total = latent +
  realized + dividends (no double count).
- Stock cost-basis switch: account P/L == Σ stock positions' P/L (reconciles with INVESTI).
- Outbound crypto WITHDRAW: realized unchanged (0).
- `cumulative_pnl` == `total_profit_loss`; bumping calc_version rebuilds; curve no longer
  collapses to 0 after a full exit.

## Rollout

Ship calc + DTO + web tiles together; bump `CURRENT_CALC_VERSION` for crypto (1→2) and stock
(0→1) in the same change so both curves realign on next login. The stock cost-basis switch is
part of this change, not a separate step.
