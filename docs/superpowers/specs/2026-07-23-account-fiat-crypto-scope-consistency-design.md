# Account summary: cash vs holdings scope consistency (crypto + stock)

**Date:** 2026-07-23
**Status:** Approved design, ready for implementation plan
**Scope:** `capitalview-api` (summary aggregate, crypto + stock) + `capitalview-web` (cards)

## Problem

Both account aggregates mix scopes:

- `total_invested` (INVESTI card) = **holdings** cost basis only (crypto / stocks).
- `current_value` (VALEUR card) = value of **all** positions, **including idle fiat cash**
  (crypto: `crypto_transaction.py`; stock: `stock_transaction.py:643-644`).

So as soon as the account holds cash, `VALEUR − INVESTI ≠ P/L`: the difference is the cash
balance, silently counted as if it were unrealized gain.

- **Crypto**: masked today only because the user holds ~0 cash. Latent.
- **Stock**: active. Stock routinely holds EUR cash (deposits parked before buying, sale
  proceeds, dividends). Worse, the web page already shows a **"Liquidités"** tile
  (`Stock.vue:411`, the EUR position amount) **while VALEUR still includes that same cash** →
  the cash is **double-counted** (once in VALEUR, once in Liquidités).

## Goal

Make portfolio metrics scope-consistent for both account types so `VALEUR − INVESTI = P/L`
always holds, and show idle cash explicitly (once) instead of folding it into portfolio
value.

## Non-goals

- Multi-currency cash beyond converting each fiat position to the display currency (already
  done via exchange rate).
- Treating deposited-but-uninvested cash as "invested" (rejected — cash is not invested).

## Design

Define portfolio metrics on **holdings only** (exclude fiat cash), and expose cash as a
separate field, for both `get_crypto_account_summary` and `get_stock_account_summary`:

- `total_invested` = Σ holdings cost basis (unchanged).
- `current_value` (VALEUR) = Σ **holdings** positions' value (drop fiat from this total).
- `profit_loss` (latent) = `current_value − total_invested` — reconciles exactly, even with
  cash present.
- New `cash_balance: Decimal` = Σ value of fiat positions (EUR + non-EUR fiat converted).

`total_value` for the account (net worth / cross-account aggregates) stays
`current_value + cash_balance` — full liquidation value — but as a **distinct** field from
the VALEUR card figure.

### Web

- VALEUR card = holdings value (`current_value`).
- **Disponible / Liquidités** tile = `cash_balance` (served by the API now, instead of the
  stock page's client-side `positions.find(EUR)`; crypto gains the same tile). No more
  double count, since VALEUR excludes cash.
- Net-worth / dashboard aggregates use full account value (`current_value + cash_balance`).

## Data flow

```
get_*_account_summary:
  holdings_value = Σ non-fiat positions.current_value
  cash_balance   = Σ fiat positions.current_value
  current_value  = holdings_value            # VALEUR card, holdings-scoped
  profit_loss    = holdings_value − total_invested
  cash_balance   = cash_balance              # new field
  # full account value = current_value + cash_balance (net worth)
```

## Edge cases / error handling

- **No cash** → `cash_balance = 0`; behaviour identical to today (crypto common case).
- **Negative fiat balance** (over-spend artifact already tolerated) → included as-is in
  `cash_balance`; never counted as portfolio value.
- **Only cash, no holdings** → `current_value = 0`, `profit_loss = 0`, `cash_balance` shows
  the cash.
- **Call sites reading old `current_value` as total account value** (net worth, dashboards,
  history `total_value`) must switch to `current_value + cash_balance`. **Audit and update
  every consumer** — this is the main risk of the change.

## Testing

- Crypto & stock holding assets + leftover EUR: `VALEUR − INVESTI == P/L` exactly;
  `cash_balance` == EUR value; total value == VALEUR + cash.
- Stock: cash no longer double-counted (VALEUR + Liquidités != cash twice).
- No-cash account: numbers identical to pre-change (regression guard).
- Net-worth / dashboard aggregate still equals full liquidation value after the split.

## Interaction with other work

Changes the composition of stored snapshots (`current_value` scope, `total_value`) → bump
`CURRENT_CALC_VERSION` for the affected types so history recomputes once. If shipped close to
the realized-P/L spec, coordinate the bumps to avoid a double rebuild.
