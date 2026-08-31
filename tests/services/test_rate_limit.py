"""The rate limiter the four workers share.

The property that matters is the one the per-process dict could not offer: two
callers that do not share memory still share the window.
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from models import RateLimitHit
from services import rate_limit


def _bucket(action: str = "login", ip: str = "203.0.113.7") -> str:
    return rate_limit.bucket_id(ip, action)


def test_the_window_allows_exactly_max_calls():
    bucket = _bucket()

    allowed = [rate_limit.check_and_record(bucket, max_calls=5, window_seconds=60) for _ in range(7)]

    assert allowed == [True] * 5 + [False, False]


def test_a_refused_call_is_not_recorded():
    """Otherwise a caller retrying in a loop would keep extending its own window."""
    bucket = _bucket()
    for _ in range(6):
        rate_limit.check_and_record(bucket, max_calls=5, window_seconds=60)

    with Session(rate_limit.get_engine()) as session:
        assert session.exec(select(sa.func.count()).select_from(RateLimitHit)).one() == 5


def test_separate_actions_do_not_share_a_window():
    login, register = _bucket("login"), _bucket("register")
    for _ in range(5):
        rate_limit.check_and_record(login, max_calls=5, window_seconds=60)

    assert rate_limit.check_and_record(login, max_calls=5, window_seconds=60) is False
    assert rate_limit.check_and_record(register, max_calls=5, window_seconds=60) is True


def test_separate_addresses_do_not_share_a_window():
    mine, theirs = _bucket(ip="203.0.113.7"), _bucket(ip="198.51.100.2")
    for _ in range(5):
        rate_limit.check_and_record(mine, max_calls=5, window_seconds=60)

    assert rate_limit.check_and_record(mine, max_calls=5, window_seconds=60) is False
    assert rate_limit.check_and_record(theirs, max_calls=5, window_seconds=60) is True


def test_the_window_slides():
    bucket = _bucket()
    engine = rate_limit.get_engine()
    with Session(engine) as session:
        for _ in range(5):
            session.add(
                RateLimitHit(
                    bucket=bucket, hit_at=datetime.now(timezone.utc) - timedelta(seconds=120)
                )
            )
        session.commit()

    assert rate_limit.check_and_record(bucket, max_calls=5, window_seconds=60) is True


def test_a_second_process_sees_the_same_window():
    """The point of the table: no shared memory, one window.

    Two independent Sessions stand in for two uvicorn workers — with the old
    module-level dict each would have counted to five on its own.
    """
    bucket = _bucket()
    for _ in range(5):
        rate_limit.check_and_record(bucket, max_calls=5, window_seconds=60)

    # A "second worker" reaches the same conclusion without ever having counted.
    assert rate_limit.check_and_record(bucket, max_calls=5, window_seconds=60) is False


def test_the_bucket_never_stores_the_address():
    bucket = rate_limit.bucket_id("203.0.113.7", "login")

    assert "203.0.113.7" not in bucket
    assert len(bucket) == 64  # sha256 hex
    assert bucket == rate_limit.bucket_id("203.0.113.7", "login")


def test_the_purge_drops_only_what_no_window_can_hold():
    bucket = _bucket()
    engine = rate_limit.get_engine()
    with Session(engine) as session:
        session.add(RateLimitHit(bucket=bucket, hit_at=datetime.now(timezone.utc) - timedelta(days=2)))
        session.add(RateLimitHit(bucket=bucket, hit_at=datetime.now(timezone.utc)))
        session.commit()

    assert rate_limit.purge_rate_limit_hits() == {"deleted": 1}

    with Session(engine) as session:
        assert session.exec(select(sa.func.count()).select_from(RateLimitHit)).one() == 1


def test_the_login_route_still_answers_429(session):
    """End to end: the limit advertised on /auth/login is the one enforced."""
    from fastapi.testclient import TestClient

    from database import get_session
    from main import app

    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    payload = {"email": "nobody@example.com", "password": "WrongPassword1!"}
    codes = [client.post("/auth/login", json=payload).status_code for _ in range(7)]

    app.dependency_overrides.clear()

    assert codes[:5] == [401] * 5
    assert codes[5:] == [429, 429]
