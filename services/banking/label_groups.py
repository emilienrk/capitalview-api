"""
Who an operation's money went to or came from, read off its label: the key that
groups "CARTE 21/06/26 CARREFOUR ANNECY CB*0837" with every other visit, and a
name a person can read.

Display and grouping only, like `operation_types.py`: no type, no pairing and
no total depends on it. A label this misses lands in a group of its own and
shows a rougher name, never a wrong figure.

The key knows no bank: it is the label's whole words once the words too common
on that side of the user's accounts ("carte", "cb", "vir") are set aside — the
words a nearby-label rule is measured on (`type_rules.telling_words`), so a
salary whose reference changes every month stays one group. The name does read
bank formats, since a card date or a card number is noise to a reader.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from services.banking.type_rules import telling_words

_NOISE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # A card payment or refund and its date.
    r"^(?:rej\s+)?(?:carte|avoir|tdf emis via cb|retrait dab)\s+\d{2}/\d{2}(?:/\d{2,4})?\s+",
    # A transfer or a direct debit, and how it was sent.
    r"^(?:rej\s+)?(?:vir|prlv)(?:\s+(?:sepa|inst))?\s+",
    r"^(?:virement\s+(?:de|à|a)\s*:|paiement envoy[ée] par|to)\s+",
    # The card number, and whatever the bank appends after it.
    r"\s+cb\*\S*.*$",
    r"\s+r[ée]f\b.*$",
    # A pending card payment: "MERCHANT\CITY\ COUNTRY".
    r"\\.*$",
    # A store number or a reference trailing the name.
    r"(?:\s+\S*\d\S*)+$",
))
_SPACES = re.compile(r"\s+")


def group_key(label: str | None, common: frozenset[str]) -> str:
    """The same key for the operations of one counterpart."""
    words = telling_words(label) - common
    if words:
        return " ".join(sorted(words))
    # No word of two letters left ("H&L", "A.R.E.A."): every such card payment
    # would share "carte cb", so the name tells them apart instead.
    return _cleaned(label).lower()


def _cleaned(label: str | None) -> str:
    cleaned = _SPACES.sub(" ", (label or "").strip())
    for pattern in _NOISE:
        cleaned = pattern.sub("", cleaned).strip()
    return cleaned


def display_label(label: str | None) -> str:
    """The label as a reader names the counterpart, noise dropped."""
    cleaned = _cleaned(label) or _SPACES.sub(" ", (label or "").strip())
    letters = [char for char in cleaned if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        cleaned = cleaned.title()
    return cleaned


def group_name(occurrences: list[tuple[date | None, str | None]]) -> str:
    """The name a group goes by: its most frequent display label, the most
    recent one on a tie."""
    names: Counter[str] = Counter()
    latest: dict[str, date] = {}
    for day, label in occurrences:
        name = display_label(label)
        names[name] += 1
        latest[name] = max(latest.get(name, date.min), day or date.min)
    return max(names, key=lambda name: (names[name], latest[name]))
