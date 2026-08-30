"""The advisory lock that keeps a scheduled job to one worker.

The lock itself is a Postgres primitive the SQLite test database has no
equivalent for, so the connection is stubbed: what is asserted here is the
protocol — try-lock, run or skip, always unlock.
"""

from contextlib import contextmanager

import pytest

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
    """Patch services.jobs.get_engine, returning the connection it hands out."""

    def _install(obtained: bool) -> _FakeConnection:
        conn = _FakeConnection(obtained)

        @contextmanager
        def _connect():
            yield conn

        monkeypatch.setattr(jobs, "get_engine", lambda: type("E", (), {"connect": staticmethod(_connect)}))
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
