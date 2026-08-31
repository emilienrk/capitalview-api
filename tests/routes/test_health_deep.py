"""The deep health report the dashboard reads.

Asserts the contract (draft-inadarei-api-health-check), the status roll-up, the
token gate, and the one rule that matters most here: nothing decrypted ever
reaches the document.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlmodel import Session

import main
from models import JobRun, JobStatus, MarketAsset, MarketPriceHistory
from models.enums import AssetType
from services import health


@pytest.fixture
def client(session, monkeypatch):
    """A bare app carrying just the deep route, bound to the test session."""
    app = FastAPI()
    app.get("/health/deep")(main.health_deep)
    app.dependency_overrides[main.get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def open_route(monkeypatch):
    """No token and not production: the route is open, as it is locally."""
    monkeypatch.setattr(main.settings, "health_token", "")
    monkeypatch.setattr(main.settings, "environment", "development")


def _seed_price(session, asset_type: AssetType, price_date: date, symbol: str):
    asset = MarketAsset(asset_key=symbol, symbol=symbol, asset_type=asset_type)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    session.add(
        MarketPriceHistory(market_asset_id=asset.id, price=1, price_date=price_date)
    )
    session.commit()


def _seed_run(session, name: str, status: str, started: datetime, counters=None):
    session.add(
        JobRun(
            job_name=name,
            started_at=started,
            finished_at=started if status != JobStatus.RUNNING else None,
            status=status,
            counters=counters,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_the_document_follows_the_health_json_contract(client):
    response = client.get("/health/deep")

    assert response.headers["content-type"].startswith("application/health+json")
    body = response.json()
    assert body["status"] in ("pass", "warn", "fail")
    assert body["serviceId"] == "capitalview-api"
    assert "releaseId" in body
    for key, entries in body["checks"].items():
        assert ":" in key, f"{key} must be componentName:measurementName"
        assert isinstance(entries, list)
        for entry in entries:
            assert entry["status"] in ("pass", "warn", "fail")
            assert "time" in entry


def test_every_expected_component_is_reported(client):
    checks = client.get("/health/deep").json()["checks"]

    assert set(checks) == {
        "postgres:responseTime",
        "alembic:revision",
        "scheduler:lastSuccess",
        "market:priceFreshness",
        "banking:lastSync",
        "jobs:runs",
    }


# ---------------------------------------------------------------------------
# Status roll-up
# ---------------------------------------------------------------------------


def test_a_warning_answers_200_so_a_monitor_does_not_page(client, session):
    _seed_price(session, AssetType.STOCK, date.today() - timedelta(days=2), "AAA")

    response = client.get("/health/deep")

    assert response.status_code == 200
    assert response.json()["status"] == "warn"


def test_a_stale_provider_is_reported_against_its_source(client, session):
    _seed_price(session, AssetType.CRYPTO, date.today() - timedelta(days=10), "BTC")
    _seed_price(session, AssetType.STOCK, date.today(), "AAA")

    entries = client.get("/health/deep").json()["checks"]["market:priceFreshness"]
    by_source = {e["componentId"]: e for e in entries}

    assert by_source["coinmarketcap"]["status"] == "fail"
    assert by_source["coinmarketcap"]["observedValue"] == 10
    assert by_source["yahoo"]["status"] == "pass"


def test_a_failing_component_answers_503(client, session):
    _seed_price(session, AssetType.CRYPTO, date.today() - timedelta(days=10), "BTC")

    response = client.get("/health/deep")

    assert response.status_code == 503
    assert response.json()["status"] == "fail"


def test_the_nightly_job_reports_its_counters(client, session):
    _seed_run(
        session,
        "daily_price_update",
        JobStatus.OK,
        datetime.now(timezone.utc),
        counters={"prices": 847},
    )

    (entry,) = client.get("/health/deep").json()["checks"]["scheduler:lastSuccess"]

    assert entry["status"] == "pass"
    assert entry["counters"] == {"prices": 847}


def test_a_cron_that_stopped_running_fails(client, session):
    _seed_run(
        session,
        "daily_price_update",
        JobStatus.OK,
        datetime.now(timezone.utc) - timedelta(days=5),
    )

    (entry,) = client.get("/health/deep").json()["checks"]["scheduler:lastSuccess"]

    assert entry["status"] == "fail"


def test_a_run_stuck_in_running_is_surfaced(client, session):
    """The rebuild that died between its DELETE and its recomputation."""
    _seed_run(
        session,
        "rebuild_account_history",
        JobStatus.RUNNING,
        datetime.now(timezone.utc) - timedelta(hours=12),
    )

    entries = client.get("/health/deep").json()["checks"]["jobs:runs"]
    stuck = [e for e in entries if e.get("componentId") == "stuck"]

    assert stuck and stuck[0]["observedValue"] == 1


def test_a_broken_check_degrades_instead_of_500ing(client, monkeypatch):
    def _boom(session):
        raise RuntimeError("column gone")

    monkeypatch.setitem(health._CHECKS, "banking:lastSync", _boom)

    response = client.get("/health/deep")

    assert response.status_code == 503
    (entry,) = response.json()["checks"]["banking:lastSync"]
    assert entry["status"] == "fail"
    assert "RuntimeError" in entry["output"]


# ---------------------------------------------------------------------------
# Access and privacy
# ---------------------------------------------------------------------------


def test_a_configured_token_is_required(client, monkeypatch):
    monkeypatch.setattr(main.settings, "health_token", "s3cret")

    assert client.get("/health/deep").status_code == 401
    assert client.get("/health/deep", headers={"X-Health-Token": "wrong"}).status_code == 401
    assert client.get("/health/deep", headers={"X-Health-Token": "s3cret"}).status_code == 200


def test_production_without_a_token_hides_the_route(client, monkeypatch):
    monkeypatch.setattr(main.settings, "health_token", "")
    monkeypatch.setattr(main.settings, "environment", "production")

    assert client.get("/health/deep").status_code == 404


def test_the_report_carries_no_user_data(client, session):
    """Identifiers, counters, dates and durations only — never a decrypted value."""
    _seed_run(
        session,
        "rebuild_account_history",
        JobStatus.FAILED,
        datetime.now(timezone.utc),
    )
    _seed_price(session, AssetType.STOCK, date.today(), "AAA")

    body = client.get("/health/deep").text

    # The rebuild is per-user; its uuid must not travel with the report.
    assert "user_uuid" not in body
    for forbidden in ("_enc", "balance", "amount", "master_key"):
        assert forbidden not in body
