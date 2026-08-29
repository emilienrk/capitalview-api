"""The currencies CapitalView knows about, in one place.

Single source of truth for the codes the app supports and the names it shows.
Before this module the same twelve codes were spelled out three times — in
`dtos/crypto.py`, in the web app's `cryptoTransactionTypes.ts`, and in a
shorter, arbitrarily different list in the bank account form.

**Curated shortlist, not a whitelist.** `SUPPORTED_CURRENCIES` drives what the
interface offers. It is deliberately *not* what the API validates against: a
bank may legitimately answer with a code that is not listed, and refusing it
would make an account unsaveable for no good reason. What the API enforces is
that a currency can actually be converted — `services.bank.require_convertible`,
which asks the market data, not this list.

See docs/currencies.md.
"""

from __future__ import annotations

from typing import NamedTuple


class Currency(NamedTuple):
    code: str
    name: str


# The pivot every stored total, curve and aggregate is expressed in. An account
# holds its own currency; everything summed across accounts is converted here.
BASE_CURRENCY = "EUR"

# Ordered for display: the euro first, then by how likely a French holder is to
# meet them. Adding one is a single line — the API route and the web app both
# read this list.
SUPPORTED_CURRENCIES: tuple[Currency, ...] = (
    Currency("EUR", "Euro"),
    Currency("USD", "Dollar américain"),
    Currency("GBP", "Livre sterling"),
    Currency("CHF", "Franc suisse"),
    Currency("CAD", "Dollar canadien"),
    Currency("JPY", "Yen japonais"),
    Currency("AUD", "Dollar australien"),
    Currency("NZD", "Dollar néo-zélandais"),
    Currency("SEK", "Couronne suédoise"),
    Currency("NOK", "Couronne norvégienne"),
    Currency("DKK", "Couronne danoise"),
    Currency("CNY", "Yuan chinois"),
)

CURRENCY_CODES: frozenset[str] = frozenset(c.code for c in SUPPORTED_CURRENCIES)

# ISO 4217's code for "no currency". Boursorama returns it on the account
# resource, and reading it as a real currency would make a balance meaningless.
NO_CURRENCY = "XXX"
