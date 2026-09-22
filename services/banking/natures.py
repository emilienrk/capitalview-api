"""
What a recurring payment is for: what the user said, and nothing else.

Nothing is guessed here. A merchant dictionary was tried and dropped: naming
what a payment is for is a judgement (a phone plan is not a roof), and the app
shows what is paid rather than saying what should be cut. Unset is unset, and
the screen says so.
"""

from __future__ import annotations

from dtos.banking import RecurringNature


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
