"""
Route tests for the Enable Banking linking flow (spec §C).

No real network: the Enable Banking client is always a FakeClient double,
monkeypatched onto services.banking.linking.build_client (the name as
imported there, per the repo's own pg_insert-patching convention — see
tests/services/test_bank.py).
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from config import get_settings
from main import app
from models.banking import BankAccountLink, BankAuthorization, BankSession
from models.user import User
from services.banking.errors import AuthorizationInvalidError
from services.banking.linking import handle_callback, start_authorization_flow
from services.encryption import decrypt_data, encrypt_data, hash_index

USER_UUID = "user_1"


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(uuid=USER_UUID, auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    from services.auth import get_current_user, get_master_key
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_master_key] = _get_master_key

    from tests.conftest import opt_into_open_banking
    opt_into_open_banking(session, USER_UUID, master_key)

    yield

    app.dependency_overrides.clear()


class FakeClient:
    """Records calls, returns/raises canned responses. Never touches the network."""

    def __init__(self, **canned):
        self.canned = canned
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def _resolve(self, name, *args):
        self.calls.append((name, *args))
        result = self.canned[name]
        if isinstance(result, Exception):
            raise result
        return result

    def get_application(self):
        return self._resolve("get_application")

    def list_aspsps(self, country):
        return self._resolve("list_aspsps", country)

    def start_authorization(self, **kwargs):
        self.calls.append(("start_authorization", kwargs))
        result = self.canned["start_authorization"]
        if isinstance(result, Exception):
            raise result
        return result

    def create_session(self, code):
        return self._resolve("create_session", code)

    def get_session(self, session_id):
        return self._resolve("get_session", session_id)

    def close_session(self, session_id):
        self._resolve("close_session", session_id)


def _patch_client(monkeypatch, fake_client: FakeClient):
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake_client)
    return fake_client


def _configure_credentials(client: TestClient):
    r = client.put(
        "/banking/credentials",
        json={"application_id": "app-123", "private_key": "-----BEGIN KEY-----secret"},
    )
    assert r.status_code == 200
    assert r.json()["has_credentials"] is True


BOURSORAMA_ASPSP = {
    "name": "Boursorama Banque",
    "country": "FR",
    "logo": "https://example/logo.png",
    "beta": False,
    "maximum_consent_validity": 15552000,  # 180 days
}


# ---------------------------------------------------------------------------
# Step 1 — GET /banking/check: config diagnostic (spec §C1)
# ---------------------------------------------------------------------------


def test_check_flags_callback_url_missing_from_declared_redirects(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    _patch_client(
        monkeypatch,
        FakeClient(get_application={"active": True, "redirect_urls": ["https://someone-else.example/cb"]}),
    )

    r = client.get("/banking/check")
    assert r.status_code == 200
    body = r.json()
    assert body["key_valid"] is True
    assert body["application_active"] is True
    assert body["callback_url_declared"] is False
    assert body["callback_url"] == get_settings().banking_callback_url


def test_check_reports_declared_callback_url_as_present(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    callback_url = get_settings().banking_callback_url
    _patch_client(
        monkeypatch,
        FakeClient(get_application={"active": True, "redirect_urls": [callback_url]}),
    )

    r = client.get("/banking/check")
    assert r.status_code == 200
    assert r.json()["callback_url_declared"] is True


def test_check_with_malformed_key_reports_invalid_key_instead_of_crashing(session, master_key, monkeypatch):
    """A malformed private key fails at JWT-signing time, inside build_client
    itself — before any HTTP call, so before FakeClient even gets a chance to
    run. The diagnostic must still answer, never surface a raw 500."""
    client = TestClient(app)
    _configure_credentials(client)

    def _raise(*args, **kwargs):
        raise ValueError("Could not deserialize key data")

    monkeypatch.setattr("services.banking.linking.build_client", _raise)

    r = client.get("/banking/check")
    assert r.status_code == 200
    body = r.json()
    assert body["key_valid"] is False
    assert body["error"] is not None


def test_check_without_credentials_never_calls_the_client(session, master_key, monkeypatch):
    client = TestClient(app)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("build_client must not be called when credentials are absent")

    monkeypatch.setattr("services.banking.linking.build_client", _fail_if_called)

    r = client.get("/banking/check")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["key_valid"] is False


# ---------------------------------------------------------------------------
# Step 1 — POST /banking/authorize: valid_until requested at the bank's max (spec §C2)
# ---------------------------------------------------------------------------


def test_authorize_requests_valid_until_at_bank_maximum(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    fake = _patch_client(
        monkeypatch,
        FakeClient(
            list_aspsps={"aspsps": [BOURSORAMA_ASPSP]},
            start_authorization={"url": "https://bank.example/authorize", "authorization_id": "auth-1"},
        ),
    )

    before = datetime.now(timezone.utc)
    r = client.post("/banking/authorize", json={"aspsp_name": "Boursorama Banque", "aspsp_country": "FR"})
    assert r.status_code == 200
    assert r.json()["auth_url"] == "https://bank.example/authorize"

    call = next(c for c in fake.calls if c[0] == "start_authorization")
    kwargs = call[1]
    assert kwargs["psu_type"] == "personal"
    assert kwargs["redirect_url"] == get_settings().banking_callback_url

    valid_until = datetime.fromisoformat(kwargs["valid_until"])
    requested_seconds = (valid_until - before).total_seconds()
    # Must be requested at (approximately) maximum_consent_validity, not some
    # arbitrary shorter "comfortable" duration — that would force a fresh
    # strong authentication far more often than the bank actually requires.
    assert abs(requested_seconds - BOURSORAMA_ASPSP["maximum_consent_validity"]) < 5

    # The random state was persisted as a blind index, never in clear.
    state = kwargs["state"]
    state_bidx = hash_index(state, master_key)
    row = session.exec(select(BankAuthorization).where(BankAuthorization.state_bidx == state_bidx)).first()
    assert row is not None


def test_authorize_unknown_aspsp_is_rejected(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    _patch_client(monkeypatch, FakeClient(list_aspsps={"aspsps": [BOURSORAMA_ASPSP]}))

    r = client.post("/banking/authorize", json={"aspsp_name": "Not A Real Bank", "aspsp_country": "FR"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Step 1 — GET /banking/callback: state validation, three outcomes (spec §C3)
# ---------------------------------------------------------------------------


def _create_pending_authorization(session, master_key, monkeypatch) -> str:
    """Runs the real authorize flow to get a valid, persisted `state`."""
    from dtos.banking import BankConnectionUpdate
    from services.banking.credentials import upsert_connection

    upsert_connection(
        session, USER_UUID, master_key, BankConnectionUpdate(application_id="app-123", private_key="key")
    )

    fake = FakeClient(
        list_aspsps={"aspsps": [BOURSORAMA_ASPSP]},
        start_authorization={"url": "https://bank.example/authorize", "authorization_id": "auth-1"},
    )
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)
    captured = {}

    def _capture(**kwargs):
        captured["state"] = kwargs["state"]
        return fake.canned["start_authorization"]

    fake.start_authorization = _capture
    start_authorization_flow(
        session, USER_UUID, master_key, "Boursorama Banque", "FR", get_settings().banking_callback_url
    )
    return captured["state"]


# Shaped like AccountResource (yaml:1772) — what POST /sessions actually returns,
# and the only place these attributes are ever delivered.
COURANT_ACCOUNT = {
    "uid": "uid-1",
    "identification_hash": "hash-1",
    "identification_hashes": ["hash-1", "hash-1-bban"],
    "name": "M. Dupont",
    "product": "Compte Bancaire",
    "currency": "EUR",
    "cash_account_type": "CACC",
    "usage": "PRIV",
    "psu_status": "Account Holder",
    "account_id": {"iban": "FR7630001007941234567890185"},
}


def _session_response(**overrides):
    body = {
        "session_id": "sess-1",
        "accounts": [COURANT_ACCOUNT],
        "aspsp": {"name": "Boursorama Banque", "country": "FR"},
        "psu_type": "personal",
        "access": {"valid_until": "2027-06-01T00:00:00+00:00"},
    }
    body.update(overrides)
    return body


def test_callback_without_master_key_cookie_shows_no_session_page(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.clear()

    r = client.get("/banking/callback", params={"code": "abc", "state": "whatever"})
    assert r.status_code == 200
    assert "onglet" in r.text  # "terminez dans l'onglet connecté"


def test_callback_unknown_state_is_rejected(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)

    r = client.get("/banking/callback", params={"code": "abc", "state": "never-issued"})
    assert r.status_code == 400


def test_callback_expired_state_is_rejected(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)

    state = "some-state-value"
    session.add(
        BankAuthorization(
            user_uuid_bidx=hash_index(USER_UUID, master_key),
            state_bidx=hash_index(state, master_key),
            aspsp_name_enc=encrypt_data("Boursorama Banque", master_key),
            aspsp_country_enc=encrypt_data("FR", master_key),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    session.commit()

    r = client.get("/banking/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400


def test_callback_access_denied_is_a_refusal_not_a_technical_error(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    state = _create_pending_authorization(session, master_key, monkeypatch)

    r = client.get("/banking/callback", params={"error": "access_denied", "state": state})
    assert r.status_code == 200
    assert "refus" in r.text.lower()


def test_callback_success_persists_session_and_redirects(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    state = _create_pending_authorization(session, master_key, monkeypatch)

    fake = FakeClient(create_session=_session_response())
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)

    r = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert r.status_code == 302
    assert "/settings/banking?bank_session=" in r.headers["location"]

    bank_session_uuid = r.headers["location"].rsplit("=", 1)[1]
    bank_session = session.get(BankSession, bank_session_uuid)
    assert bank_session is not None
    assert bank_session.status == "AUTHORIZED"
    assert decrypt_data(bank_session.session_id_enc, master_key) == "sess-1"

    # The authorization row is consumed — a second use is no longer possible.
    assert session.exec(select(BankAuthorization)).all() == []


def test_callback_replayed_request_after_success_does_not_duplicate_or_crash(
    session, master_key, monkeypatch
):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    state = _create_pending_authorization(session, master_key, monkeypatch)

    fake = FakeClient(create_session=_session_response())
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)

    r1 = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert r1.status_code == 302

    r2 = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    # The state was already consumed; replaying it must never crash, and must
    # never create a second session.
    assert r2.status_code == 400
    assert len(session.exec(select(BankSession)).all()) == 1


def test_callback_already_authorized_code_is_handled_gracefully(session, master_key, monkeypatch):
    """Direct service-level test of the AuthorizationInvalidError branch: the
    exact 'this code was already exchanged' business error from Enable Banking
    must produce a soft failure, never an unhandled exception, and must leave
    no partial state behind (spec §B5: 'idempotence covers the replay')."""
    state = "a-state-value"
    session.add(
        BankAuthorization(
            user_uuid_bidx=hash_index(USER_UUID, master_key),
            state_bidx=hash_index(state, master_key),
            aspsp_name_enc=encrypt_data("Boursorama Banque", master_key),
            aspsp_country_enc=encrypt_data("FR", master_key),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    session.commit()
    from dtos.banking import BankConnectionUpdate
    from services.banking.credentials import upsert_connection

    upsert_connection(session, USER_UUID, master_key, BankConnectionUpdate(application_id="a", private_key="k"))

    fake = FakeClient(
        create_session=AuthorizationInvalidError("ALREADY_AUTHORIZED", "Code already used")
    )
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)

    result = handle_callback(session, master_key, code="the-code", state=state, error=None)

    assert result.outcome == "error"
    assert "déjà été utilisé" in result.detail
    assert session.exec(select(BankSession)).all() == []
    # Nothing crashed and nothing was silently deleted either — a genuinely
    # concurrent legitimate retry could still find the row.
    assert session.exec(select(BankAuthorization)).all() != []


def _run_successful_callback(client, session, master_key, monkeypatch, response=None):
    """One full authorize → callback round trip, entirely behind the double."""
    state = _create_pending_authorization(session, master_key, monkeypatch)
    fake = FakeClient(create_session=response or _session_response())
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)
    r = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert r.status_code == 302
    return r.headers["location"].rsplit("=", 1)[1]


def test_callback_persists_the_accounts_payload_delivered_only_once(
    session, master_key, monkeypatch
):
    """POST /sessions returns AccountResource (name, IBAN, currency, product…);
    GET /sessions/{id} later returns SessionAccount, which is uid +
    identification hashes and nothing else. Anything not captured here is lost
    for good, so the whole payload is stored, encrypted (spec §C4)."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)

    bank_session_uuid = _run_successful_callback(client, session, master_key, monkeypatch)

    bank_session = session.get(BankSession, bank_session_uuid)
    assert bank_session.accounts_enc is not None
    stored = json.loads(decrypt_data(bank_session.accounts_enc, master_key))
    assert stored == [COURANT_ACCOUNT]
    # Kept alongside the primary hash: a later fuzzy match (IBAN vs BBAN) needs it.
    assert stored[0]["identification_hashes"] == ["hash-1", "hash-1-bban"]

    # And the rattachement listing serves them without any further API call.
    _forbid_client(monkeypatch)
    r = client.get(f"/banking/sessions/{bank_session_uuid}/accounts")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "M. Dupont"
    assert r.json()[0]["account_id"] == "FR7630001007941234567890185"


def test_callback_repoints_an_existing_link_at_the_new_session(session, master_key, monkeypatch):
    """The callback's own reconnection branch, the only consumer of the POST
    payload's `accounts` key: an already-linked account must follow the new
    session, uid included."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    bank_account_uuid = _create_bank_account(session, master_key)

    first_uuid = _run_successful_callback(client, session, master_key, monkeypatch)
    r = client.post(
        f"/banking/sessions/{first_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200

    second_uuid = _run_successful_callback(
        client,
        session,
        master_key,
        monkeypatch,
        _session_response(
            session_id="sess-2", accounts=[{**COURANT_ACCOUNT, "uid": "uid-9"}]
        ),
    )
    assert second_uuid != first_uuid

    links = session.exec(select(BankAccountLink)).all()
    assert len(links) == 1
    assert links[0].session_uuid == second_uuid
    assert decrypt_data(links[0].account_uid_enc, master_key) == "uid-9"


def test_callback_retires_the_superseded_session(session, master_key, monkeypatch):
    """Without this the abandoned row keeps status=AUTHORIZED and its stale
    consent_valid_until, and the Master-Key-less expiry job (spec §A3) would
    notify on a dead consent. Retired by status, never deleted: the links FK is
    ON DELETE RESTRICT."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)

    first_uuid = _run_successful_callback(client, session, master_key, monkeypatch)
    second_uuid = _run_successful_callback(
        client, session, master_key, monkeypatch, _session_response(session_id="sess-2")
    )

    assert session.get(BankSession, first_uuid).status == "CLOSED"
    assert session.get(BankSession, second_uuid).status == "AUTHORIZED"


def test_callback_keeps_a_superseded_session_that_still_holds_a_link(
    session, master_key, monkeypatch
):
    """An account the new consent didn't re-discover keeps its link on the old
    session — whose consent is genuinely still live. Retiring it would strand
    that link on a CLOSED session."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    bank_account_uuid = _create_bank_account(session, master_key)

    first_uuid = _run_successful_callback(client, session, master_key, monkeypatch)
    r = client.post(
        f"/banking/sessions/{first_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200

    _run_successful_callback(
        client,
        session,
        master_key,
        monkeypatch,
        _session_response(
            session_id="sess-2",
            accounts=[{"uid": "uid-2", "identification_hash": "hash-2"}],
        ),
    )

    assert session.get(BankSession, first_uuid).status == "AUTHORIZED"


def test_callback_without_a_frontend_url_never_500s_after_burning_the_code(
    session, master_key, monkeypatch
):
    """The authorization code is one-shot: a crash here costs the user a fresh
    strong authentication. A missing FRONTEND_URL degrades to a message page."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    monkeypatch.setattr(get_settings(), "frontend_url", "")
    state = _create_pending_authorization(session, master_key, monkeypatch)

    fake = FakeClient(create_session=_session_response())
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)

    r = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert r.status_code == 200
    assert "connect" in r.text.lower()
    # The session was still persisted — the consent isn't lost with the page.
    assert len(session.exec(select(BankSession)).all()) == 1


def test_callback_redirect_uses_the_frontend_url_setting(session, master_key, monkeypatch):
    client = TestClient(app)
    client.cookies.set("master_key", master_key)
    monkeypatch.setattr(get_settings(), "frontend_url", "https://app.example")
    state = _create_pending_authorization(session, master_key, monkeypatch)

    fake = FakeClient(create_session=_session_response())
    monkeypatch.setattr("services.banking.linking.build_client", lambda *a, **k: fake)

    r = client.get(
        "/banking/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://app.example/settings/banking?bank_session=")


def test_callback_error_message_does_not_reflect_raw_html(session, master_key, monkeypatch):
    """`error` is an attacker-controllable query parameter rendered on the very
    origin that holds the Master Key cookie."""
    client = TestClient(app)
    client.cookies.set("master_key", master_key)

    r = client.get("/banking/callback", params={"error": "<script>alert(1)</script>"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


# ---------------------------------------------------------------------------
# Step 6 — rattachement to a CapitalView account, including reconnection
# ---------------------------------------------------------------------------


def _create_bank_account(session, master_key, balance="1000.00") -> str:
    from dtos.bank import BankAccountCreate
    from models.enums import BankAccountType
    from services.bank import create_bank_account

    created = create_bank_account(
        session,
        BankAccountCreate(
            name="Compte courant",
            balance=balance,
            account_type=BankAccountType.CHECKING,
        ),
        USER_UUID,
        master_key,
    )
    return created.id


def _create_bank_session(session, master_key, session_id="sess-1", accounts=None) -> str:
    if accounts is None:
        accounts = [COURANT_ACCOUNT]
    bank_session = BankSession(
        user_uuid_bidx=hash_index(USER_UUID, master_key),
        session_id_enc=encrypt_data(session_id, master_key),
        aspsp_name_enc=encrypt_data("Boursorama Banque", master_key),
        aspsp_country_enc=encrypt_data("FR", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime.now(timezone.utc) + timedelta(days=90),
        authorized_at=datetime.now(timezone.utc),
        accounts_enc=encrypt_data(json.dumps(accounts), master_key),
    )
    session.add(bank_session)
    session.commit()
    session.refresh(bank_session)
    return bank_session.uuid


def _forbid_client(monkeypatch):
    """The rattachement step reads the accounts captured at the callback; it must
    not need the network at all (GET /sessions/{id} no longer carries them)."""

    def _fail(*args, **kwargs):
        raise AssertionError("the rattachement step must not call Enable Banking")

    monkeypatch.setattr("services.banking.linking.build_client", _fail)


def test_list_session_accounts_reports_linked_state(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(
        session,
        master_key,
        accounts=[COURANT_ACCOUNT, {"uid": "uid-2", "identification_hash": "hash-2"}],
    )
    _forbid_client(monkeypatch)

    r = client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200
    assert r.json()["reconnected"] is False

    r = client.get(f"/banking/sessions/{bank_session_uuid}/accounts")
    assert r.status_code == 200
    by_hash = {a["identification_hash"]: a for a in r.json()}
    assert by_hash["hash-1"]["linked"] is True
    assert by_hash["hash-1"]["bank_account_uuid"] == bank_account_uuid
    assert by_hash["hash-2"]["linked"] is False

    # The picker needs something a human can recognise, not just a base64 hash.
    assert by_hash["hash-1"]["name"] == "M. Dupont"
    assert by_hash["hash-1"]["product"] == "Compte Bancaire"
    assert by_hash["hash-1"]["currency"] == "EUR"
    assert by_hash["hash-1"]["cash_account_type"] == "CACC"
    assert by_hash["hash-1"]["account_id"] == "FR7630001007941234567890185"


def test_link_new_account_bootstraps_anchor_from_capitalview_balance(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key, balance="1234.56")
    bank_session_uuid = _create_bank_session(session, master_key)
    _forbid_client(monkeypatch)

    r = client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200

    link = session.exec(select(BankAccountLink)).one()
    assert decrypt_data(link.anchor_balance_enc, master_key) == "1234.56"
    assert link.anchor_date == date.today()
    # Set before today so the front's "sync if last_synced_at < today" trigger
    # fires the real fetch immediately after linking.
    assert link.last_synced_at < date.today()


def test_reconnection_updates_existing_link_instead_of_creating_a_new_one(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    first_session_uuid = _create_bank_session(session, master_key, session_id="sess-1")
    _forbid_client(monkeypatch)

    r = client.post(
        f"/banking/sessions/{first_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200
    assert r.json()["reconnected"] is False
    assert len(session.exec(select(BankAccountLink)).all()) == 1

    # Simulate a reconnection: a brand new bank_sessions row, same identification_hash.
    second_session_uuid = _create_bank_session(
        session,
        master_key,
        session_id="sess-2",
        accounts=[{**COURANT_ACCOUNT, "uid": "uid-9"}],
    )
    r = client.post(
        f"/banking/sessions/{second_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200
    assert r.json()["reconnected"] is True

    links = session.exec(select(BankAccountLink)).all()
    assert len(links) == 1
    assert links[0].session_uuid == second_session_uuid
    assert decrypt_data(links[0].account_uid_enc, master_key) == "uid-9"


def test_link_account_not_in_session_is_rejected(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(session, master_key)
    _forbid_client(monkeypatch)

    r = client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "not-in-session", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /banking/sessions/{uuid} — R3: only ever against an injected double
# ---------------------------------------------------------------------------


def test_delete_session_unlinks_accounts_and_closes_remotely_via_double(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(session, master_key)

    fake = _patch_client(monkeypatch, FakeClient(close_session=None))
    r = client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    )
    assert r.status_code == 200
    assert len(session.exec(select(BankAccountLink)).all()) == 1

    r = client.delete(f"/banking/sessions/{bank_session_uuid}")
    assert r.status_code == 204

    assert session.get(BankSession, bank_session_uuid) is None
    assert session.exec(select(BankAccountLink)).all() == []
    assert ("close_session", "sess-1") in fake.calls


def test_delete_unknown_session_is_404(session, master_key, monkeypatch):
    client = TestClient(app)
    r = client.delete("/banking/sessions/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /banking/status and GET /banking/aspsps — frozen in api-contract.md, and
# until this fix round neither had a single test. `/banking/status` was calling
# a name its module had stopped importing, so every call raised NameError.
# ---------------------------------------------------------------------------


def test_status_reports_no_credentials_before_anything_is_configured(session, master_key):
    r = TestClient(app).get("/banking/status")

    assert r.status_code == 200
    assert r.json() == {"has_credentials": False, "application_id": None}


def test_status_reports_the_application_id_but_never_the_private_key(session, master_key):
    client = TestClient(app)
    _configure_credentials(client)

    r = client.get("/banking/status")

    assert r.status_code == 200
    assert r.json() == {"has_credentials": True, "application_id": "app-123"}
    assert "secret" not in r.text
    assert "private_key" not in r.text


def test_aspsps_lists_the_catalogue_for_a_country(session, master_key, monkeypatch):
    client = TestClient(app)
    _configure_credentials(client)
    fake = _patch_client(monkeypatch, FakeClient(list_aspsps={"aspsps": [BOURSORAMA_ASPSP]}))

    r = client.get("/banking/aspsps", params={"country": "FR"})

    assert r.status_code == 200
    assert [a["name"] for a in r.json()] == ["Boursorama Banque"]
    assert ("list_aspsps", "FR") in fake.calls


def test_aspsps_without_credentials_is_a_400_not_a_crash(session, master_key):
    r = TestClient(app).get("/banking/aspsps", params={"country": "FR"})

    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /banking/import-export — Task 11's only route, and it had no test either
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Same workaround as tests/services/test_bank.py:30."""
    import sqlalchemy as sa

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


def test_import_export_ingests_an_export_for_a_linked_account(
    session, master_key, monkeypatch, sqlite_pg_insert
):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(session, master_key)
    _forbid_client(monkeypatch)
    assert (
        client.post(
            f"/banking/sessions/{bank_session_uuid}/link",
            json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
        ).status_code
        == 200
    )

    r = client.post(
        "/banking/import-export",
        json={
            "accounts": [
                {
                    "info": {"identification_hash": "hash-1", "cash_account_type": "CACC"},
                    "transactions": [
                        {
                            "entry_reference": "export-ref-1",
                            "transaction_amount": {"currency": "EUR", "amount": "45.50"},
                            "credit_debit_indicator": "DBIT",
                            "status": "BOOK",
                            "booking_date": (date.today() - timedelta(days=10)).isoformat(),
                        }
                    ],
                    "balances": [
                        {
                            "balance_amount": {"currency": "EUR", "amount": "954.50"},
                            "balance_type": "CLBD",
                            "reference_date": (date.today() - timedelta(days=1)).isoformat(),
                        }
                    ],
                }
            ]
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["imported_accounts"] == 1
    assert body["results"][0]["status"] == "imported"
    assert body["results"][0]["inserted"] == 1


def test_import_export_rejects_a_payload_that_is_not_an_export():
    r = TestClient(app).post("/banking/import-export", json={"nope": True})

    assert r.status_code == 400
    assert "accounts" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The opt-in gate, and the connection listing it must survive
# ---------------------------------------------------------------------------


def _opt_out(session, master_key):
    from services.settings import get_or_create_settings

    settings = get_or_create_settings(session, USER_UUID, master_key)
    settings.open_banking_enabled = False
    session.add(settings)
    session.commit()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/banking/credentials", {"application_id": "a", "private_key": "k"}),
        ("get", "/banking/aspsps?country=FR", None),
        ("post", "/banking/authorize", {"aspsp_name": "X", "aspsp_country": "FR"}),
        ("get", "/banking/sessions/whatever/accounts", None),
        ("post", "/banking/sessions/whatever/link", {"identification_hash": "h", "bank_account_uuid": "b"}),
        ("post", "/banking/sync", None),
        ("post", "/banking/import-export", {"accounts": []}),
    ],
)
def test_opting_out_closes_every_route_that_reaches_the_bank(
    session, master_key, monkeypatch, method, path, body
):
    client = TestClient(app)
    _configure_credentials(client)
    _opt_out(session, master_key)
    # Nothing may reach Enable Banking: the refusal has to come before the call.
    monkeypatch.setattr(
        "services.banking.linking.build_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gated route called the bank")),
    )

    r = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 403


def test_opting_out_still_lets_the_user_see_and_drop_what_is_attached(
    session, master_key, monkeypatch
):
    """Turning the feature off must not strand an existing connection."""
    client = TestClient(app)
    _configure_credentials(client)
    bank_session_uuid = _create_bank_session(session, master_key)
    _opt_out(session, master_key)

    assert client.get("/banking/status").status_code == 200
    listed = client.get("/banking/sessions")
    assert listed.status_code == 200
    assert [s["uuid"] for s in listed.json()] == [bank_session_uuid]

    _patch_client(monkeypatch, FakeClient(close_session=None))
    assert client.delete(f"/banking/sessions/{bank_session_uuid}").status_code == 204


def test_list_sessions_reports_the_consent_and_its_attached_accounts(
    session, master_key, monkeypatch
):
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(session, master_key)
    _forbid_client(monkeypatch)

    assert client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    ).status_code == 200

    r = client.get("/banking/sessions")
    assert r.status_code == 200
    [summary] = r.json()
    assert summary["uuid"] == bank_session_uuid
    assert summary["aspsp_name"] == "Boursorama Banque"
    assert summary["aspsp_country"] == "FR"
    assert summary["status"] == "AUTHORIZED"
    assert summary["active"] is True
    assert summary["status_message"] == "Consentement actif et autorisé."
    assert [a["bank_account_uuid"] for a in summary["accounts"]] == [bank_account_uuid]
    assert summary["accounts"][0]["name"] == "Compte courant"


def test_a_retired_session_is_still_listed_with_its_accounts(session, master_key, monkeypatch):
    """An expired consent keeps its links by design — the list is where the user
    learns that a reconnection is the only thing missing."""
    client = TestClient(app)
    _configure_credentials(client)
    bank_account_uuid = _create_bank_account(session, master_key)
    bank_session_uuid = _create_bank_session(session, master_key)
    _forbid_client(monkeypatch)
    assert client.post(
        f"/banking/sessions/{bank_session_uuid}/link",
        json={"identification_hash": "hash-1", "bank_account_uuid": bank_account_uuid},
    ).status_code == 200

    expired = session.get(BankSession, bank_session_uuid)
    expired.status = "EXPIRED"
    session.add(expired)
    session.commit()

    [summary] = client.get("/banking/sessions").json()
    assert summary["active"] is False
    assert "Reconnectez" in summary["status_message"]
    assert [a["bank_account_uuid"] for a in summary["accounts"]] == [bank_account_uuid]


def test_flows_is_readable_with_the_feature_switched_off(session, master_key, monkeypatch):
    """Observed history belongs to the user whether or not the opt-in is on."""
    client = TestClient(app)
    _configure_credentials(client)
    _opt_out(session, master_key)

    r = client.get("/banking/flows?months=3")
    assert r.status_code == 200
    body = r.json()
    assert body["account_count"] == 0
    assert len(body["months"]) == 3
    assert body["inflow"] == "0"


def test_flows_window_is_clamped_rather_than_rejected(session, master_key):
    client = TestClient(app)
    _configure_credentials(client)

    assert len(client.get("/banking/flows?months=0").json()["months"]) == 1
    assert len(client.get("/banking/flows?months=999").json()["months"]) == 120
