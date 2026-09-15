"""
Route tests for the categories, their rules, filing an operation and the queue
of operations left to file.
"""
import pytest
from fastapi.testclient import TestClient

from dtos.banking import CategoryNature, CategoryOrigin
from main import app
from models.user import User
from services.banking.categories import create_category
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


LABELS = (
    "CARTE 01/03/26 CARREFOUR ANNECY CB*08",
    "CARTE 08/03/26 CARREFOUR CITY LYON CB*08",
    "CARTE 15/03/26 CARREFOUR ANNECY CB*08",
    "CARTE 19/03/26 PHARMACIE DU LAC CB*08",
)


def _seed(session, master_key) -> None:
    _link(session, master_key, "current")
    _store(session, master_key, "current", *[
        _raw("10.00", "DBIT", f"2026-03-{n + 1:02d}", ref=f"r{n}", label=label) for n, label in enumerate(LABELS)
    ])


def _month(client: TestClient) -> dict[str, dict]:
    response = client.get("/banking/transactions?period=2026-03")
    assert response.status_code == 200
    return {tx["label"]: tx for tx in response.json()["transactions"]}


def _create(client: TestClient, name: str = "Courses", nature: str = "EXPENSE") -> dict:
    response = client.post("/banking/categories", json={"name": name, "nature": nature})
    assert response.status_code == 201
    return response.json()


def test_correcting_one_operation_files_every_similar_one(client, session, master_key):
    _seed(session, master_key)
    courses = _create(client)
    target = _month(client)[LABELS[0]]

    response = client.put(
        f"/banking/transactions/{target['id']}/category",
        json={"category_id": courses["id"], "apply_to_similar": True, "tokens": ["carrefour"]},
    )

    assert response.status_code == 200
    assert response.json()["filed_count"] == 3
    month = _month(client)
    assert [month[label]["category_name"] for label in LABELS] == ["Courses", "Courses", "Courses", None]
    assert {month[label]["category_source"] for label in LABELS[:3]} == {"user_rule"}
    assert [c["rule_count"] for c in client.get("/banking/categories").json()] == [1]
    [rule] = client.get("/banking/category-rules").json()
    assert (rule["tokens"], rule["category_name"], rule["source"]) == (["carrefour"], "Courses", "user")
    assert [g["label"] for g in client.get("/banking/uncategorized").json()["groups"]] == [LABELS[3]]


def test_deleting_the_rule_unfiles_them(client, session, master_key):
    _seed(session, master_key)
    courses = _create(client)
    target = _month(client)[LABELS[0]]
    client.put(
        f"/banking/transactions/{target['id']}/category",
        json={"category_id": courses["id"], "apply_to_similar": True, "tokens": ["carrefour"]},
    )
    [rule] = client.get("/banking/category-rules").json()

    assert client.delete(f"/banking/category-rules/{rule['id']}").status_code == 204
    assert {tx["category_id"] for tx in _month(client).values()} == {None}


def test_a_duplicate_name_is_a_409(client, session, master_key):
    _create(client, "Épargne", "SAVING")
    response = client.post("/banking/categories", json={"name": "epargne", "nature": "SAVING"})
    assert response.status_code == 409


def test_a_create_without_nature_is_a_422(client):
    assert client.post("/banking/categories", json={"name": "Courses"}).status_code == 422


def test_a_category_can_be_renamed_and_its_nature_changed(client):
    livret = _create(client, "Livret")
    response = client.patch(f"/banking/categories/{livret['id']}", json={"name": "Mis de côté", "nature": "SAVING"})
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["nature"]) == ("Mis de côté", "SAVING")


def test_another_user_s_category_is_a_404(client, session, master_key):
    _seed(session, master_key)
    theirs = create_category(session, "someone_else", master_key, "Voyages", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    target = _month(client)[LABELS[0]]

    assert client.patch(f"/banking/categories/{theirs.uuid}", json={}).status_code == 404
    assert client.patch(f"/banking/categories/{theirs.uuid}", json={"name": "Mine"}).status_code == 404
    assert client.delete(f"/banking/categories/{theirs.uuid}").status_code == 404
    response = client.put(f"/banking/transactions/{target['id']}/category", json={"category_id": theirs.uuid})
    assert response.status_code == 404


def test_an_unknown_operation_is_a_404(client, session, master_key):
    courses = _create(client)
    response = client.put("/banking/transactions/nope/category", json={"category_id": courses["id"]})
    assert response.status_code == 404


def test_an_unknown_rule_is_a_404(client):
    assert client.delete("/banking/category-rules/nope").status_code == 404


@pytest.mark.parametrize("tokens", [[], ["CB*08", "carte"]])
def test_an_empty_or_general_rule_is_a_400(client, session, master_key, tokens):
    _seed(session, master_key)
    # Enough signatures for "carte" and "cb" to be common, "carrefour" not.
    _store(session, master_key, "current", *[
        _raw("1.00", "DBIT", "2026-03-20", ref=f"m{n}", label=f"CARTE 20/03/26 MARCHAND{chr(65 + n)} CB*08")
        for n in range(20)
    ])
    courses = _create(client)
    target = _month(client)[LABELS[0]]
    response = client.put(
        f"/banking/transactions/{target['id']}/category",
        json={"category_id": courses["id"], "apply_to_similar": True, "tokens": tokens},
    )
    assert response.status_code == 400


def test_a_single_operation_can_be_filed_as_none(client, session, master_key):
    _seed(session, master_key)
    target = _month(client)[LABELS[3]]
    response = client.put(f"/banking/transactions/{target['id']}/category", json={"category_id": None})
    assert response.status_code == 200
    assert (response.json()["filed_count"], response.json()["transaction"]["category_source"]) == (0, "manual")


def test_the_available_categories_follow_the_ai_setting(client, session, master_key):
    _create(client, "Courses")
    create_category(session, USER, master_key, "Loyer", CategoryNature.EXPENSE, CategoryOrigin.CASHFLOW)

    assert [c["name"] for c in client.get("/banking/categories/available?scope=bank").json()] == ["Courses", "Loyer"]
    assert client.put("/settings", json={"ai_categorization_enabled": True}).status_code == 200
    assert [c["name"] for c in client.get("/banking/categories/available?scope=bank").json()] == ["Courses"]
    assert [c["name"] for c in client.get("/banking/categories/available?scope=planned").json()] == ["Loyer"]


def test_a_cashflow_category_is_materialised_on_demand(client, session, master_key):
    created = client.post(
        "/cashflow",
        json={"name": "Paie", "flow_type": "INFLOW", "category": "Salaire", "amount": "2000",
              "frequency": "MONTHLY", "transaction_date": "2026-01-01"},
    )
    assert created.status_code == 201
    response = client.post("/banking/categories", json={"name": "salaire", "from_cashflow": True})
    assert response.status_code == 201
    assert (response.json()["name"], response.json()["nature"], response.json()["origin"]) == ("Salaire", "INCOME", "cashflow")

    assert client.post("/banking/categories", json={"name": "Inconnue", "from_cashflow": True}).status_code == 404
