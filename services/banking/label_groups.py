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
from collections import Counter, defaultdict
from datetime import date

from services.banking.transfer_decisions import SIMILARITY_THRESHOLD
from services.banking.type_rules import telling_words

# The plumbing a bank writes around a counterpart's name. `label_common` already
# drops what is too frequent on one side of a user's accounts, but a format seen
# on a handful of labels survives it ("TDF EMIS VIA CB"), and two counterparts
# then read as one. Kept short on purpose: only words no counterpart is named
# after, and French stop words a name carries no meaning through.
NOISE_WORDS = frozenset({
    "achat", "avoir", "carte", "cb", "courant", "dab", "emis", "envoye", "envoyé", "inst",
    "mandat", "paiement", "payment", "prelevement", "prlv", "recu", "reçu", "ref", "réf",
    "rej", "retrait", "rum", "sent", "sepa", "tdf", "transfer", "via", "vir", "virement",
    "de", "des", "du", "from", "la", "le", "les", "par", "pour", "to",
})

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


def group_words(label: str | None, common: frozenset[str]) -> frozenset[str]:
    """What names the counterpart in a label: its words, bank plumbing aside."""
    return telling_words(label) - common - NOISE_WORDS


def group_key(label: str | None, common: frozenset[str]) -> str:
    """The same key for the operations of one counterpart."""
    words = group_words(label, common)
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


def merge_similar(groups: list[tuple[str, frozenset[str], int]]) -> dict[str, str]:
    """One key per counterpart, for the groups of a single side.

    A bank writes the same counterpart several ways ("VILMORIN & CIE", "VILMORIN
    & CIE SALAIRE DE 2026-08"), and each spelling would take a line of its own.
    Two groups sharing enough of their words are the same counterpart, measured
    as a nearby label is (`transfer_decisions.SIMILARITY_THRESHOLD`). The group
    carrying the most operations gives the merged key, so the name a reader
    knows wins; ties go to the first key in order.

    Only groups sharing a word are compared, which keeps this linear in
    practice: a four-year history compares a couple of thousand pairs.
    """
    order = {key: n for n, (key, _, _) in enumerate(groups)}
    words = {key: group_words for key, group_words, _ in groups}
    weight = {key: (-count, order[key]) for key, _, count in groups}
    by_word: dict[str, list[str]] = defaultdict(list)
    for key, group_words, _ in groups:
        for word in group_words:
            by_word[word].append(key)

    parent = {key: key for key, _, _ in groups}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for key, group_words, _ in groups:
        for word in group_words:
            for other in by_word[word]:
                if order[other] <= order[key] or not _alike(group_words, words[other]):
                    continue
                first, second = find(key), find(other)
                if first != second:
                    parent[max(first, second, key=lambda k: weight[k])] = min(first, second, key=lambda k: weight[k])
    return {key: find(key) for key, _, _ in groups}


def _alike(words: frozenset[str], others: frozenset[str]) -> bool:
    # Both hold the word they were found by, so neither is empty: a group no
    # word names is never compared, and stays on its own.
    return len(words & others) / len(words | others) >= SIMILARITY_THRESHOLD
