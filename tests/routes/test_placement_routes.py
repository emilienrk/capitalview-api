"""Tests for /placements routes."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from main import app
from models.enums import PlacementType
from models.placement import PlacementEntry
from models.user import User

CURRENT_USER = {"uuid": "user_1"}


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(
            uuid=CURRENT_USER["uuid"], auth_salt="salt", username="test", email="t@test", password_hash="x"
        )

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_master_key] = _get_master_key

    # The rebuild opens its own engine: the routes only have to schedule it.
    with patch("routes.placement.rebuild_account_history_from_date") as rebuild:
        yield rebuild

    CURRENT_USER["uuid"] = "user_1"
    app.dependency_overrides.clear()


def _placement(client: TestClient, **overrides) -> str:
    payload = {"name": "Linxea Spirit", "placement_type": "AV", "opened_at": "2020-03-15", **overrides}
    r = client.post("/placements", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _entry(client: TestClient, placement_id: str, kind: str, day: str, amount: str):
    return client.post(
        f"/placements/{placement_id}/entries",
        json={"type": kind, "amount": amount, "occurred_at": day},
    )


def test_a_new_placement_is_worth_nothing_and_asks_for_no_statement():
    client = TestClient(app)
    placement_id = _placement(client)

    r = client.get(f"/placements/{placement_id}")

    assert r.status_code == 200
    data = r.json()
    assert data["placement_type"] == "AV"
    assert float(data["current_value"]) == 0
    assert data["gain"] is None
    assert data["is_stale"] is False
    assert data["tax_anniversary_date"] == "2028-03-15"


def test_the_summary_reads_the_value_from_statements_and_flows():
    client = TestClient(app)
    placement_id = _placement(client)
    assert _entry(client, placement_id, "DEPOSIT", "2023-01-01", "1000").status_code == 201
    assert _entry(client, placement_id, "VALUATION", "2023-12-31", "1030").status_code == 201
    assert _entry(client, placement_id, "DEPOSIT", "2024-01-01", "500").status_code == 201

    data = client.get("/placements").json()

    placement = data["accounts"][0]
    assert float(placement["current_value"]) == 1530
    assert float(placement["net_invested"]) == 1500
    assert float(placement["gain"]) == 30
    assert placement["last_valuation_date"] == "2023-12-31"
    assert placement["is_stale"] is True
    assert float(data["total_value"]) == 1530


def test_money_in_without_any_statement_is_flagged_stale():
    client = TestClient(app)
    placement_id = _placement(client)
    _entry(client, placement_id, "DEPOSIT", (date.today() - timedelta(days=3)).isoformat(), "100")

    placement = client.get(f"/placements/{placement_id}").json()

    assert placement["is_stale"] is True
    assert placement["gain"] is None


@pytest.mark.parametrize("placement_type", [t.value for t in PlacementType if t != PlacementType.AV])
def test_only_an_av_has_an_eight_year_anniversary(placement_type):
    client = TestClient(app)
    placement_id = _placement(client, placement_type=placement_type)

    data = client.get(f"/placements/{placement_id}").json()

    assert data["placement_type"] == placement_type
    assert data["tax_anniversary_date"] is None


def test_a_scpi_is_valued_from_its_statements_like_any_placement():
    client = TestClient(app)
    placement_id = _placement(client, name="SCPI Épargne Pierre", placement_type="SCPI")
    assert _entry(client, placement_id, "DEPOSIT", "2023-01-01", "10000").status_code == 201
    assert _entry(client, placement_id, "VALUATION", "2023-12-31", "10450").status_code == 201

    placement = client.get("/placements").json()["accounts"][0]

    assert placement["placement_type"] == "SCPI"
    assert float(placement["current_value"]) == 10450
    assert float(placement["gain"]) == 450
    assert placement["tax_anniversary_date"] is None


def test_turning_an_av_into_a_scpi_drops_its_anniversary():
    client = TestClient(app)
    placement_id = _placement(client)

    r = client.put(f"/placements/{placement_id}", json={"placement_type": "SCPI"})

    assert r.status_code == 200
    assert r.json()["placement_type"] == "SCPI"
    assert r.json()["tax_anniversary_date"] is None


def test_a_deposit_of_zero_is_refused_but_a_zero_balance_is_not():
    client = TestClient(app)
    placement_id = _placement(client)

    assert _entry(client, placement_id, "DEPOSIT", "2024-01-01", "0").status_code == 422
    assert _entry(client, placement_id, "DEPOSIT", "2024-01-01", "100").status_code == 201
    assert _entry(client, placement_id, "VALUATION", "2024-06-01", "0").status_code == 201


def test_a_balance_needs_a_deposit_on_or_before_it():
    """Without one, the whole balance would read as gain."""
    client = TestClient(app)
    placement_id = _placement(client)

    refused = _entry(client, placement_id, "VALUATION", "2024-06-01", "10000")
    assert refused.status_code == 400
    assert "versement" in refused.json()["detail"]

    _entry(client, placement_id, "DEPOSIT", "2024-07-01", "9000")
    assert _entry(client, placement_id, "VALUATION", "2024-06-01", "10000").status_code == 400
    assert _entry(client, placement_id, "VALUATION", "2024-07-01", "9000").status_code == 201


def test_no_edit_or_deletion_may_leave_a_balance_without_a_deposit_before_it():
    client = TestClient(app)
    placement_id = _placement(client)
    deposit_id = _entry(client, placement_id, "DEPOSIT", "2024-01-01", "1000").json()["id"]
    _entry(client, placement_id, "VALUATION", "2024-06-01", "1020")

    moved = client.put(
        f"/placements/{placement_id}/entries/{deposit_id}", json={"occurred_at": "2024-09-01"}
    )
    assert moved.status_code == 400
    assert client.delete(f"/placements/{placement_id}/entries/{deposit_id}").status_code == 400

    assert len(client.get(f"/placements/{placement_id}/entries").json()) == 2


def test_adding_an_entry_rebuilds_history_from_the_placement_start(_override_deps):
    client = TestClient(app)
    placement_id = _placement(client, opened_at=None)
    _entry(client, placement_id, "DEPOSIT", "2024-05-01", "1000")
    _override_deps.reset_mock()

    _entry(client, placement_id, "VALUATION", "2024-12-31", "1040")

    _override_deps.assert_called_once()
    assert _override_deps.call_args.args[2] == date(2024, 5, 1)


def test_entries_can_be_edited_and_deleted():
    client = TestClient(app)
    placement_id = _placement(client)
    entry_id = _entry(client, placement_id, "DEPOSIT", "2024-01-01", "1000").json()["id"]

    r = client.put(f"/placements/{placement_id}/entries/{entry_id}", json={"amount": "1200"})
    assert r.status_code == 200
    assert float(r.json()["amount"]) == 1200
    assert r.json()["type"] == "DEPOSIT"

    assert client.put(
        f"/placements/{placement_id}/entries/{entry_id}", json={"amount": "0"}
    ).status_code == 400

    assert client.delete(f"/placements/{placement_id}/entries/{entry_id}").status_code == 204
    assert client.get(f"/placements/{placement_id}/entries").json() == []


def test_updating_a_placement_can_clear_its_optional_fields():
    client = TestClient(app)
    placement_id = _placement(client, institution_name="Spirica", expected_return_rate="0.03")

    r = client.put(
        f"/placements/{placement_id}",
        json={"name": "Spirit", "institution_name": None, "expected_return_rate": None},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Spirit"
    assert data["institution_name"] is None
    assert data["expected_return_rate"] is None
    assert data["opened_at"] == "2020-03-15"


def test_deleting_a_placement_takes_its_entries_with_it(session):
    client = TestClient(app)
    placement_id = _placement(client)
    _entry(client, placement_id, "DEPOSIT", "2024-01-01", "1000")

    assert client.delete(f"/placements/{placement_id}").status_code == 204

    assert client.get(f"/placements/{placement_id}").status_code == 404
    assert session.exec(select(PlacementEntry)).all() == []


def test_another_users_placement_is_out_of_reach():
    client = TestClient(app)
    placement_id = _placement(client)

    CURRENT_USER["uuid"] = "user_2"

    assert client.get(f"/placements/{placement_id}").status_code == 404
    assert _entry(client, placement_id, "DEPOSIT", "2024-01-01", "10").status_code == 404
    assert client.delete(f"/placements/{placement_id}").status_code == 404
    assert client.get("/placements").json()["accounts"] == []
