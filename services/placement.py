"""
Placement service — AV, PER, SCPI and the like, followed by hand.

No provider exposes these placements, so a placement is what the user writes down:
the balances read on a statement, and the deposits and withdrawals in between.
Its value on any day is derived from those alone:

    value(d) = last balance before d + net flows since + a share of the gain
               the next balance reveals, accrued linearly between the two

After the last balance, only the flows move it: the app never compounds an
assumed rate into a figure presented as the placement's worth. The user's
expected rate exists, but only the projection reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.placement import (
    PlacementAccountCreate,
    PlacementAccountResponse,
    PlacementAccountUpdate,
    PlacementEntryCreate,
    PlacementEntryResponse,
    PlacementEntryUpdate,
    PlacementSummaryResponse,
)
from dtos.transaction import AccountHistorySnapshotResponse
from models.account_history import AccountHistory
from models.enums import PlacementType, PlacementEntryType
from models.placement import PlacementAccount, PlacementEntry
from services.analytics.returns import annualize
from services.encryption import decrypt_data, encrypt_data, hash_index

_ZERO = Decimal("0")

# Same threshold as projection_basis: under a year, an annualised return is an
# extrapolation of whatever the statements happened to catch.
MIN_DAYS_FOR_A_RATE = 365

# Most providers send one statement a year; half a year past it, the figure on
# show is worth refreshing from the provider's site.
STALE_AFTER_DAYS = 180

TAX_ANNIVERSARY_YEARS = 8


@dataclass(frozen=True)
class EntryPoint:
    day: date
    kind: PlacementEntryType
    amount: Decimal


class PlacementTimeline:
    """A placement's value, deposits and withdrawals on any day, from its entries.

    A balance is read as the value at the end of its day, so a deposit on the
    same day is already inside it.
    """

    def __init__(self, entries: list[EntryPoint]):
        entries = sorted(entries, key=lambda e: e.day)
        self.start: date | None = entries[0].day if entries else None
        self.flows: dict[date, Decimal] = {}
        self.deposits: list[tuple[date, Decimal]] = []
        self.withdrawals: list[tuple[date, Decimal]] = []
        valuations: dict[date, Decimal] = {}
        for entry in entries:
            if entry.kind == PlacementEntryType.VALUATION:
                # Two balances on one day: the one entered last is the reading.
                valuations[entry.day] = entry.amount
            elif entry.kind == PlacementEntryType.DEPOSIT:
                self.flows[entry.day] = self.flows.get(entry.day, _ZERO) + entry.amount
                self.deposits.append((entry.day, entry.amount))
            else:
                self.flows[entry.day] = self.flows.get(entry.day, _ZERO) - entry.amount
                self.withdrawals.append((entry.day, entry.amount))
        self.valuations: list[tuple[date, Decimal]] = sorted(valuations.items())

    def _anchors(self) -> list[tuple[date, Decimal]]:
        # The placement is worth nothing the day before its first entry.
        if self.start is None:
            return []
        return [(self.start - timedelta(days=1), _ZERO), *self.valuations]

    def _net_flows(self, after: date, until: date) -> Decimal:
        return sum((a for d, a in self.flows.items() if after < d <= until), _ZERO)

    def value_on(self, d: date) -> Decimal:
        anchors = self._anchors()
        if not anchors or d < self.start:
            return _ZERO
        index = max(i for i, (day, _) in enumerate(anchors) if day <= d)
        t0, v0 = anchors[index]
        value = v0 + self._net_flows(t0, d)
        if index + 1 < len(anchors):
            t1, v1 = anchors[index + 1]
            gain = v1 - v0 - self._net_flows(t0, t1)
            value += gain * Decimal((d - t0).days) / Decimal((t1 - t0).days)
        return max(value, _ZERO)

    def flow_on(self, d: date) -> Decimal:
        return self.flows.get(d, _ZERO)

    def deposits_until(self, d: date) -> Decimal:
        return sum((a for day, a in self.deposits if day <= d), _ZERO)

    def withdrawals_until(self, d: date) -> Decimal:
        return sum((a for day, a in self.withdrawals if day <= d), _ZERO)

    def annual_return(self) -> tuple[Decimal | None, int]:
        """Time-weighted return between statements, annualised.

        Each span between two balances is measured with Modified Dietz — the
        gain over the capital at work, each flow weighted by the share of the
        span it was invested for — and the spans are chained, so the timing of
        the deposits does not read as performance.

        Returns:
            (annual rate or None, days covered)
        """
        anchors = self._anchors()
        if len(anchors) < 2:
            return None, 0
        days = (anchors[-1][0] - anchors[0][0]).days
        if days < MIN_DAYS_FOR_A_RATE:
            return None, days

        growth = Decimal("1")
        for (t0, v0), (t1, v1) in zip(anchors, anchors[1:]):
            span = Decimal((t1 - t0).days)
            in_span = [(d, a) for d, a in self.flows.items() if t0 < d <= t1]
            net = sum((a for _, a in in_span), _ZERO)
            weighted = sum((a * Decimal((t1 - d).days) / span for d, a in in_span), _ZERO)
            capital = v0 + weighted
            if capital <= 0:
                return None, days
            growth *= Decimal("1") + (v1 - v0 - net) / capital
        return annualize(growth - Decimal("1"), days), days


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _parse_day(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _decrypt_entry(entry: PlacementEntry, master_key: str) -> EntryPoint | None:
    day = _parse_day(decrypt_data(entry.occurred_at_enc, master_key))
    if day is None:
        return None
    return EntryPoint(
        day=day,
        kind=PlacementEntryType(decrypt_data(entry.type_enc, master_key)),
        amount=Decimal(decrypt_data(entry.amount_enc, master_key)),
    )


def _account_entries(session: Session, account_uuid: str) -> list[PlacementEntry]:
    rows = session.exec(
        select(PlacementEntry).where(PlacementEntry.account_uuid == account_uuid)
    ).all()
    # Oldest entered first, so a later balance on the same day wins in the timeline.
    return sorted(rows, key=lambda row: row.created_at)


def build_timeline(session: Session, account_uuid: str, master_key: str) -> PlacementTimeline:
    points = [_decrypt_entry(row, master_key) for row in _account_entries(session, account_uuid)]
    return PlacementTimeline([p for p in points if p is not None])


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def _map_account(
    account: PlacementAccount,
    timeline: PlacementTimeline,
    master_key: str,
    today: date | None = None,
) -> PlacementAccountResponse:
    today = today or datetime.now(timezone.utc).date()
    placement_type = PlacementType(decrypt_data(account.placement_type_enc, master_key))

    deposits = timeline.deposits_until(today)
    withdrawals = timeline.withdrawals_until(today)
    net_invested = deposits - withdrawals
    current_value = timeline.value_on(today)

    gain = gain_pct = None
    last_valuation_date = last_valuation_value = days_since = None
    if timeline.valuations:
        last_valuation_date, last_valuation_value = timeline.valuations[-1]
        days_since = (today - last_valuation_date).days
        gain = current_value - net_invested
        if net_invested > 0:
            gain_pct = round(gain / net_invested * 100, 2)

    annual_rate, return_days = timeline.annual_return()

    tax_anniversary = None
    clock_start = account.opened_at or timeline.start
    if placement_type == PlacementType.AV and clock_start is not None:
        tax_anniversary = _add_years(clock_start, TAX_ANNIVERSARY_YEARS)

    expected = (
        Decimal(decrypt_data(account.expected_return_rate_enc, master_key))
        if account.expected_return_rate_enc
        else None
    )

    return PlacementAccountResponse(
        id=account.uuid,
        name=decrypt_data(account.name_enc, master_key),
        placement_type=placement_type,
        institution_name=(
            decrypt_data(account.institution_name_enc, master_key)
            if account.institution_name_enc
            else None
        ),
        opened_at=account.opened_at,
        expected_return_rate=expected,
        current_value=round(current_value, 2),
        total_deposits=round(deposits, 2),
        total_withdrawals=round(withdrawals, 2),
        net_invested=round(net_invested, 2),
        gain=round(gain, 2) if gain is not None else None,
        gain_percentage=gain_pct,
        last_valuation_date=last_valuation_date,
        last_valuation_value=last_valuation_value,
        days_since_valuation=days_since,
        # A placement with money in it and no balance at all is the stalest case.
        is_stale=(days_since > STALE_AFTER_DAYS) if days_since is not None else net_invested > 0,
        annual_return_rate=round(annual_rate, 4) if annual_rate is not None else None,
        return_days=return_days,
        tax_anniversary_date=tax_anniversary,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _map_entry(entry: PlacementEntry, master_key: str) -> PlacementEntryResponse:
    return PlacementEntryResponse(
        id=entry.uuid,
        account_id=entry.account_uuid,
        type=PlacementEntryType(decrypt_data(entry.type_enc, master_key)),
        amount=Decimal(decrypt_data(entry.amount_enc, master_key)),
        occurred_at=_parse_day(decrypt_data(entry.occurred_at_enc, master_key)),
        note=decrypt_data(entry.note_enc, master_key) if entry.note_enc else None,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------


def user_placements(session: Session, user_uuid: str, master_key: str) -> list[PlacementAccount]:
    user_bidx = hash_index(user_uuid, master_key)
    return list(
        session.exec(
            select(PlacementAccount).where(PlacementAccount.user_uuid_bidx == user_bidx)
        ).all()
    )


def get_owned_account(
    session: Session, account_uuid: str, user_uuid: str, master_key: str
) -> PlacementAccount | None:
    account = session.get(PlacementAccount, account_uuid)
    if account is None or account.user_uuid_bidx != hash_index(user_uuid, master_key):
        return None
    return account


def create_account(
    session: Session, data: PlacementAccountCreate, user_uuid: str, master_key: str
) -> PlacementAccountResponse:
    account = PlacementAccount(
        user_uuid_bidx=hash_index(user_uuid, master_key),
        name_enc=encrypt_data(data.name, master_key),
        institution_name_enc=(
            encrypt_data(data.institution_name, master_key) if data.institution_name else None
        ),
        placement_type_enc=encrypt_data(data.placement_type.value, master_key),
        expected_return_rate_enc=(
            encrypt_data(str(data.expected_return_rate), master_key)
            if data.expected_return_rate is not None
            else None
        ),
        opened_at=data.opened_at,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return _map_account(account, PlacementTimeline([]), master_key)


def update_account(
    session: Session,
    account: PlacementAccount,
    data: PlacementAccountUpdate,
    master_key: str,
) -> PlacementAccountResponse:
    fields = data.model_fields_set
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
    if data.placement_type is not None:
        account.placement_type_enc = encrypt_data(data.placement_type.value, master_key)
    if "institution_name" in fields:
        account.institution_name_enc = (
            encrypt_data(data.institution_name, master_key) if data.institution_name else None
        )
    if "expected_return_rate" in fields:
        account.expected_return_rate_enc = (
            encrypt_data(str(data.expected_return_rate), master_key)
            if data.expected_return_rate is not None
            else None
        )
    if "opened_at" in fields:
        account.opened_at = data.opened_at

    session.add(account)
    session.commit()
    session.refresh(account)
    return _map_account(account, build_timeline(session, account.uuid, master_key), master_key)


def delete_account(session: Session, account: PlacementAccount, master_key: str) -> None:
    session.exec(
        sa.delete(PlacementEntry).where(PlacementEntry.account_uuid == account.uuid)
    )
    session.exec(
        sa.delete(AccountHistory).where(
            AccountHistory.account_id_bidx == hash_index(account.uuid, master_key)
        )
    )
    session.delete(account)
    session.commit()


def get_account(
    session: Session, account: PlacementAccount, master_key: str
) -> PlacementAccountResponse:
    return _map_account(account, build_timeline(session, account.uuid, master_key), master_key)


def get_user_placements(
    session: Session, user_uuid: str, master_key: str
) -> PlacementSummaryResponse:
    accounts = [
        _map_account(account, build_timeline(session, account.uuid, master_key), master_key)
        for account in user_placements(session, user_uuid, master_key)
    ]
    accounts.sort(key=lambda a: a.name.lower())
    return PlacementSummaryResponse(
        total_value=sum((a.current_value for a in accounts), _ZERO),
        total_deposits=sum((a.total_deposits for a in accounts), _ZERO),
        total_withdrawals=sum((a.total_withdrawals for a in accounts), _ZERO),
        net_invested=sum((a.net_invested for a in accounts), _ZERO),
        accounts=accounts,
    )


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def list_entries(
    session: Session, account: PlacementAccount, master_key: str
) -> list[PlacementEntryResponse]:
    entries = [_map_entry(row, master_key) for row in _account_entries(session, account.uuid)]
    entries.sort(key=lambda e: (e.occurred_at, e.created_at), reverse=True)
    return entries


ORPHAN_VALUATION_MESSAGE = (
    "Un relevé de solde doit être précédé d'au moins un versement : sans lui, tout le solde "
    "serait compté comme de la plus-value. Si vous ne connaissez pas le détail de vos "
    "versements, saisissez le total versé à ce jour en un seul versement, à la date d'ouverture."
)


def _require_deposit_before_valuations(points: list[EntryPoint]) -> None:
    """Refuse a placement state holding a balance with no deposit on or before it.

    The value before the first entry is taken as zero, so a balance nothing was
    paid in for would read as entirely gain. Checked on the state an operation
    would leave behind, so an edit or a deletion cannot orphan a balance either.
    """
    deposits = [p.day for p in points if p.kind == PlacementEntryType.DEPOSIT]
    first_deposit = min(deposits) if deposits else None
    for point in points:
        if point.kind == PlacementEntryType.VALUATION and (
            first_deposit is None or point.day < first_deposit
        ):
            raise ValueError(ORPHAN_VALUATION_MESSAGE)


def _points_after(
    session: Session,
    account_uuid: str,
    master_key: str,
    removed: str | None = None,
    added: EntryPoint | None = None,
) -> list[EntryPoint]:
    """The placement's entries once *removed* is gone and *added* is in."""
    points = [
        _decrypt_entry(row, master_key)
        for row in _account_entries(session, account_uuid)
        if row.uuid != removed
    ]
    if added is not None:
        points.append(added)
    return [p for p in points if p is not None]


def create_entry(
    session: Session,
    account: PlacementAccount,
    data: PlacementEntryCreate,
    master_key: str,
) -> PlacementEntryResponse:
    _require_deposit_before_valuations(
        _points_after(
            session, account.uuid, master_key,
            added=EntryPoint(data.occurred_at, data.type, data.amount),
        )
    )
    entry = PlacementEntry(
        account_uuid=account.uuid,
        type_enc=encrypt_data(data.type.value, master_key),
        amount_enc=encrypt_data(str(data.amount), master_key),
        occurred_at_enc=encrypt_data(data.occurred_at.isoformat(), master_key),
        note_enc=encrypt_data(data.note, master_key) if data.note else None,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return _map_entry(entry, master_key)


def get_owned_entry(
    session: Session, account: PlacementAccount, entry_uuid: str
) -> PlacementEntry | None:
    entry = session.get(PlacementEntry, entry_uuid)
    if entry is None or entry.account_uuid != account.uuid:
        return None
    return entry


def entry_day(entry: PlacementEntry, master_key: str) -> date | None:
    return _parse_day(decrypt_data(entry.occurred_at_enc, master_key))


def update_entry(
    session: Session,
    entry: PlacementEntry,
    data: PlacementEntryUpdate,
    master_key: str,
) -> PlacementEntryResponse:
    fields = data.model_fields_set
    current = _decrypt_entry(entry, master_key)
    if data.occurred_at is not None and current is not None:
        _require_deposit_before_valuations(
            _points_after(
                session, entry.account_uuid, master_key,
                removed=entry.uuid,
                added=EntryPoint(data.occurred_at, current.kind, current.amount),
            )
        )
    if data.amount is not None:
        kind = PlacementEntryType(decrypt_data(entry.type_enc, master_key))
        if kind != PlacementEntryType.VALUATION and data.amount <= 0:
            raise ValueError("Le montant d'un versement ou d'un rachat doit être positif.")
        entry.amount_enc = encrypt_data(str(data.amount), master_key)
    if data.occurred_at is not None:
        entry.occurred_at_enc = encrypt_data(data.occurred_at.isoformat(), master_key)
    if "note" in fields:
        entry.note_enc = encrypt_data(data.note, master_key) if data.note else None

    session.add(entry)
    session.commit()
    session.refresh(entry)
    return _map_entry(entry, master_key)


def delete_entry(session: Session, entry: PlacementEntry, master_key: str) -> None:
    _require_deposit_before_valuations(
        _points_after(session, entry.account_uuid, master_key, removed=entry.uuid)
    )
    session.delete(entry)
    session.commit()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _decode_row(row: AccountHistory, master_key: str) -> AccountHistorySnapshotResponse:
    def dec(value: str | None) -> Decimal | None:
        return Decimal(decrypt_data(value, master_key)) if value else None

    total_value = dec(row.total_value_enc) or _ZERO
    return AccountHistorySnapshotResponse(
        snapshot_date=row.snapshot_date,
        total_value=total_value,
        total_invested=dec(row.total_invested_enc) or _ZERO,
        total_deposits=dec(row.total_deposits_enc) or _ZERO,
        total_withdrawals=dec(row.total_withdrawals_enc) or _ZERO,
        daily_pnl=dec(row.daily_pnl_enc),
        cumulative_pnl=dec(row.cumulative_pnl_enc),
    )


def get_placement_account_history(
    session: Session, account: PlacementAccount, master_key: str
) -> list[AccountHistorySnapshotResponse]:
    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == hash_index(account.uuid, master_key))
        .order_by(AccountHistory.snapshot_date)
    ).all()
    return [_decode_row(row, master_key) for row in rows]


def get_all_placements_history(
    session: Session, user_uuid: str, master_key: str
) -> list[AccountHistorySnapshotResponse]:
    """Every placement's snapshots summed by date.

    A placement carries its last snapshot forward over the days it has none, as
    the bank aggregate does, so placements opened at different dates do not make
    the total drop on the days only one of them has a row.
    """
    per_account = [
        get_placement_account_history(session, account, master_key)
        for account in user_placements(session, user_uuid, master_key)
    ]
    all_dates = sorted({snap.snapshot_date for snaps in per_account for snap in snaps})
    cursors = [0] * len(per_account)
    carried: list[AccountHistorySnapshotResponse | None] = [None] * len(per_account)

    result: list[AccountHistorySnapshotResponse] = []
    for d in all_dates:
        value = invested = deposits = withdrawals = daily = cumulative = _ZERO
        for i, snaps in enumerate(per_account):
            while cursors[i] < len(snaps) and snaps[cursors[i]].snapshot_date <= d:
                carried[i] = snaps[cursors[i]]
                cursors[i] += 1
            snap = carried[i]
            if snap is None:
                continue
            value += snap.total_value
            invested += snap.total_invested
            deposits += snap.total_deposits
            withdrawals += snap.total_withdrawals
            cumulative += snap.cumulative_pnl or _ZERO
            if snap.snapshot_date == d:
                daily += snap.daily_pnl or _ZERO
        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=d,
                total_value=value,
                total_invested=invested,
                total_deposits=deposits,
                total_withdrawals=withdrawals,
                daily_pnl=daily,
                cumulative_pnl=cumulative,
            )
        )
    return result
