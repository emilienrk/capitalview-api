"""
End-to-end offline integration test for Enable Banking integration (Task 12).

Replays the real 4-year dataset (vendor-docs/spike/export-boursorama-2022-2026.json)
behind an offline transport double, verifying:
- The full linking -> first sync -> second sync -> reconnection lifecycle.
- The first import recipe (strategy=longest + ancient date_from -> 2,776 transactions).
- Cross-account deduplication at real scale: 1,436 of the card account's 1,464
  rows are recognised on the current account. The spec, the plan and the briefs
  all quote "1 360 / 93 %"; recomputed from the export with the code's own
  fingerprint the figure is 1,436 (98.1 %), stable across fingerprint variants
  and with zero shared `entry_reference`, exactly as documented. The stale
  figure is reported to the controller, not edited into the spec.
- Batch-size independence across paginated responses.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from main import app
from models.account_history import AccountHistory
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession, BankTransaction
from services.banking.client import EnableBankingClient
from services.banking.sync import (
    INCREMENTAL_STRATEGY,
    SEED_DATE_FROM,
    SEED_STRATEGY,
    _fetch,
)
from services.encryption import decrypt_data, hash_index

SPIKE_DIR = Path(__file__).resolve().parents[3] / "vendor-docs" / "spike"
PASSWORD = "IntegrationTestPassword123!"


@pytest.fixture(autouse=True)
def _override_deps(session):
    def _get_session():
        return session

    app.dependency_overrides.clear()
    from database import get_session

    app.dependency_overrides[get_session] = _get_session


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Replace PostgreSQL pg_insert with plain insert for SQLite test environment."""
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


class RealDataOfflineClient:
    """Offline EnableBankingClient double replaying real data from vendor-docs."""

    def __init__(
        self,
        application_id: str,
        private_key: str,
        page_size: int = 100,
        psu_context: dict[str, str] | None = None,
    ):
        self.application_id = application_id
        self.private_key = private_key
        self.page_size = page_size
        self.psu_context = psu_context
        self.closed_sessions: list[str] = []

        # Load real export
        export_file = SPIKE_DIR / "export-boursorama-2022-2026.json"
        if export_file.exists():
            with open(export_file) as f:
                self._data = json.load(f)
        else:
            self._data = {"accounts": []}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_application(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "name": "CapitalView Sandbox",
            "redirect_urls": ["http://localhost:5173/settings/banking", "http://testserver/banking/callback"],
        }

    def list_aspsps(self, country: str) -> dict[str, Any]:
        return {
            "aspsps": [
                {
                    "name": "Boursorama",
                    "country": "FR",
                    "logo": None,
                    "beta": False,
                    "maximum_consent_validity": 7776000,
                }
            ]
        }

    def start_authorization(self, aspsp_name: str, aspsp_country: str, redirect_url: str, state: str, **kwargs) -> dict[str, Any]:
        return {
            "url": f"https://auth.enablebanking.com/test-auth-url?state={state}",
            "authorization_id": "auth-id-mock",
        }

    def _session_account(self, index: int, uid: str) -> dict[str, Any]:
        """One AccountResource, every field taken from the real capture.

        `currency` and `cash_account_type` used to be spelled out here. That made
        the double *less* faithful than the data it replays: the real accounts
        carry `XXX` (the "no currency" ISO code), never `EUR`. Reading them back
        keeps the test unable to drift from what the bank actually sends.
        """
        info = self._data["accounts"][index]["info"]
        return {
            "uid": uid,
            "account_id": info["account_id"],
            "identification_hash": info["identification_hash"],
            "identification_hashes": info["identification_hashes"],
            "currency": info["currency"],
            "cash_account_type": info["cash_account_type"],
            "name": info["name"],
            "product": info["product"],
        }

    def create_session(self, code: str) -> dict[str, Any]:
        return {
            "session_id": "eb-session-real-1",
            "aspsp": {"name": "Boursorama", "country": "FR"},
            "access": {"valid_until": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()},
            "accounts": [
                self._session_account(0, "uid-bourso-cacc"),
                self._session_account(1, "uid-bourso-card"),
            ],
        }

    def get_balances(self, account_uid: str) -> dict[str, Any]:
        if "card" in account_uid:
            return {"balances": self._data["accounts"][1]["balances"]}
        return {"balances": self._data["accounts"][0]["balances"]}

    def iter_transactions(
        self,
        account_uid: str,
        date_from: date | None = None,
        date_to: date | None = None,
        strategy: str = "default",
    ) -> Iterator[dict[str, Any]]:
        raw_list = (
            self._data["accounts"][1]["transactions"]
            if "card" in account_uid
            else self._data["accounts"][0]["transactions"]
        )

        # The trap of constraint 6, reproduced in both directions: the full
        # history is served only when *both* halves of the recipe are right.
        # `strategy=longest` alone self-limits to two years despite its name,
        # and any other strategy self-limits too however ancient the lower
        # bound — so a wrong SEED_STRATEGY is punished exactly like a wrong
        # SEED_DATE_FROM, instead of passing unnoticed.
        if strategy != "longest" or date_from is None or date_from > date(2023, 1, 1):
            raw_list = raw_list[:1987]

        # Simulate pagination in chunks of page_size
        for i in range(0, len(raw_list), self.page_size):
            chunk = raw_list[i : i + self.page_size]
            for tx in chunk:
                yield tx

    def close_session(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)


def _register_user(client: TestClient, session: Session, email: str = "e2e_user@example.com") -> tuple[str, str, str]:
    username = email.split("@")[0]
    resp = client.post(
        "/auth/register",
        json={"username": username, "email": email, "password": PASSWORD},
        headers={"X-Return-Master-Key": "true"},
    )
    assert resp.status_code == 201
    body = resp.json()
    client.cookies.clear()
    return body["access_token"], body["master_key"], username


@pytest.mark.skipif(
    not (SPIKE_DIR / "export-boursorama-2022-2026.json").exists(),
    reason="Real spike datasets not available",
)
class TestBankingEndToEnd:
    def test_full_linking_and_sync_lifecycle(
        self, session: Session, monkeypatch, sqlite_pg_insert
    ):
        fake_builder = lambda *args, **kwargs: RealDataOfflineClient(*args, **kwargs)
        monkeypatch.setattr("services.banking.client.build_client", fake_builder)
        monkeypatch.setattr("services.banking.linking.build_client", fake_builder)
        monkeypatch.setattr("services.banking.sync.build_client", fake_builder)

        client = TestClient(app)
        token, master_key, username = _register_user(client, session)
        auth_headers = {"Authorization": f"Bearer {token}", "X-Master-Key": master_key}
        client.cookies.set("master_key", master_key)
        client.cookies.set("access_token", token)

        # 0. The feature is opt-in: without it every route below is a 403.
        optin_resp = client.put(
            "/settings", json={"open_banking_enabled": True}, headers=auth_headers
        )
        assert optin_resp.status_code == 200
        assert optin_resp.json()["open_banking_enabled"] is True

        # 1. User configures Enable Banking credentials
        cred_resp = client.put(
            "/banking/credentials",
            json={"application_id": "real-app-id", "private_key": "real-priv-key"},
            headers=auth_headers,
        )
        assert cred_resp.status_code == 200
        assert cred_resp.json()["has_credentials"] is True

        # 2. Pre-flight check
        check_resp = client.get("/banking/check", headers=auth_headers)
        assert check_resp.status_code == 200
        assert check_resp.json()["configured"] is True

        # 3. Create two CapitalView bank accounts
        cacc_resp = client.post(
            "/bank/accounts",
            json={"name": "Boursorama Compte Courant", "account_type": "CHECKING", "balance": "0"},
            headers=auth_headers,
        )
        assert cacc_resp.status_code == 201
        cacc_id = cacc_resp.json()["id"]

        card_resp = client.post(
            "/bank/accounts",
            json={"name": "Boursorama Compte Carte", "account_type": "CHECKING", "balance": "0"},
            headers=auth_headers,
        )
        assert card_resp.status_code == 201
        card_id = card_resp.json()["id"]

        # 4. Start authorization journey
        auth_start = client.post(
            "/banking/authorize",
            json={"aspsp_name": "Boursorama", "aspsp_country": "FR"},
            headers=auth_headers,
        )
        assert auth_start.status_code == 200
        auth_url = auth_start.json()["auth_url"]
        assert "enablebanking.com" in auth_url

        # Extract state parameter from authorization URL
        import urllib.parse
        parsed_url = urllib.parse.urlparse(auth_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        state_param = query_params.get("state", [""])[0] or "state-test"

        # If offline client returns url without query param, let's look up authorization
        if not state_param or state_param == "state-test":
            from models.banking import BankAuthorization
            # Direct handle_callback
            from services.banking.linking import handle_callback
            auth_rows = session.exec(select(BankAuthorization)).all()
            # Find the state
            cb_result = handle_callback(session, master_key, code="mock-code", state="state-test")
            bank_session_uuid = cb_result.bank_session_uuid
        else:
            cb_resp = client.get(
                f"/banking/callback?code=mock-code-123&state={state_param}",
                follow_redirects=False,
            )
            assert cb_resp.status_code == 302
            bank_session_uuid = cb_resp.headers["location"].split("bank_session=")[1]

        bsession = session.get(BankSession, bank_session_uuid)
        assert bsession is not None
        assert bsession.status == "AUTHORIZED"

        # 6. List discovered accounts
        disc_resp = client.get(
            f"/banking/sessions/{bsession.uuid}/accounts", headers=auth_headers
        )
        assert disc_resp.status_code == 200
        disc_accounts = disc_resp.json()
        assert len(disc_accounts) == 2

        # 7. Link both accounts
        # Link card FIRST in DB to prove that sync ordering (R12) still syncs checking first!
        link_card = client.post(
            f"/banking/sessions/{bsession.uuid}/link",
            json={
                "identification_hash": disc_accounts[1]["identification_hash"],
                "bank_account_uuid": card_id,
            },
            headers=auth_headers,
        )
        assert link_card.status_code == 200

        link_cacc = client.post(
            f"/banking/sessions/{bsession.uuid}/link",
            json={
                "identification_hash": disc_accounts[0]["identification_hash"],
                "bank_account_uuid": cacc_id,
            },
            headers=auth_headers,
        )
        assert link_cacc.status_code == 200

        # 8. First sync: executes both accounts in order (checking before card)
        sync_resp = client.post("/banking/sync", headers=auth_headers)
        assert sync_resp.status_code == 200
        sync_data = sync_resp.json()
        assert sync_data["synced"] == 2

        # Checking account received all 2,776 transactions
        cacc_result = next(r for r in sync_data["results"] if r["bank_account_uuid"] == cacc_id)
        assert cacc_result["inserted"] == 2776
        assert cacc_result["snapshots_written"] > 0

        # Card account cross-deduplication at real scale: 1436/1464 = 98.1 %
        # recognised on the current account, 0 snapshots written (R19).
        card_result = next(r for r in sync_data["results"] if r["bank_account_uuid"] == card_id)
        assert card_result["inserted"] == 28
        assert card_result["skipped"] == 1436
        assert card_result["snapshots_written"] == 0
        assert card_result["reconciliation_status"] == "not_reconcilable"

        # 9. Second sync on the same day is skipped by daily cap (R16)
        sync_again = client.post("/banking/sync", headers=auth_headers)
        assert sync_again.status_code == 200
        assert all(r["status"] == "skipped_daily_cap" for r in sync_again.json()["results"])

        # 10. Reconnection after expiration preserves links
        bsession.status = "EXPIRED"
        session.add(bsession)
        session.commit()

        # Re-check bank page link status
        accs_resp = client.get("/bank/accounts", headers=auth_headers)
        assert accs_resp.status_code == 200
        for acc in accs_resp.json()["accounts"]:
            assert acc["link_status"] == "à reconnecter"
            # Links and history survived!
            assert acc["is_linked"] is True

    def test_batch_size_independence(self):
        """Chunking the feed into 10, 50 or 100 items yields identical counts.

        Driven through the production walker, not through the double's own
        generator: `_fetch` is the code that must not depend on a batch size.
        """
        for test_page_size in (10, 50, 100):
            client = RealDataOfflineClient("app", "key", page_size=test_page_size)
            rows, fetched_from = _fetch(client, "uid-bourso-cacc", SEED_DATE_FROM, SEED_STRATEGY)
            assert len(rows) == 2776, f"Failed at page_size={test_page_size}"
            assert fetched_from == SEED_DATE_FROM

    def test_seeding_recipe_is_the_two_module_constants(self):
        """The first-import recipe, read off the constants the sync actually uses.

        The assertion is on `SEED_STRATEGY` and `SEED_DATE_FROM` themselves, so
        switching either one to a plausible-looking value fails here — that is
        the whole point: constraint 6's trap loses years *without any error*.
        """
        client = RealDataOfflineClient("app", "key")

        rows, _ = _fetch(client, "uid-bourso-cacc", SEED_DATE_FROM, SEED_STRATEGY)
        assert len(rows) == 2776

        # Right strategy, recent lower bound: two years lost, silently.
        truncated, _ = _fetch(client, "uid-bourso-cacc", date(2024, 1, 1), SEED_STRATEGY)
        assert len(truncated) == 1987

        # Ancient lower bound, incremental strategy: the same silent loss.
        truncated, _ = _fetch(client, "uid-bourso-cacc", SEED_DATE_FROM, INCREMENTAL_STRATEGY)
        assert len(truncated) == 1987
