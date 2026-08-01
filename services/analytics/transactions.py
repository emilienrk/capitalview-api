"""Reading a transaction row, defined once.

Every analytics module needs the same two things from a transaction: its type as
a plain string, and the calendar day it was executed on. Both were copied into
six modules, and copies drift — `_tx_day` had already picked up two spellings.

The rows these read are duck-typed on purpose. They arrive as decrypted
`TransactionResponse` objects in production and as plain stubs in tests, so
nothing here may assume an ORM model or a concrete enum.
"""

from datetime import date


def tx_type(tx) -> str:
    """The transaction type as a plain string.

    Compares equal to `StockTransactionType` members, which subclass `str`, so
    callers can test against the enum without converting anything.
    """
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def tx_day(tx) -> date | None:
    """The day the transaction was executed, or None when it carries no date."""
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None
