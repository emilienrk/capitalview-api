"""
Who is being paid, whatever the label says this month: one identity for the
labels a bank writes for the same merchant.

A label changes under a charge that does not: a new bank format appends a
client number or a mandate reference, a card payment becomes a direct debit, a
payment moves from one account to another. `label_groups` keeps the exact
words, which is right for grouping one month's operations; a subscription
spans years of those changes, so this reads the label looser — words
weighted by how rare they are among the user's labels, a word matching its
truncation or a one-letter typo, a label that grew still matching the one it
grew from.

Measured on 53 months of real operations (docs/superpowers/plans/
2026-09-18-subscriptions.md): 3 088 debits, 758 merchants. Pure: labels in,
groups out.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable

from services.banking.label_groups import NOISE_WORDS, display_label

MerchantKey = tuple[str, ...]


def fold(text: str) -> str:
    """Lower case, accents dropped."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


# Beyond the bank plumbing `label_groups` drops: the words of the long format
# Boursorama writes since 2026-06 ("PRELEV", "Numero de client", "SCT"), legal
# forms, HTML entities a bank left escaped ("OLNESS.apos"), web noise and French
# stop words. Not "com" nor "fr": "COM AIR" is named by its first word.
NOISE = frozenset(fold(word) for word in NOISE_WORDS) | frozenset({
    "prelev", "numero", "client", "sct", "vers", "ech",
    "sarl", "sas", "sasu", "eurl", "sa", "sca", "cie", "ltd", "gmbh", "inc", "llc", "srl", "bv", "ag", "plc",
    "apos", "amp", "quot",
    "www", "http", "https", "et", "en", "au", "aux",
})

# The threshold nearby labels already merge at (label_groups.merge_similar),
# where 0.5 let "Carrefour Annecy" swallow "Annecy".
JACCARD_THRESHOLD = 0.6
# A label that grew keeps most of the weight of the one it grew from.
CONTAINMENT_THRESHOLD = 0.75
# A prefix is the same word only when long enough and most of the other one:
# "electronic ar" is "electronic arts", "total" is not "totalenergies".
PREFIX_MIN_LETTERS = 4
PREFIX_MIN_SHARE = 0.5
TYPO_MIN_LETTERS = 5

_RUN = re.compile(r"[^\W_]+")
_FALLBACK = "#"


def merchant_words(label: str | None) -> MerchantKey:
    """The words naming who is paid, in label order: runs of letters of two or
    more, a run holding a digit dropped whole, noise and repeats dropped. A
    label with no such word keys on its cleaned text, compared as is."""
    words: list[str] = []
    for run in _RUN.findall(fold(label or "")):
        if len(run) >= 2 and not any(char.isdigit() for char in run) and run not in NOISE and run not in words:
            words.append(run)
    if words:
        return tuple(words)
    return (_FALLBACK + fold(display_label(label)),)


def _is_fallback(key: MerchantKey) -> bool:
    return key[0].startswith(_FALLBACK)


class Idf:
    """How much a word says, by how few of the user's labels carry it: a word in
    every label (the user's own name) weighs little, a merchant's name much."""

    def __init__(self, keys: Iterable[MerchantKey]):
        distinct = set(keys)
        self.count = len(distinct)
        self.frequency = Counter(word for key in distinct for word in set(key))

    def __call__(self, word: str) -> float:
        return math.log((self.count + 1) / (self.frequency.get(word, 0) + 1)) + 1.0


def words_alike(a: str, b: str) -> bool:
    """The same word, truncated or with a one-letter typo."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= PREFIX_MIN_LETTERS and long_.startswith(short) and len(short) / len(long_) >= PREFIX_MIN_SHARE:
        return True
    return len(short) >= TYPO_MIN_LETTERS and _one_edit(a, b)


def _one_edit(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance of one: a letter changed, added, dropped, or
    two neighbours swapped."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (
            len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
        )
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def same_merchant(a: MerchantKey, b: MerchantKey, idf: Idf) -> bool:
    """Weighted Jaccard over alike words, or a label that grew: most of the
    shorter one's weight found in the longer one, and the same first word — so
    "ANNECY" never swallows "CARREFOUR ANNECY"."""
    if a == b:
        return True
    if _is_fallback(a) or _is_fallback(b):
        return False
    weight_a, weight_b = sum(map(idf, a)), sum(map(idf, b))
    shared = min(
        sum(idf(word) for n, word in enumerate(a) if _found(word, n, a, b)),
        sum(idf(word) for n, word in enumerate(b) if _found(word, n, b, a)),
    )
    union = weight_a + weight_b - shared
    if union > 0 and shared / union >= JACCARD_THRESHOLD:
        return True
    return shared / min(weight_a, weight_b) >= CONTAINMENT_THRESHOLD and words_alike(a[0], b[0])


def _found(word: str, position: int, key: MerchantKey, other: MerchantKey) -> bool:
    if any(words_alike(word, candidate) for candidate in other):
        return True
    # A bank cuts a long label short: "EA *ELECTRONIC AR". Its last word may be
    # the start of the word the full label holds at that place, however short —
    # never the first word, which would make "OVH" of "OVHcloud".
    if position == 0 or position >= len(other):
        return False
    counterpart = other[position]
    return (position == len(key) - 1 and counterpart.startswith(word)) or (
        position == len(other) - 1 and word.startswith(counterpart)
    )


def group_merchants(keys: Iterable[MerchantKey]) -> dict[MerchantKey, int]:
    """One merchant number per key, the same whatever order the keys come in.

    Only keys sharing a word's first four letters are compared, which keeps
    this near linear on a real history.
    """
    ordered = sorted(set(keys))
    idf = Idf(ordered)
    parent = {key: key for key in ordered}

    def find(key: MerchantKey) -> MerchantKey:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    buckets: dict[str, list[MerchantKey]] = defaultdict(list)
    for key in ordered:
        for stem in dict.fromkeys(word[:PREFIX_MIN_LETTERS] for word in key):
            buckets[stem].append(key)
    for stem in sorted(buckets):
        bucket = buckets[stem]
        for x in range(len(bucket)):
            for y in range(x + 1, len(bucket)):
                a, b = bucket[x], bucket[y]
                root_a, root_b = find(a), find(b)
                if root_a != root_b and same_merchant(a, b, idf):
                    parent[max(root_a, root_b)] = min(root_a, root_b)

    numbers: dict[MerchantKey, int] = {}
    groups: dict[MerchantKey, int] = {}
    for key in ordered:
        groups[key] = numbers.setdefault(find(key), len(numbers))
    return groups
