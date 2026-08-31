"""A rate limiter the four workers share.

The previous one was a module-level dict behind an asyncio lock, so each uvicorn
worker counted on its own: the five-logins-a-minute limit really allowed up to
twenty, and a redeploy wiped the counters. Postgres holds the window instead —
no Redis to run, back up and watch for this one use.

Its own Session, never the request's: the limiter has to commit the moment it
counts a hit, and committing the caller's session would flush half-finished
route work with it.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import text
from sqlmodel import Session, select

from config import get_settings
from database import get_engine
from models import RateLimitHit

logger = logging.getLogger(__name__)

# Buckets are dropped once nothing has hit them for a day; the per-call purge
# only ever touches the bucket in front of it, so abandoned ones need a sweep.
_SWEEP_OLDER_THAN = timedelta(days=1)


def bucket_id(ip: str, action: str) -> str:
    """An opaque, stable id for "<ip>:<action>".

    Keyed with SECRET_KEY so the table cannot be walked back to the addresses
    that produced it. Equality is the only operation the limiter needs.
    """
    digest = hmac.new(
        get_settings().secret_key.encode(), f"{ip}:{action}".encode(), hashlib.sha256
    )
    return digest.hexdigest()


def _serialise(session: Session, bucket: str) -> None:
    """Hold the bucket for the rest of the transaction, on Postgres.

    Counting and inserting are two statements, and four workers reaching them
    together would each read the same count and each insert — the very leak this
    module exists to close. A transaction-scoped advisory lock closes it; it is
    released by the commit. SQLite (tests) serialises writes anyway.
    """
    if session.bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(bytes.fromhex(bucket)[:8], "big", signed=True)
    session.exec(text("SELECT pg_advisory_xact_lock(:key)"), params={"key": key})


def check_and_record(bucket: str, max_calls: int, window_seconds: int) -> bool:
    """Count one request against *bucket*. False when the window is already full.

    A refused request is not recorded, so the window slides on accepted traffic
    rather than extending itself every time a caller retries.
    """
    engine = get_engine()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)

    with Session(engine) as session:
        _serialise(session, bucket)
        session.exec(
            sa.delete(RateLimitHit).where(
                RateLimitHit.bucket == bucket, RateLimitHit.hit_at < cutoff
            )
        )
        hits = session.exec(
            select(sa.func.count()).select_from(RateLimitHit).where(RateLimitHit.bucket == bucket)
        ).one()

        if hits >= max_calls:
            session.commit()  # keep the purge, refuse the call
            return False

        session.add(RateLimitHit(bucket=bucket, hit_at=now))
        session.commit()
        return True


def purge_rate_limit_hits() -> dict:
    """Drop hits no live window can still contain. Runs as a daily job."""
    engine = get_engine()
    cutoff = datetime.now(timezone.utc) - _SWEEP_OLDER_THAN
    with Session(engine) as session:
        deleted = session.exec(sa.delete(RateLimitHit).where(RateLimitHit.hit_at < cutoff))
        session.commit()
        return {"deleted": deleted.rowcount}
