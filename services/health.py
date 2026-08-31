"""The deep health check the dashboard reads.

Follows draft-inadarei-api-health-check: a `status` of pass/warn/fail, a `checks`
object keyed `component:measurement`, served as `application/health+json`. One
contract, so a dashboard aggregating several services writes a single parser.

Two rules shape what is in here.

It never calls a provider. Yahoo and CoinMarketCap are judged on the freshness of
what they already wrote — a health route that reached out would burn API credits
on every scrape and report red whenever a third party hiccuped, which is the
classic way a deep check turns one outage into two.

It never carries an amount, a merchant, a category or anything else decrypted.
The store is encrypted per user; this route is read by another service, so it
answers in identifiers, counters, dates and durations only.
"""

import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import sqlalchemy as sa
from sqlmodel import Session, select

from models import BankAccountLink, JobRun, JobStatus, MarketAsset, MarketPriceHistory
from models.enums import AssetType

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# Worst-first, so aggregating a set of checks is a max().
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}

# A price older than this is stale. Two days rather than one: the job runs at
# 23:30 and markets close at the weekend, so a Sunday reading is legitimately
# behind without anything being broken.
_PRICE_STALE_AFTER = timedelta(days=3)
_PRICE_WARN_AFTER = timedelta(days=2)

# The nightly job is expected once a day; past this it has silently stopped.
_CRON_STALE_AFTER = timedelta(days=2)

# A run still marked "running" after this died without closing its row.
_RUN_STUCK_AFTER = timedelta(hours=6)

# Which provider feeds which kind of asset, for reporting freshness by source.
_SOURCE_OF = {
    AssetType.STOCK: "yahoo",
    AssetType.FIAT: "yahoo",
    AssetType.CRYPTO: "coinmarketcap",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aged(value: datetime | date | None) -> timedelta | None:
    """How long ago *value* was, tolerating a naive datetime or a bare date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    return _now() - moment


def _entry(status: str, **fields) -> dict:
    """One element of a `checks` array, timestamped as the spec asks."""
    return {"status": status, "time": _now().isoformat(), **fields}


@lru_cache
def _expected_revision() -> str | None:
    """The migration head the running code carries, or None if unreadable."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception:
        return None


def _check_postgres(session: Session) -> list[dict]:
    started = time.perf_counter()
    session.exec(select(1))
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return [
        _entry(
            PASS if elapsed_ms < 500 else WARN,
            componentType="datastore",
            observedValue=elapsed_ms,
            observedUnit="ms",
        )
    ]


def _check_migrations(session: Session) -> list[dict]:
    """Whether the database is on the revision this code expects."""
    try:
        applied = session.exec(sa.text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        # No alembic_version at all: a schema built by create_all rather than by
        # a migration. Worth flagging, but it is not a version mismatch.
        return [_entry(WARN, componentType="datastore", output="no migration state recorded")]

    expected = _expected_revision()
    if expected is None:
        status, output = WARN, "migration head unreadable"
    elif applied == expected:
        status, output = PASS, None
    else:
        status, output = FAIL, f"database at {applied}, code expects {expected}"
    entry = _entry(status, componentType="datastore", observedValue=applied)
    if output:
        entry["output"] = output
    return [entry]


def _check_nightly_job(session: Session) -> list[dict]:
    """When the price cron last succeeded — the one signal that it still runs."""
    last_ok = session.exec(
        select(JobRun)
        .where(JobRun.job_name == "daily_price_update", JobRun.status == JobStatus.OK)
        .order_by(JobRun.started_at.desc())
    ).first()

    if last_ok is None:
        # No row yet is normal until the first night after deploying job_runs,
        # so this is a warning and never a failure.
        return [_entry(WARN, componentType="component", output="no successful run recorded")]

    age = _aged(last_ok.finished_at or last_ok.started_at)
    entry = _entry(
        FAIL if age > _CRON_STALE_AFTER else PASS,
        componentType="component",
        observedValue=(last_ok.finished_at or last_ok.started_at).isoformat(),
    )
    if last_ok.counters:
        entry["counters"] = last_ok.counters
    return [entry]


def _check_price_freshness(session: Session) -> list[dict]:
    """Age of the newest price per source, which is how providers are judged."""
    rows = session.exec(
        select(MarketAsset.asset_type, sa.func.max(MarketPriceHistory.price_date))
        .join(MarketPriceHistory, MarketPriceHistory.market_asset_id == MarketAsset.id)
        .group_by(MarketAsset.asset_type)
    ).all()

    newest_by_source: dict[str, date] = {}
    for asset_type, newest in rows:
        source = _SOURCE_OF.get(asset_type)
        if source is None or newest is None:
            continue
        if source not in newest_by_source or newest > newest_by_source[source]:
            newest_by_source[source] = newest

    entries = []
    for source in sorted(set(_SOURCE_OF.values())):
        newest = newest_by_source.get(source)
        if newest is None:
            entries.append(
                _entry(WARN, componentId=source, componentType="system", output="no price stored")
            )
            continue
        age = _aged(newest)
        status = (
            FAIL if age > _PRICE_STALE_AFTER else WARN if age > _PRICE_WARN_AFTER else PASS
        )
        entry = _entry(
            status,
            componentId=source,
            componentType="system",
            observedValue=age.days,
            observedUnit="days",
        )
        if status != PASS:
            entry["output"] = f"newest price {newest.isoformat()}"
        entries.append(entry)
    return entries


def _check_bank_sync(session: Session) -> list[dict]:
    """When any bank link last synced. Absent links are not a fault."""
    newest = session.exec(select(sa.func.max(BankAccountLink.last_synced_at))).one()
    if newest is None:
        return [_entry(PASS, componentType="component", output="no bank link configured")]
    age = _aged(newest)
    return [
        _entry(
            WARN if age > timedelta(days=2) else PASS,
            componentType="component",
            observedValue=newest.isoformat(),
        )
    ]


def _check_job_failures(session: Session) -> list[dict]:
    """Failed and stuck runs, the two things job_runs exists to surface."""
    since = _now() - timedelta(hours=24)
    failed = session.exec(
        select(sa.func.count())
        .select_from(JobRun)
        .where(JobRun.status == JobStatus.FAILED, JobRun.started_at >= since)
    ).one()
    stuck = session.exec(
        select(sa.func.count())
        .select_from(JobRun)
        .where(JobRun.status == JobStatus.RUNNING, JobRun.started_at < _now() - _RUN_STUCK_AFTER)
    ).one()

    entries = [
        _entry(
            WARN if failed else PASS,
            componentId="failures24h",
            componentType="component",
            observedValue=failed,
        )
    ]
    if stuck:
        # A run that never closed its row: the process died mid-job, and for a
        # history rebuild that means a curve left truncated.
        entries.append(
            _entry(WARN, componentId="stuck", componentType="component", observedValue=stuck)
        )
    return entries


_CHECKS = {
    "postgres:responseTime": _check_postgres,
    "alembic:revision": _check_migrations,
    "scheduler:lastSuccess": _check_nightly_job,
    "market:priceFreshness": _check_price_freshness,
    "banking:lastSync": _check_bank_sync,
    "jobs:runs": _check_job_failures,
}


def build_health_report(session: Session, release_id: str, service_id: str) -> tuple[dict, str]:
    """Assemble the report and the status it rolls up to.

    A check that raises becomes a failing entry rather than a 500: a health
    route that cannot answer is worse than one reporting a broken component.
    """
    checks: dict[str, list[dict]] = {}
    for key, probe in _CHECKS.items():
        # Each check runs in a savepoint: on Postgres a failed statement poisons
        # the transaction, and one broken probe must not take the others down.
        savepoint = session.begin_nested()
        try:
            checks[key] = probe(session)
            savepoint.commit()
        except Exception as exc:
            savepoint.rollback()
            checks[key] = [_entry(FAIL, output=f"{type(exc).__name__}: {exc}"[:200])]

    overall = PASS
    for entries in checks.values():
        for entry in entries:
            if _SEVERITY[entry["status"]] > _SEVERITY[overall]:
                overall = entry["status"]

    return {
        "status": overall,
        "releaseId": release_id,
        "serviceId": service_id,
        "description": "CapitalView API",
        "checks": checks,
    }, overall
