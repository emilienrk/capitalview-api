"""How many banking days separate two dates, on the euro area's calendar.

Money moves on the days TARGET2 settles: one calendar for every euro bank,
unlike national public holidays. Counted in calendar days, a transfer sent on
a Friday and booked on Tuesday reads four days apart for two days of
settlement, and one around Easter or Christmas reads further still.

Read by the pairing of internal transfers (`flows`) and by the matching of a
declared deposit with its bank movement (`contributions`).
"""

from datetime import date, timedelta
from functools import lru_cache


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


@lru_cache(maxsize=64)
def _target_closing_days(year: int) -> frozenset[date]:
    """The days TARGET2 does not settle, weekends aside."""
    easter = _easter_sunday(year)
    return frozenset({
        date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
        date(year, 5, 1), date(year, 12, 25), date(year, 12, 26),
    })


def banking_days_between(first: date, second: date) -> int:
    """Settlement days stepped over going from the earlier date to the later one."""
    low, high = sorted((first, second))
    count = 0
    day = low
    while day < high:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in _target_closing_days(day.year):
            count += 1
    return count
