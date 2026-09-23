"""
What a recurring payment is for, or where a recurring income comes from: what
the user said, and nothing else.

Nothing is guessed here. A merchant dictionary was tried and dropped: naming
what a payment is for is a judgement (a phone plan is not a roof), and the app
shows what is paid rather than saying what should be cut. Unset is unset, and
the screen says so.
"""

from __future__ import annotations

from dtos.banking import RecurringDirection, RecurringNature

INCOME_NATURES = frozenset({
    RecurringNature.SALARY, RecurringNature.ALLOWANCE, RecurringNature.PENSION, RecurringNature.RENTAL,
    RecurringNature.SUPPORT, RecurringNature.INTEREST, RecurringNature.OTHER,
})
EXPENSE_NATURES = frozenset(RecurringNature) - INCOME_NATURES | {RecurringNature.OTHER}


def of(nature: str | None) -> RecurringNature | None:
    """The nature the user set, None when they have not said.

    A stored value this list no longer knows reads as unset: a nature dropped
    from the enum must not make a payment unreadable."""
    if not nature:
        return None
    try:
        return RecurringNature(nature)
    except ValueError:
        return None


def fits(nature: RecurringNature, direction: RecurringDirection) -> bool:
    """Whether a nature files a recurring of this direction: a salary is never
    a payment, a phone plan never an income."""
    return nature in (INCOME_NATURES if direction is RecurringDirection.INCOME else EXPENSE_NATURES)
