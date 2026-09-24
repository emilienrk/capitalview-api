"""
What kind of operation a label describes: a card payment, a transfer, a direct
debit, a cash withdrawal, interest.

The one place a label is read with banking vocabulary. `flows.py` refuses to,
and rightly: a bank's label format is its own invention, and no total may rest
on guessing it. The type is the assumed exception, and stays harmless by
construction — it feeds display and filtering, and otherwise only ever holds
a question back: the pairing never offers a card payment or a direct debit
against a transfer received. No total and no nature reads it, so a bank format
this lexicon misses yields an incomplete filter or one more question, never a
wrong figure.

It answers only when the label is unambiguous, keyword first, and says UNKNOWN
otherwise — not "other", which would claim the operation was identified. A
Revolut card purchase is labelled with the merchant alone ("Carrefour") and
stays UNKNOWN.

Stored on each row when it is written, and re-derived for every stored row by
the transfer-pattern rebuild: a change to the lexicon bumps
`transfer_patterns._VERSION`, and the history follows without a migration.
"""

from __future__ import annotations

import re
import unicodedata

from dtos.banking import OperationType

# Anchored at the start of the label, accents and case folded. A cancelled
# operation keeps its type: "REJ VIR INST …" is still a transfer.
_LEXICON: tuple[tuple[OperationType, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(r"(?:rej\s+)?(?:" + pattern + r")"))
    for kind, pattern in (
        # "AVOIR" alone is any credit note; with a card number it is a card refund.
        (OperationType.CARD, r"carte\s|tdf emis via cb\b|avoir\s.*\bcb\*|card payment\b"),
        (OperationType.DIRECT_DEBIT, r"prlv\b|prelevement\b|direct debit\b"),
        (
            OperationType.TRANSFER,
            r"vir\b|virement\b|to\s|transfer (?:to|from)\b|paiement (?:envoye|recu) (?:par|de)\b|payment from\b",
        ),
        (OperationType.WITHDRAWAL, r"retrait\b|cash withdrawal\b"),
        (OperationType.INTEREST, r"\*?inter(?:\.|ets\b)|interets\b|interest\b"),
    )
)


def operation_type(label: str | None) -> OperationType:
    folded = _fold(label or "")
    for kind, pattern in _LEXICON:
        if pattern.match(folded):
            return kind
    return OperationType.UNKNOWN


def _fold(label: str) -> str:
    decomposed = unicodedata.normalize("NFKD", label.strip().casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))
