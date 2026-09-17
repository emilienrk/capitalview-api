"""
Route tests for /banking/review-queue, /banking/ledger and
/banking/real-cashflow/current.
"""
import gzip

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from tests.services.test_banking_flows import USER, _link, _raw, _store


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid=USER, auth_salt="salt", username="t", email="t@test", password_hash="x"
    )
    app.dependency_overrides[get_master_key] = lambda: master_key
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed(session, master_key) -> None:
    _link(session, master_key, "current")
    _store(
        session, master_key, "current",
        _raw("400.00", "DBIT", "2025-03-05", ref="a", label="VIR INST ROUKINE EMILIEN"),
        _raw("19000.00", "DBIT", "2026-03-05", ref="b", label="VIR SEPA JEAN TIERS"),
        *[_raw("12.30", "DBIT", f"2026-02-{day:02d}", ref=f"card-{day}", label=f"CARTE {day:02d}/02/26 BOULANGERIE CB*08") for day in range(1, 28)],
    )


def test_the_review_queue_is_heaviest_first_and_narrows_on_a_year(client, session, master_key):
    _seed(session, master_key)

    body = client.get("/banking/review-queue").json()
    assert [(q["kind"], q["transaction"]["label"], float(q["amount"])) for q in body["questions"]] == [
        ("flow", "VIR SEPA JEAN TIERS", 19000), ("flow", "VIR INST ROUKINE EMILIEN", 400),
    ]
    assert [q["transaction"]["label"] for q in client.get("/banking/review-queue?year=2025").json()["questions"]] == [
        "VIR INST ROUKINE EMILIEN",
    ]


def test_the_ledger_answers_not_modified_until_something_changes(client, session, master_key):
    _seed(session, master_key)

    first = client.get("/banking/ledger")
    etag = first.headers["etag"]
    assert (first.status_code, len(first.json()["rows"])) == (200, 29)
    assert first.headers["cache-control"] == "private, no-cache"

    again = client.get("/banking/ledger", headers={"If-None-Match": etag})
    assert (again.status_code, again.content, again.headers["etag"]) == (304, b"", etag)

    _store(session, master_key, "current", _raw("4.00", "DBIT", "2026-03-06", ref="c", label="CARTE BOULANGERIE CB*08"))
    assert client.get("/banking/ledger", headers={"If-None-Match": etag}).status_code == 200


def test_the_ledger_is_compressed_when_the_reader_accepts_it(client, session, master_key):
    _seed(session, master_key)
    response = client.get("/banking/ledger", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    # The client decompressed it: the body is still the ledger.
    assert len(response.json()["rows"]) == 29


def test_the_month_in_progress_has_its_pace(client, session, master_key):
    _seed(session, master_key)
    body = client.get("/banking/real-cashflow/current").json()
    assert {"period", "spent_to_date", "median_to_date", "curve"} <= body.keys()
