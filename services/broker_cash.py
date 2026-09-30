"""Idle cash on stock and crypto accounts, as net worth counts it.

A negative balance is not a debt: it means the deposits were never entered, and
the purchases were paid from a bank account that already shows the money gone.
Counting it would take that money off net worth twice, so every aggregate floors
it at zero, and wherever flows are measured the part left out reads as a deposit
made on the day it appeared. Account pages keep showing the real balance.
"""

from decimal import Decimal

_ZERO = Decimal("0")


def counted_cash(cash_balance: Decimal) -> Decimal:
    return max(Decimal(cash_balance), _ZERO)


def uncounted_cash(cash_balance: Decimal) -> Decimal:
    return max(-Decimal(cash_balance), _ZERO)
