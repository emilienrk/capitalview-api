"""The advisory lock that keeps a scheduled job to one worker.

The lock itself is a Postgres primitive the SQLite test database has no
equivalent for, so the connection is stubbed: what is asserted here is the
protocol — try-lock, run or skip, always unlock.
"""

from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from services import jobs


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    """Records the statements issued and answers the try-lock with *obtained*."""

    def __init__(self, obtained: bool):
        self._obtained = obtained
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self._obtained)
        return _FakeResult(True)


@pytest.fixture
def fake_engine(monkeypatch):
    """Patch services.jobs.get_engine, returning the connection it hands out.

    `job_run` is neutralised alongside: these tests are about the lock protocol,
    and the execution record has its own tests further down.
    """

    @contextmanager
    def _no_record(name, user_uuid=None):
        yield {}

    def _install(obtained: bool) -> _FakeConnection:
        conn = _FakeConnection(obtained)

        @contextmanager
        def _connect():
            yield conn

        monkeypatch.setattr(jobs, "get_engine", lambda: type("E", (), {"connect": staticmethod(_connect)}))
        monkeypatch.setattr(jobs, "job_run", _no_record)
        return conn

    return _install


def test_lock_key_fits_a_signed_int4(fake_engine):
    """Postgres rejects an advisory key outside int4."""
    for name in ("daily_price_update", "daily_bank_consent_check", "rebuild_account_history"):
        assert -(2**31) <= jobs._lock_key(name) < 2**31


def test_lock_key_is_stable_and_distinct(fake_engine):
    assert jobs._lock_key("daily_price_update") == jobs._lock_key("daily_price_update")
    assert jobs._lock_key("daily_price_update") != jobs._lock_key("daily_bank_consent_check")


def test_the_worker_holding_the_lock_runs_the_job(fake_engine):
    conn = fake_engine(obtained=True)
    calls = []

    jobs.single_run("daily_price_update", lambda: calls.append(1))()

    assert calls == [1]
    assert any("pg_advisory_unlock" in s for s in conn.statements)


def test_a_worker_without_the_lock_skips_the_job(fake_engine):
    conn = fake_engine(obtained=False)
    calls = []

    jobs.single_run("daily_price_update", lambda: calls.append(1))()

    assert calls == []
    # Nothing was taken, so nothing is released.
    assert not any("pg_advisory_unlock" in s for s in conn.statements)


def test_the_lock_is_released_when_the_job_raises(fake_engine):
    conn = fake_engine(obtained=True)

    def _boom():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        jobs.single_run("daily_price_update", _boom)()

    assert any("pg_advisory_unlock" in s for s in conn.statements)


# ---------------------------------------------------------------------------
# job_run — the execution record (point 6)
# ---------------------------------------------------------------------------


@pytest.fixture
def recording_engine(engine, monkeypatch):
    """Point services.jobs at the test database so job_runs rows are real.

    `job_run` commits on a Session of its own — that is the point of it — so its
    rows escape the rollback the `session` fixture relies on and have to be
    cleared by hand.
    """
    from models import JobRun

    def _clear():
        with Session(engine) as cleanup:
            cleanup.exec(sa.delete(JobRun))
            cleanup.commit()

    _clear()
    monkeypatch.setattr(jobs, "get_engine", lambda: engine)
    yield engine
    _clear()


def _runs(session):
    from models import JobRun

    return list(session.exec(select(JobRun).order_by(JobRun.id)).all())


def test_a_successful_run_is_recorded_with_its_counters(recording_engine, session):
    from models import JobStatus

    with jobs.job_run("nightly_prices") as counters:
        counters["prices"] = 847

    (run,) = _runs(session)
    assert run.job_name == "nightly_prices"
    assert run.status == JobStatus.OK
    assert run.counters == {"prices": 847}
    assert run.finished_at is not None
    assert run.error is None


def test_a_failed_run_keeps_the_reason_and_re_raises(recording_engine, session):
    from models import JobStatus

    with pytest.raises(RuntimeError):
        with jobs.job_run("rebuild_account_history", user_uuid="user-1"):
            raise RuntimeError("provider down")

    (run,) = _runs(session)
    assert run.status == JobStatus.FAILED
    assert run.user_uuid == "user-1"
    assert "RuntimeError: provider down" in run.error


def test_a_run_that_never_finishes_stays_visible_as_running(recording_engine, session):
    """The row is committed up front, so a job that dies mid-way is not simply absent."""
    from models import JobStatus

    cm = jobs.job_run("rebuild_account_history", user_uuid="user-2")
    cm.__enter__()  # entered, never exited — the process died here

    (run,) = _runs(session)
    assert run.status == JobStatus.RUNNING
    assert run.finished_at is None


def test_the_error_message_is_truncated(recording_engine, session):
    with pytest.raises(ValueError):
        with jobs.job_run("nightly_prices"):
            raise ValueError("x" * 5000)

    (run,) = _runs(session)
    assert len(run.error) == jobs._MAX_ERROR_CHARS
