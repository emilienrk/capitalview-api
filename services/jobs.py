"""Cross-worker coordination for scheduled jobs.

The API runs under `uvicorn --workers 4` and each worker executes the lifespan,
so each one starts its own APScheduler: every nightly job fired four times.
Four times the CoinMarketCap credits, four times the Yahoo rate-limit pressure,
and a read-then-insert race in `check_pick_targets` able to send the same
"target reached" notification several times over.

A Postgres advisory lock is what makes the four schedulers agree without adding
any infrastructure: the first worker to reach the job takes it, the other three
find it held and return.
"""

import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from zlib import crc32

from sqlalchemy import text
from sqlmodel import Session

from database import get_engine

logger = logging.getLogger(__name__)

# An exception message is stored whole up to this length. Long enough for a
# stack-less driver error, short enough that nothing dumps a row into the table.
_MAX_ERROR_CHARS = 500

# Namespace shared by every advisory lock this app takes, so a key of ours can
# never collide with one taken by something else on the same database.
_LOCK_NAMESPACE = 0x4356  # "CV"


def _lock_key(name: str) -> int:
    """A stable key for a job name. Advisory keys are signed 32-bit."""
    value = crc32(name.encode())
    return value - 2**32 if value >= 2**31 else value


@contextmanager
def job_lock(name: str):
    """Hold a cluster-wide lock for *name*, yielding whether it was obtained.

    The lock sits on a connection of its own, held open for the whole job: an
    advisory lock belongs to its connection, and the job's own Session hands
    its connection back to the pool at every commit. Releasing is explicit
    because a session-level lock survives both commit and rollback; a worker
    that dies outright loses its connection, and Postgres frees the lock.
    """
    engine = get_engine()
    params = {"ns": _LOCK_NAMESPACE, "key": _lock_key(name)}
    with engine.connect() as conn:
        obtained = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:ns, :key)"), params).scalar()
        )
        if not obtained:
            logger.info(
                "job %s: skipped, held by another worker", name, extra={"pid": os.getpid()}
            )
            yield False
            return
        try:
            logger.info("job %s: started", name, extra={"pid": os.getpid()})
            yield True
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), params)


@contextmanager
def job_run(name: str, user_uuid: str | None = None):
    """Record one execution of *name*, yielding a dict to fill with counters.

    The "running" row is committed up front, so a job still in flight — or one
    that died mid-way and will never come back to close its row — is visible
    rather than merely absent.

    Its own Session, independent of the job's: the record has to survive a
    rollback of the work it describes.

    The `error` field is the one place an exception message is persisted. Values
    are encrypted before they ever reach the database, so a driver error carries
    ciphertext; still, a job must not raise an exception whose message quotes a
    decrypted amount.
    """
    from models import JobRun, JobStatus

    engine = get_engine()
    counters: dict = {}
    started = datetime.now(timezone.utc)

    with Session(engine) as session:
        record = JobRun(
            job_name=name, user_uuid=user_uuid, started_at=started, status=JobStatus.RUNNING
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        run_id = record.id

    def _close(status: str, error: str | None) -> None:
        with Session(engine) as session:
            row = session.get(JobRun, run_id)
            if row is None:  # purged under us; nothing to close
                return
            row.finished_at = datetime.now(timezone.utc)
            row.status = status
            row.counters = counters or None
            row.error = error
            session.add(row)
            session.commit()

    try:
        yield counters
    except Exception as exc:
        _close(JobStatus.FAILED, f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS])
        logger.exception("job %s: failed", name)
        raise
    else:
        _close(JobStatus.OK, None)
        logger.info("job %s: finished", name, extra={"counters": counters})


def single_run(name: str, fn: Callable[[], dict | None]) -> Callable[[], None]:
    """Wrap a scheduled job so exactly one worker runs it, and record the run.

    A job may return a dict of counters; it lands in `job_runs.counters`.
    """

    def runner() -> None:
        with job_lock(name) as obtained:
            if not obtained:
                return
            with job_run(name) as counters:
                counters.update(fn() or {})

    runner.__name__ = f"single_run:{name}"
    return runner
