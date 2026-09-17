"""
How each operation counts in the real cashflow: one type per operation, and
the sign its amount takes there.

Pure: every input is already decrypted and loaded by `flows.py`.

The order is fixed, strongest first. A pair the pairing trusts is a transfer or
a cancellation whatever the user said of either leg — it is undone through the
transfer decisions, not here. Then what the user forced on this very
operation, then the rule of its label, then the deposit found facing it on one
of the user's investment accounts, and only then the direction.

A type is never guessed from a label's vocabulary: NEUTRAL only comes from a
pair or from the user, and saving or investing from a pair, from the user, or
from a movement the user declared on the account the money went to.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from dtos.banking import BankTransferStatus, CashflowType, TypeSource

# Pairs the pairing trusts as one transfer between two of the user's accounts.
TRANSFERS = frozenset({
    BankTransferStatus.SAVINGS, BankTransferStatus.RECURRING,
    BankTransferStatus.LEARNED, BankTransferStatus.CONFIRMED,
})
CANCELLATIONS = frozenset({BankTransferStatus.REVERSAL, BankTransferStatus.REFUND})


@dataclass(frozen=True)
class Resolution:
    type: CashflowType
    source: TypeSource
    rule_id: str | None = None


def resolve_type(
    is_credit: bool,
    transfer_status: BankTransferStatus | None,
    savings_legs: int,
    override: CashflowType | None,
    rule: tuple[str, CashflowType] | None,
    contributed: bool = False,
) -> Resolution:
    """One operation's type.

    `transfer_status` is its pair's, None when unpaired; `savings_legs` is how
    many of the pair's two accounts hold savings; `rule` is the (id, type) of
    the rule its label reaches, exact or nearby. A pair only offered to the user
    is not a pair yet. `contributed` is set when a deposit the user declared on
    an investment account proves this very operation
    (`services/banking/contributions.py`) — it comes after the user's own
    answers, which correct a deduction rather than being corrected by it.
    """
    if transfer_status in CANCELLATIONS:
        return Resolution(CashflowType.NEUTRAL, TypeSource.PAIR)
    if transfer_status in TRANSFERS:
        # Exactly one savings leg puts money aside or takes it back; between two
        # savings accounts, or two current ones, money only moved.
        kind = CashflowType.SAVING if savings_legs == 1 else CashflowType.NEUTRAL
        return Resolution(kind, TypeSource.PAIR)
    if override is not None:
        return Resolution(override, TypeSource.OVERRIDE)
    if rule is not None:
        rule_id, kind = rule
        return Resolution(kind, TypeSource.RULE, rule_id)
    if contributed:
        # A debit went to the investment account, a credit came back from it.
        return Resolution(CashflowType.INVESTMENT, TypeSource.CONTRIBUTION)
    return Resolution(CashflowType.INCOME if is_credit else CashflowType.EXPENSE, TypeSource.DEFAULT)


def counted_leg(is_credit: bool, on_savings_account: bool, kind: CashflowType, paired: bool) -> bool:
    """Whether this leg of a pair is the one the pair counts on, so it counts once.

    Saving reads on the leg outside the savings account: its debit puts money
    aside, its credit takes it back. Any other pair reads on its debit. An
    unpaired operation always counts.
    """
    if not paired or kind not in (CashflowType.SAVING, CashflowType.NEUTRAL):
        return True
    if kind is CashflowType.SAVING:
        return not on_savings_account
    return not is_credit


def signed_amount(amount: Decimal, is_credit: bool, kind: CashflowType) -> Decimal:
    """An amount in its type's own direction: a credit is income, a debit is
    spent, set aside or invested, and the reverse is taken back off — a refund
    typed EXPENSE lowers the expenses of the month it lands in."""
    if kind is CashflowType.INCOME:
        return amount if is_credit else -amount
    if kind is CashflowType.NEUTRAL:
        return amount
    return -amount if is_credit else amount
