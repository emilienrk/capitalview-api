"""How a label is read into words: the one reading every comparison of labels
goes through, so no two of them disagree on what a label says.

A word is a run of two letters or more, case and accents folded. A run holding
a digit is dropped whole: a date, a card number, a transfer reference
("ZZ1L2ZJSYU78NB5") changes from one occurrence to the next, and splitting it
on its digits would leave letters behind that no two months share. A store
number glued to a name ("LIDL4364") goes with it. Measured on 4 562 real
labels against a reading that kept those letters: no total, no type and no
recurring payment moved, and one question fewer was asked.

Built on it, strictest first:

- `label_signature`: every word of a label, as one key — the identity a rule,
  a flow question and a transfer's shape are keyed on;
- `label_groups.group_key`: the words naming the counterpart, bank plumbing set
  aside — what the lists group and display by;
- `merchants.same_merchant`: words weighted by rarity, a truncation or a typo
  forgiven — one merchant across years of label changes.

Two sets of words are near when they share `SIMILARITY_THRESHOLD` of all
their words: a rule reaching a nearby label, two groups merging, a leg reading
like a transfer the user settled.
"""

import re
import unicodedata

# Measured by replaying four years of transfer decisions month by month:
# lower, a "VIR INST <someone else>" passed for the user's own "VIR INST
# <user>" and true transfers were rejected by association; higher, only more
# questions. Groups merging at 0.5 let "Carrefour Annecy" swallow "Annecy".
SIMILARITY_THRESHOLD = 0.6

_RUN = re.compile(r"[^\W_]+")


def fold(text: str) -> str:
    """Lower case, accents dropped."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def words_in_order(label: str | None) -> list[str]:
    return [
        run for run in _RUN.findall(fold(label or ""))
        if len(run) >= 2 and not any(char.isdigit() for char in run)
    ]


def label_words(label: str | None) -> frozenset[str]:
    return frozenset(words_in_order(label))


def label_signature(label: str | None) -> str | None:
    """Every word of a label in one key; None for a label without any."""
    words = label_words(label)
    return " ".join(sorted(words)) if words else None


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """The share of their words two sets have in common."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0
