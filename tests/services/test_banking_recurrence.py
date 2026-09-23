"""
Recurring payment detection (services/banking/recurrence.py) on synthetic debits:
29 edge cases, and the rules each false positive among them taught. Then the
same layers over credits, for recurring income.
"""
import random
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

import pytest

from dtos.banking import CashflowType, OperationType
from services.banking import recurrence as R
from services.banking.merchants import group_merchants, merchant_words
from services.banking.recurrence import RecurrenceOp, Status

TODAY = date(2026, 9, 18)
START = date(2025, 1, 1)
CARD, DD, TRANSFER, UNKNOWN = (
    OperationType.CARD, OperationType.DIRECT_DEBIT, OperationType.TRANSFER, OperationType.UNKNOWN,
)
EXPENSE, NEUTRAL, INCOME = CashflowType.EXPENSE, CashflowType.NEUTRAL, CashflowType.INCOME
INTEREST = OperationType.INTEREST


class _Debit(NamedTuple):
    day: date
    amount: str
    label: str
    method: OperationType = CARD
    account: str = "A"
    type: CashflowType = EXPENSE
    cancelled: bool = False


class Found(NamedTuple):
    confidence: str
    cadence: str
    variable: bool
    count: int
    extras: int
    amount: Decimal
    status: str
    episodes: int
    levels: int


def _ops(debits: list[_Debit]) -> list[RecurrenceOp]:
    keys = {d.label: merchant_words(d.label) for d in debits}
    groups = group_merchants(keys.values())
    return [
        RecurrenceOp(
            f"{n:04d}", d.account, d.day, Decimal(d.amount), "EUR", d.method, d.type, d.cancelled, groups[keys[d.label]],
        )
        for n, d in enumerate(debits)
    ]


def _found(debits: list[_Debit], today: date = TODAY, kind: CashflowType = EXPENSE) -> list[Found]:
    detection = R.detect(_ops(debits), kind)
    found = []
    for series in detection.series:
        confidence = R.confidence(series, R.features(series, detection))
        if confidence is None:
            continue
        found.append(Found(
            confidence.value, series.cadence.name, series.variable, len(series.regular), len(series.extras),
            R.current_amount(series), R.status(series, today, today).value,
            len(R.episodes(series)[0]), len(R.levels(series)),
        ))
    return sorted(found, key=lambda f: (f.amount, f.count))


def _monthly(start: date, n: int, amount, label: str, *, day: int | None = None, lag=(0, 0), skip=(),
             rng: random.Random | None = None, **kw) -> list[_Debit]:
    rng = rng or random.Random(0)
    out = []
    for k in range(n):
        if k in skip:
            continue
        d = R.add_months(start, k)
        if day:
            d = d.replace(day=min(day, 28 if d.month == 2 and d.year % 4 else 29 if d.month == 2 else
                                  30 if d.month in (4, 6, 9, 11) else 31))
        d += timedelta(days=rng.randint(*lag))
        while d.weekday() >= 5 and kw.get("method") is DD:
            d += timedelta(days=1)
        out.append(_Debit(d, str(amount(k) if callable(amount) else amount), label, **kw))
    return out


def _noise(start: date, days: int, per_week: int, label: str, low: float, high: float, rng: random.Random, **kw):
    out, d = [], start
    while (d - start).days < days:
        for _ in range(per_week):
            out.append(_Debit(d + timedelta(days=rng.randint(0, 6)), f"{rng.uniform(low, high):.2f}", label, **kw))
        d += timedelta(days=7)
    return out


def _one(debits, today: date = TODAY) -> Found:
    [found] = _found(debits, today)
    return found


# ---------------------------------------------------------------------------
# The 29 edge cases: found
# ---------------------------------------------------------------------------


def test_a_price_rise_under_card_lag_is_two_levels_of_one_series():
    found = _one(_monthly(START, 16, lambda k: "13.49" if k < 9 else "15.99", "CARTE NETFLIX.COM", day=15, lag=(0, 3)))
    assert found[:4] == ("certain", "monthly", False, 16)
    assert (found.amount, found.levels) == (Decimal("15.99"), 2)


def test_a_rename_carries_the_series_on():
    found = _one(
        _monthly(START, 5, "4.02", "CARTE AllSecur", day=4)
        + _monthly(R.add_months(START, 5), 5, "4.02", "CARTE Allianz Direct", day=4)
    )
    assert found[:4] == ("probable", "monthly", False, 10)


def test_a_missed_month():
    assert _one(_monthly(START, 12, "9.99", "CARTE SPOTIFY", day=8, skip={5}))[:4] == ("probable", "monthly", False, 11)


def test_two_missed_months_in_a_row():
    assert _one(_monthly(START, 12, "9.99", "CARTE SPOTIFY", day=8, skip={5, 6}))[:4] == ("probable", "monthly", False, 10)


def test_a_refunded_month_keeps_the_rhythm_and_not_the_amount():
    debits = [
        d._replace(cancelled=True, type=NEUTRAL) if d.day.month == 6 else d
        for d in _monthly(START, 12, "9.99", "CARTE DEEZER", day=8)
    ]
    found = _one(debits)
    assert found[:4] == ("certain", "monthly", False, 12)
    [series] = R.detect(_ops(debits)).series
    assert sum(not op.cancelled for op in series.regular) == 11


def test_a_pause_is_a_second_episode_of_the_same_series():
    found = _one(
        _monthly(START, 6, "39.00", "PRLV SEPA BASIC FIT", day=10, method=DD)
        + _monthly(R.add_months(START, 11), 6, "39.00", "PRLV SEPA BASIC FIT", day=10, method=DD)
    )
    assert found[:4] == ("certain", "monthly", False, 12)
    assert found.episodes == 2


def test_two_recurring_payments_under_one_label_on_different_days():
    found = _found(
        _monthly(START, 12, "2.99", "CARTE APPLE.COM/BILL", day=3)
        + _monthly(START, 12, "9.99", "CARTE APPLE.COM/BILL", day=18)
    )
    assert [(f.confidence, f.amount, f.count) for f in found] == [
        ("certain", Decimal("2.99"), 12), ("certain", Decimal("9.99"), 12),
    ]


def test_two_recurring_payments_under_one_label_on_the_same_day():
    found = _found(
        _monthly(START, 12, "2.99", "CARTE APPLE.COM/BILL", day=3)
        + _monthly(START, 12, "10.99", "CARTE APPLE.COM/BILL", day=3)
    )
    assert [(f.confidence, f.amount, f.count) for f in found] == [
        ("certain", Decimal("2.99"), 12), ("certain", Decimal("10.99"), 12),
    ]


def test_two_lines_at_the_same_price():
    found = _found(
        _monthly(START, 10, "9.99", "PRLV SEPA FREE MOBILE", day=6, method=DD)
        + _monthly(START, 10, "9.99", "PRLV SEPA FREE MOBILE", day=6, method=DD)
    )
    assert [(f.confidence, f.count) for f in found] == [("certain", 10), ("certain", 10)]


def test_an_annual_recurring_payment_among_a_shops_purchases():
    rng = random.Random(1)
    found = _one(
        [_Debit(date(2023, 3, 10), "69.90", "CARTE AMAZON PRIME FR"),
         _Debit(date(2024, 3, 11), "69.90", "CARTE AMAZON PRIME FR"),
         _Debit(date(2025, 3, 10), "69.90", "CARTE AMAZON PRIME FR")]
        + _noise(date(2023, 1, 1), 900, 1, "CARTE AMAZON EU SARL", 8, 120, rng)
    )
    assert found[:5] == ("probable", "annual", False, 3, 0)


def test_an_annual_insurance_seen_twice():
    found = _one([
        _Debit(date(2024, 11, 2), "119.00", "PRLV SEPA ASSURANCE HABITATION MAAF", method=DD),
        _Debit(date(2025, 11, 3), "124.50", "PRLV SEPA ASSURANCE HABITATION MAAF", method=DD),
    ])
    assert found[:4] == ("probable", "annual", False, 2)
    assert found.status == "active"


def test_a_weekly_standing_order_is_asked():
    found = _one([
        _Debit(START + timedelta(weeks=k), "45.00", "VIR PERMANENT MME DUPONT MENAGE", method=TRANSFER) for k in range(20)
    ])
    assert found[:4] == ("probable", "weekly", False, 20)


def test_a_variable_utility_debit_is_counted():
    rng = random.Random(2)
    found = _one(_monthly(START, 14, lambda k: f"{rng.uniform(40, 90):.2f}", "PRLV SEPA ENGIE", day=5, method=DD))
    assert found[:3] == ("certain", "monthly", True)


def test_a_quarterly_water_bill():
    found = _one([
        _Debit(R.add_months(date(2024, 1, 20), 3 * k), amount, "PRLV SEPA VEOLIA EAU", method=DD)
        for k, amount in enumerate(["85.10", "92.30", "88.00", "90.20", "86.70"])
    ])
    assert found[:4] == ("probable", "quarterly", True, 5)


def test_four_instalments_then_nothing_are_asked_not_counted():
    found = _one(_monthly(date(2026, 1, 12), 4, "62.50", "PRLV SEPA ALMA 4X", method=DD))
    assert found[:4] == ("probable", "monthly", False, 4)
    assert found.status == "ended"


def test_a_loan():
    assert _one(_monthly(date(2024, 6, 5), 24, "350.00", "PRLV SEPA ECHEANCE PRET IMMO", day=5, method=DD))[:4] == (
        "certain", "monthly", False, 24,
    )


@pytest.mark.parametrize("seed", range(10))
def test_an_amount_floating_with_the_exchange_rate_is_one_price(seed):
    rng = random.Random(seed)
    found = _one(_monthly(START, 10, lambda k: f"{10.2 + rng.uniform(-0.15, 0.15):.2f}", "CARTE SPOTIFY USD", day=20))
    assert found[:4] == ("probable", "monthly", False, 10)
    assert found.levels == 1


def test_month_ends():
    assert _one(_monthly(START, 14, "12.00", "PRLV SEPA SFR", day=31, method=DD))[:4] == ("certain", "monthly", False, 14)


def test_every_four_weeks():
    found = _one([
        _Debit(START + timedelta(days=28 * k), "29.99", "PRLV SEPA FITNESS PARK", method=DD) for k in range(9)
    ])
    assert found[:4] == ("certain", "fourweekly", False, 9)


def test_biweekly_pocket_money_is_asked():
    found = _one([_Debit(START + timedelta(days=14 * k), "20.00", "VIR PERMANENT LEO MARTIN", method=TRANSFER) for k in range(12)])
    assert found[:4] == ("probable", "biweekly", False, 12)


def test_a_trial_then_the_full_price():
    found = _one(
        [_Debit(date(2026, 3, 2), "1.00", "CARTE CANAL PLUS FR")]
        + _monthly(date(2026, 4, 2), 6, "11.99", "CARTE CANAL PLUS FR", day=2)
    )
    assert found[:4] == ("probable", "monthly", False, 6)
    assert (found.amount, found.status) == (Decimal("11.99"), "active")


def test_a_card_payment_then_a_direct_debit():
    found = _one(
        [_Debit(date(2025, 1, 9), "39.00", "CARTE OLNESS'")]
        + _monthly(date(2025, 2, 10), 8, "39.00", "PRLV SEPA OLNESS-OLNESS'", day=10, method=DD)
    )
    assert found[:4] == ("certain", "monthly", False, 9)


def test_a_move_to_another_account():
    found = _one(
        _monthly(START, 6, "21.60", "CARTE ANTHROPIC* CLAUDE", day=2, account="A")
        + _monthly(R.add_months(START, 6), 6, "21.60", "Anthropic Claude", day=2, account="B", method=UNKNOWN)
    )
    assert found[:4] == ("certain", "monthly", False, 12)


def test_a_label_that_grows():
    found = _one(
        _monthly(START, 6, "72.96", "PRLV SEPA MACIF Production-MACIF", day=5, method=DD)
        + _monthly(R.add_months(START, 6), 6, "72.96",
                   "PRLV SEPA MACIF Production-MACIF -PRELEV 0306072026 01450093118 RUM MA02", day=5, method=DD)
    )
    assert found[:4] == ("certain", "monthly", False, 12)


# ---------------------------------------------------------------------------
# The 29 edge cases: nothing
# ---------------------------------------------------------------------------


def test_a_supermarket_three_times_a_week():
    assert _found(_noise(START, 400, 3, "CARTE CARREFOUR MARKET", 5, 90, random.Random(4))) == []


def test_a_bakery_at_a_fixed_price_every_other_day():
    rng = random.Random(5)
    assert _found([
        _Debit(START + timedelta(days=2 * k + rng.randint(0, 1)), "1.20", "CARTE BOULANGERIE PAUL") for k in range(150)
    ]) == []


def test_a_canteen_on_weekdays():
    debits = [
        _Debit(START + timedelta(days=k), "3.30", "CARTE CROUS RESTO U")
        for k in range(300) if (START + timedelta(days=k)).weekday() < 5
    ]
    # Too dense for any cadence: not even looked for.
    assert R.detect(_ops(debits)).series == []


@pytest.mark.parametrize("seed", range(30))
def test_transfers_to_a_friend(seed):
    rng = random.Random(seed)
    assert _found([
        _Debit(START + timedelta(days=rng.randint(0, 400)), f"{rng.uniform(3, 60):.1f}", "VIR INST PAUL BOUCHERET", method=TRANSFER)
        for _ in range(25)
    ]) == []


def test_round_top_ups_almost_monthly():
    rng = random.Random(7)
    assert _found([
        _Debit(R.add_months(START, k) + timedelta(days=rng.randint(-6, 6)), "20.00", "CARTE IZLY SMONEY") for k in range(10)
    ]) == []


# ---------------------------------------------------------------------------
# What each false positive taught
# ---------------------------------------------------------------------------


def _at(day: date, account: str = "A") -> RecurrenceOp:
    return RecurrenceOp("x", account, day, Decimal("1"), "EUR", CARD, EXPENSE, False, 0)


def test_a_step_counts_calendar_months():
    monthly = R.CADENCE["monthly"]
    slots, residual = R.step(monthly, _at(date(2026, 1, 31)), _at(date(2026, 2, 28)))
    assert slots == 1 and residual < 0.5
    assert R.step(monthly, _at(date(2024, 1, 29)), _at(date(2024, 2, 29)))[0] == 1
    # Two missed due dates are tolerated, three are not.
    assert R.step(monthly, _at(date(2026, 1, 5)), _at(date(2026, 4, 5)))[0] == 3
    assert R.step(monthly, _at(date(2026, 1, 5)), _at(date(2026, 5, 5))) is None


def test_the_same_amount_at_another_merchant_is_not_a_rename():
    # A restaurant whose last bill happens to be a clothes shop's first.
    debits = [
        _Debit(date(2025, 1, 10), "19.50", "CARTE ESPRIT THAI"),
        _Debit(date(2025, 2, 10), "19.50", "CARTE ESPRIT THAI"),
        _Debit(date(2025, 3, 10), "19.99", "CARTE ESPRIT THAI"),
        _Debit(date(2025, 4, 10), "19.99", "CARTE JULES"),
        _Debit(date(2025, 5, 10), "45.00", "CARTE JULES"),
        _Debit(date(2025, 6, 10), "32.00", "CARTE JULES"),
    ]
    assert all(len(s.merchants) == 1 for s in R.detect(_ops(debits)).series)


def test_two_identical_bills_a_year_apart_at_a_pub_are_not_an_annual_recurring_payment():
    rng = random.Random(8)
    visits = [
        _Debit(date(2025, 3, 14) + timedelta(days=rng.randint(1, 360)), f"{rng.uniform(8, 40):.2f}", "CARTE LE PUB DU COIN")
        for _ in range(4)
    ]
    debits = [_Debit(date(2025, 3, 14), "23.40", "CARTE LE PUB DU COIN"), _Debit(date(2026, 3, 13), "23.40", "CARTE LE PUB DU COIN"), *visits]
    assert _found(debits) == []


def test_a_regular_chain_inside_weekly_shopping_is_not_a_recurring_payment():
    rng = random.Random(9)
    debits = []
    for k in range(12):
        month = R.add_months(date(2025, 1, 1), k)
        debits.append(_Debit(month + timedelta(days=4), f"{rng.uniform(20, 60):.2f}", "CARTE LIDL"))
        for _ in range(2):
            debits.append(_Debit(month + timedelta(days=rng.randint(10, 26)), f"{rng.uniform(5, 60):.2f}", "CARTE LIDL"))
    assert _found(debits) == []


def test_a_rejected_last_debit_leaves_the_question_on_the_one_before():
    debits = _monthly(date(2025, 11, 12), 7, lambda k: ["31.96", "28.40", "35.10", "30.00", "33.50", "29.90", "41.96"][k],
                      "PRLV SEPA COM AIR", method=DD)
    debits[-1] = debits[-1]._replace(cancelled=True, type=NEUTRAL)
    [series] = R.detect(_ops(debits)).series
    assert R.carrier(series).day == debits[-2].day
    assert _one(debits).confidence == "certain"
    # What it costs now: the last three debits paid, the rejected one aside.
    assert R.current_amount(series) == Decimal("30.00")


def test_a_debit_refunded_every_other_time_is_not_a_recurring_payment():
    debits = [
        d._replace(cancelled=True, type=NEUTRAL) if n % 2 == 0 else d
        for n, d in enumerate(_monthly(START, 8, "12.00", "CARTE LW - YAPLA", day=3))
    ]
    assert _found(debits) == []


def test_a_series_mostly_typed_neutral_is_not_a_recurring_payment():
    debits = _monthly(START, 8, "380.00", "PRLV SEPA FREDERIC DURAND", day=5, method=DD)
    debits = [d._replace(type=NEUTRAL) if n < 5 else d for n, d in enumerate(debits)]
    assert _found(debits) == []


def test_a_series_whose_last_debit_is_neutral_is_not_a_recurring_payment():
    debits = _monthly(START, 8, "380.00", "PRLV SEPA FREDERIC DURAND", day=5, method=DD)
    debits[-1] = debits[-1]._replace(type=NEUTRAL)
    assert _found(debits) == []


def test_a_first_card_payment_and_a_last_prorata_are_the_series_extras():
    debits = [
        _Debit(date(2024, 8, 24), "17.00", "CARTE PATHE CINEPASS"),
        *_monthly(date(2024, 9, 9), 5, "16.90", "PRLV SEPA Pathe CinePass", day=9, method=DD),
        # A month skipped, then the last month paid for the days it ran.
        _Debit(date(2025, 3, 10), "14.15", "PRLV SEPA Pathe CinePass", method=DD),
    ]
    [series] = R.detect(_ops(debits)).series
    assert len(series.regular) == 5
    assert sorted(op.amount for op in series.extras) == [Decimal("14.15"), Decimal("17.00")]


def test_extras_are_never_taken_at_a_shop_that_also_bills_a_recurring_payment():
    rng = random.Random(11)
    debits = _monthly(START, 12, "6.99", "CARTE AMAZON PRIME", day=3) + _noise(START, 360, 1, "CARTE AMAZON", 5, 20, rng)
    series = [s for s in R.detect(_ops(debits)).series if s.regular[0].amount == Decimal("6.99")]
    assert [s.extras for s in series] == [[]]


def test_a_new_name_seen_once_takes_over_at_the_next_due_date():
    debits = [*_monthly(date(2026, 2, 4), 3, "7.79", "CARTE OVH SAS", day=4), _Debit(date(2026, 5, 5), "7.79", "CARTE OVHcloud")]
    [series] = R.detect(_ops(debits)).series
    assert len(series.regular) == 4 and len(series.merchants) == 2
    assert [link.kind for link in series.links] == ["rename_once"]


def test_four_debits_28_days_apart_stay_monthly():
    found = _one([_Debit(START + timedelta(days=28 * k), "30.00", "PRLV SEPA ARVERNE FITNESS", method=DD) for k in range(4)])
    assert found.cadence == "monthly"


def test_two_recurring_payments_of_one_merchant_that_overlap_are_never_stitched():
    first = _monthly(date(2026, 3, 4), 7, "4.02", "PRLV SEPA ALLIANZ DIRECT", day=4, method=DD)
    second = _monthly(date(2026, 5, 4), 5, lambda k: "12.31" if k == 0 else "5.81", "PRLV SEPA ALLIANZ DIRECT", day=4, method=DD)
    series = R.detect(_ops(first + second)).series
    assert sorted((len(s.regular), {op.amount for op in s.regular}) for s in series) == [
        (4, {Decimal("5.81")}), (7, {Decimal("4.02")}),
    ]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _series(debits) -> R.Series:
    [series] = R.detect(_ops(debits)).series
    return series


def test_status_on_the_day():
    series = _series(_monthly(date(2026, 1, 5), 6, "9.99", "CARTE DEEZER", day=5))
    assert (series.last.day, R.next_date(series)) == (date(2026, 6, 5), date(2026, 7, 5))
    # A week's grace past the due date, then late while two more may be missed.
    assert R.status(series, date(2026, 7, 12), None) is Status.ACTIVE
    assert R.status(series, date(2026, 7, 13), None) is Status.LATE
    assert R.status(series, date(2026, 9, 10), None) is Status.LATE
    assert R.status(series, date(2026, 9, 11), None) is Status.ENDED
    # An account last synced before the next due date says nothing past it.
    assert R.status(series, date(2026, 9, 11), date(2026, 7, 1)) is Status.STALE
    assert R.status(series, date(2026, 7, 3), date(2026, 7, 1)) is Status.ACTIVE


def test_price_changes_are_between_levels_only():
    # A start-up fee and a discounted month before the price holds.
    series = _series([
        _Debit(date(2026, 5, 20), "35.00", "PRLV SEPA ARVERNE FITNESS BREZET", method=DD),
        _Debit(date(2026, 6, 17), "10.00", "PRLV SEPA ARVERNE FITNESS BREZET", method=DD),
        *[_Debit(date(2026, 7, 15) + timedelta(days=28 * k), "30.00", "PRLV SEPA ARVERNE FITNESS BREZET", method=DD)
          for k in range(3)],
    ])
    assert [level.amount for level in R.levels(series)] == [Decimal("30.00")]
    rise = _series(_monthly(START, 8, lambda k: "13.49" if k < 4 else "15.99", "CARTE NETFLIX.COM", day=15))
    assert [(level.amount, level.count) for level in R.levels(rise)] == [(Decimal("13.49"), 4), (Decimal("15.99"), 4)]
    # A rise of 2.2 %, within what an exchange rate moves, is a price change
    # all the same for a price that repeats to the cent.
    small = _series(_monthly(START, 12, lambda k: "57.30" if k < 6 else "58.55", "PRLV SEPA EDF clients particuliers",
                             day=5, method=DD))
    assert [level.amount for level in R.levels(small)] == [Decimal("57.30"), Decimal("58.55")]


def test_the_annual_estimate_follows_the_cadence_and_the_current_price():
    fourweekly = _series([_Debit(START + timedelta(days=28 * k), "30.00", "PRLV SEPA FITNESS PARK", method=DD) for k in range(6)])
    assert R.annual_estimate(fourweekly) == Decimal("390.00")
    variable = _series(_monthly(START, 6, lambda k: ["40", "60", "50", "90", "70", "30"][k], "PRLV SEPA ENGIE", day=5, method=DD))
    assert R.current_amount(variable) == Decimal("70")
    assert R.annual_estimate(variable) == Decimal("840")


def test_refunds_are_credits_of_the_series_merchant_up_to_four_months_after():
    debits = _monthly(date(2025, 1, 5), 6, "55.00", "PRLV SEPA EDF clients particuliers", day=5, method=DD)
    ops = _ops(debits + [
        _Debit(date(2025, 8, 20), "44.15", "VIR SEPA EDF clients particuliers REGULARISATION", account="B"),
        _Debit(date(2026, 3, 1), "39.26", "VIR SEPA EDF clients particuliers"),
        _Debit(date(2025, 8, 20), "12.00", "VIR SEPA MACIF"),
    ])
    detection = R.detect(ops[:6])
    [series] = detection.series
    refunds = R.linked_refunds(series, ops[6:])
    assert [op.amount for op in refunds] == [Decimal("44.15")]


def test_a_series_marked_by_hand_grows_from_its_debit():
    ops = _ops(
        [_Debit(date(2025, 3, 10), "89.00", "CARTE ASSURANCE SKI")]
        + _monthly(date(2025, 1, 20), 3, "7.00", "CARTE CAFE DU COIN")
    )
    annual = R.seed_series(ops[0], ops, R.CADENCE["annual"])
    assert (annual.cadence.name, [op.day for op in annual.regular]) == ("annual", [date(2025, 3, 10)])
    assert R.next_date(annual) == date(2026, 3, 10)
    grown = R.seed_series(ops[2], ops)
    assert (grown.cadence.name, len(grown.regular)) == ("monthly", 3)


def test_detection_does_not_depend_on_the_order_debits_come_in():
    debits = (
        _monthly(START, 12, "2.99", "CARTE APPLE.COM/BILL", day=3)
        + _monthly(START, 12, "10.99", "CARTE APPLE.COM/BILL", day=3)
        + _noise(START, 300, 2, "CARTE CARREFOUR MARKET", 5, 90, random.Random(10))
    )
    ops = _ops(debits)
    expected = [[op.id for op in s.regular] for s in R.detect(ops).series]
    for seed in range(3):
        shuffled = list(ops)
        random.Random(seed).shuffle(shuffled)
        assert [[op.id for op in s.regular] for s in R.detect(shuffled).series] == expected


# ---------------------------------------------------------------------------
# Income: the same layers over credits
# ---------------------------------------------------------------------------


def _income(credits: list[_Debit], today: date = TODAY) -> list[Found]:
    return _found(credits, today, INCOME)


def _salary(n: int, amount="1380.71", **kw) -> list[_Debit]:
    return _monthly(START, n, amount, "VIR SEPA VILMORIN & CIE SALAIRE", day=28, method=TRANSFER, type=INCOME, **kw)


def _offered(credits: list[_Debit]) -> list[R.Series]:
    detection = R.detect(_ops(credits), INCOME)
    return [s for s in detection.series if R.confidence(s, R.features(s, detection))]


def test_a_salary_paid_by_transfer_for_half_a_year_is_counted_unasked():
    detection = R.detect(_ops(_salary(8, lag=(0, 3))), INCOME)
    [series] = detection.series
    features = R.features(series, detection)
    level = R.confidence(series, features)
    assert (level, series.cadence.name, len(series.regular)) == (R.Confidence.CERTAIN, "monthly", 8)
    assert R.counted_unasked(series, level, features)


def test_a_first_partial_salary_and_one_paid_early_for_the_holidays_are_extras():
    credits = _salary(8, skip={4}) + [
        _Debit(date(2024, 12, 30), "724.01", "VIR SEPA VILMORIN & CIE SALAIRE", TRANSFER, type=INCOME),
        # Paid on the 20th, a week before its due date.
        _Debit(date(2025, 5, 20), "1380.71", "VIR SEPA VILMORIN & CIE SALAIRE", TRANSFER, type=INCOME),
    ]
    [found] = _income(credits)
    assert (found.count, found.extras, found.amount) == (7, 2, Decimal("1380.71"))


def test_a_salary_whose_cents_move_keeps_its_first_months():
    label = "VIR SEPA VILMORIN & CIE SALAIRE"
    credits = _salary(8, lambda k: "1380.99" if k < 2 else "1380.71", skip={2}) + [
        # December's, paid before the holidays: a payment off its due date.
        _Debit(date(2025, 3, 20), "1380.99", label, TRANSFER, type=INCOME),
    ]
    [series] = _offered(credits)
    assert series.first.day == date(2025, 1, 28)


def test_a_salary_that_moves_is_asked():
    amounts = ["487.20", "456.75", "609.00", "1196.24", "1286.76", "1369.20", "1245.04", "1237.24"]
    detection = R.detect(_ops(_salary(8, lambda k: amounts[k])), INCOME)
    [series] = detection.series
    features = R.features(series, detection)
    level = R.confidence(series, features)
    # The first, partial months stay: the employer pays nothing else.
    assert (level, len(series.regular)) == (R.Confidence.PROBABLE, 8)
    assert not R.counted_unasked(series, level, features)


@pytest.mark.parametrize("seed", range(10))
def test_a_parent_s_allowance_starts_at_its_first_repeated_amount(seed):
    rng = random.Random(seed)
    label = "VIR SEPA M JEAN DUPONT"
    # Two years of transfers of any amount, twice a month as on the real
    # history, then the same one every month.
    scattered = [
        _Debit(date(2023, 1, 1) + timedelta(days=rng.randint(0, 700)), f"{rng.uniform(10, 200):.2f}", label,
               TRANSFER, type=INCOME)
        for _ in range(48)
    ]
    allowance = _monthly(date(2025, 1, 4), 16, "250", label, method=TRANSFER, type=INCOME)
    [series] = _offered(scattered + allowance)
    assert series.first.day == date(2025, 1, 4)
    assert {op.amount for op in series.regular} == {Decimal("250")}


def test_a_parent_s_allowance_is_asked_not_counted():
    label = "VIR SEPA MME MARIE DUPONT"
    [found] = _income(_monthly(START, 14, "250", label, day=28, method=TRANSFER, type=INCOME) + [
        _Debit(date(2025, 6, 12), "80", label, TRANSFER, type=INCOME),
        _Debit(date(2025, 9, 2), "35", label, TRANSFER, type=INCOME),
    ])
    # Other transfers from the same payer: sure enough to ask, never to count.
    assert (found.confidence, found.count, found.amount) == ("probable", 14, Decimal("250"))


@pytest.mark.parametrize("seed", range(10))
def test_friends_paying_back_are_no_income(seed):
    rng = random.Random(seed)
    credits = [
        _Debit(START + timedelta(days=rng.randint(0, 600)), f"{rng.uniform(4, 150):.2f}", f"Virement de : {name}",
               TRANSFER, type=INCOME)
        for name in ("TITOUAN MARTIN", "HUGO BERNARD", "MATTEO PETIT") for _ in range(rng.randint(8, 25))
    ]
    assert _income(credits) == []


def test_bank_interest_once_a_year_is_counted_unasked():
    credits = [
        _Debit(date(2025, 1, 2), "13.50", "*INTER.BRUTS 31/12/24", INTEREST, type=INCOME),
        _Debit(date(2026, 1, 2), "9.29", "*INTER.BRUTS 31/12/25", INTEREST, type=INCOME),
    ]
    [found] = _income(credits)
    assert (found.confidence, found.cadence, found.amount) == ("certain", "annual", Decimal("9.29"))


def test_weekly_pocket_money_is_no_recurring_income():
    weeks = [
        _Debit(START + timedelta(days=7 * k), "20", "VIR INST MME MARIE DUPONT", TRANSFER, type=INCOME)
        for k in range(20)
    ]
    assert _income(weeks) == []


def test_credits_the_user_typed_otherwise_are_no_income():
    assert _income([d._replace(type=NEUTRAL) for d in _salary(8)]) == []


def test_a_debit_series_is_never_read_as_income():
    debits = _monthly(START, 8, "39.00", "PRLV SEPA BASIC FIT", day=5, method=DD)
    assert _income(debits) == []
