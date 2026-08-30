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
from zlib import crc32

from sqlalchemy import text

from database import get_engine

logger = logging.getLogger(__name__)

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


def single_run(name: str, fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a scheduled job so exactly one worker runs it."""

    def runner() -> None:
        with job_lock(name) as obtained:
            if obtained:
                fn()

    runner.__name__ = f"single_run:{name}"
    return runner
