"""Attribution of an atomic crypto group's euro cost to the BUY rows inside it.

Kept in a module of its own because the summary, the per-asset PRU, the price
timeline and the community stats all replay the same ledger, and
services.market cannot import services.crypto_transaction without a cycle.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal

_ZERO = Decimal("0")


def split_group_cost(
    buys: Iterable[tuple[str, str, str, Decimal]],
    group_cost: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """Cost carried by each BUY row, keyed by row id.

    *buys* holds ``(row_id, group_uuid, asset_key, amount)`` for every grouped
    BUY. A group's cost is what the whole order cost, so an order an exchange
    filled in several parts — several BUY rows under one ANCHOR — must share it
    rather than each claim all of it. Fills of one asset share it by quantity;
    a group buying different assets has no common unit, so its rows share it
    evenly. The last row takes the rounding remainder, so the shares always sum
    back to the group's cost exactly.
    """
    rows_by_group: dict[str, list[tuple[str, str, Decimal]]] = defaultdict(list)
    for row_id, group, asset_key, amount in buys:
        rows_by_group[group].append((row_id, asset_key, abs(Decimal(amount or 0))))

    costs: dict[str, Decimal] = {}
    for group, rows in rows_by_group.items():
        total = group_cost.get(group, _ZERO)
        weights = [amount for _, _, amount in rows]
        if len({asset_key for _, asset_key, _ in rows}) > 1 or sum(weights) <= 0:
            weights = [Decimal(1)] * len(rows)
        weight_sum = sum(weights)

        assigned = _ZERO
        for index, ((row_id, _, _), weight) in enumerate(zip(rows, weights)):
            if index == len(rows) - 1:
                costs[row_id] = total - assigned
            else:
                share = total * weight / weight_sum
                costs[row_id] = share
                assigned += share
    return costs
