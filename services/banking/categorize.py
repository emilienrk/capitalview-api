"""
Which category an operation falls under, and how it counts.

Resolved when operations are read, like transfer pairing, and never copied onto
them: a rule written today files the whole history at once, and deleting it
unfiles it just as fully. Only the user's override of one operation is stored
on its row.

A rule is a set of words that must *all* appear in a label. Not a fixed key
such as "the first two telling words": measured on real data, whichever common
words that key skipped, every direct debit collapsed under `prlv sepa`. The
words are those of `transactions.label_words`, so a rule and a label speak the
same language, and no bank's vocabulary is known here — only how often each
word occurs in the user's own history.

Everything in this module is pure.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from dtos.banking import (
    BankTransferStatus,
    CategoryNature,
    CategorySource,
    OperationNature,
    RuleSource,
)
from services.banking.transactions import label_words

if TYPE_CHECKING:
    from services.banking.categories import Category

# An operation's override saying "no category", whatever the rules say.
NO_CATEGORY = "none"

# How many telling words a proposed rule keeps: one more narrows a merchant to
# a place, which the user can still untick.
PROPOSED_WORDS = 2

_CANCELLATIONS = frozenset({BankTransferStatus.REVERSAL, BankTransferStatus.REFUND})
# The pairs kept out of the totals as transfers (flows.DEDUCTED, less the cancellations).
_TRANSFERS = frozenset({
    BankTransferStatus.SAVINGS, BankTransferStatus.RECURRING,
    BankTransferStatus.LEARNED, BankTransferStatus.CONFIRMED,
})


class EmptyRuleError(ValueError):
    """A rule with no word would file every operation."""


class TooGeneralRuleError(ValueError):
    """A rule made only of words found everywhere would file most operations."""


@dataclass(frozen=True)
class Rule:
    uuid: str
    tokens: frozenset[str]
    category_uuid: str
    source: RuleSource
    created_at: datetime


@dataclass(frozen=True)
class Resolution:
    category: Category | None = None
    source: CategorySource | None = None
    rule_uuid: str | None = None


@dataclass(frozen=True)
class WordFrequency:
    """How many distinct label signatures of the user's history hold each word.

    Signatures, not operations: a merchant visited every week is one signature
    repeated, and counting its operations would make its name look as common as
    "carte".
    """
    counts: dict[str, int] = field(default_factory=dict)
    # A word held by more signatures than this tells nothing apart.
    common_above: float = 0.0

    def of(self, word: str) -> int:
        return self.counts.get(word, 0)

    def is_common(self, word: str) -> bool:
        return self.of(word) > self.common_above


def rule_tokens(words: Iterable[str]) -> frozenset[str]:
    """Whatever the user typed or ticked, in the words a label is read as."""
    return frozenset(token for word in words for token in label_words(word))


def check_rule(tokens: frozenset[str], frequency: WordFrequency) -> None:
    """Refuse the two rules measured to swallow the history: an empty one, and
    one whose every word is common (`carte cb` filed 620 signatures)."""
    if not tokens:
        raise EmptyRuleError()
    if all(frequency.is_common(token) for token in tokens):
        raise TooGeneralRuleError(sorted(tokens))


def propose_tokens(label: str | None, frequency: WordFrequency) -> list[str]:
    """The words a rule for this label should require, rarest first.

    Up to PROPOSED_WORDS words below the common threshold. A word held by a
    single signature is only taken when no other word qualifies: it is usually
    a reference unique to this operation — a payslip's `GJPBAZZ` — and a rule
    on it would file nothing else. With no word below the threshold at all,
    every word comes back, and the rule is refused if the user keeps them all.
    """
    words = sorted(label_words(label), key=lambda word: (frequency.of(word), word))
    telling = [word for word in words if not frequency.is_common(word)]
    recurring = [word for word in telling if frequency.of(word) > 1]
    chosen = recurring or telling
    return chosen[:PROPOSED_WORDS] if chosen else words


def resolve(
    words: frozenset[str],
    override: str | None,
    rules: list[Rule],
    categories: dict[str, Category],
) -> Resolution:
    """The category of one operation, from its label's words and its override.

    1. The override: "none" files it nowhere and the rules are not read; an
       override naming a deleted category reads as uncategorised.
    2. The rules whose words all appear: the most specific wins (most words),
       then the user's over the AI's, then the most recent.
    3. Otherwise, nothing.
    """
    if override is not None:
        category = categories.get(override)
        if category is not None:
            return Resolution(category, CategorySource.MANUAL)
        if override == NO_CATEGORY:
            return Resolution(source=CategorySource.MANUAL)
        return Resolution()

    matching = [
        rule for rule in rules
        if rule.category_uuid in categories and rule.tokens <= words
    ]
    if not matching:
        return Resolution()
    best = min(
        matching,
        key=lambda rule: (
            -len(rule.tokens),
            rule.source is not RuleSource.USER,
            -rule.created_at.timestamp(),
            rule.uuid,
        ),
    )
    source = CategorySource.USER_RULE if best.source is RuleSource.USER else CategorySource.AI_RULE
    return Resolution(categories[best.category_uuid], source, best.uuid)


def nature_of(
    is_credit: bool,
    transfer_status: BankTransferStatus | None,
    savings_legs: int,
    category: Category | None,
) -> OperationNature:
    """How one operation counts.

    `transfer_status` is its pair's, None when unpaired; `savings_legs` is how
    many of the pair's two accounts are savings accounts. A pair only offered
    to the user is not a pair yet: its legs count by their category.

    A transfer with exactly one savings leg puts money aside or takes it back —
    which of the two reads on the other leg, a debit or a credit. Between two
    savings accounts, or two current ones, money only moved.
    """
    if transfer_status in _CANCELLATIONS:
        return OperationNature.NEUTRALIZED
    if transfer_status in _TRANSFERS:
        return OperationNature.SAVING if savings_legs == 1 else OperationNature.INTERNAL
    if category is not None:
        return _NATURE_OF_CATEGORY[category.nature]
    return OperationNature.INCOME if is_credit else OperationNature.EXPENSE


_NATURE_OF_CATEGORY = {
    CategoryNature.EXPENSE: OperationNature.EXPENSE,
    CategoryNature.INCOME: OperationNature.INCOME,
    CategoryNature.SAVING: OperationNature.SAVING,
    CategoryNature.INVESTMENT: OperationNature.INVESTMENT,
}
