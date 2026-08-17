"""
Enable Banking API client — RS256-signed reads for accounts, balances and transactions.

Every CapitalView user brings their own Enable Banking application (the free
tier only exposes accounts the application owner linked themselves, see
credentials.py), so one `EnableBankingClient` is built per user via
`build_client`, the module-level factory later tasks call and tests
monkeypatch. This module never touches credential storage — it receives
`application_id` and `private_key` as plain arguments.

Normalising API payloads (transactions, balances) into the database is out
of scope here: `iter_transactions` and `get_balances` yield/return the raw
API shapes, untouched.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import jwt

from services.banking.errors import PaginationLimitExceededError, error_from_response

BASE_URL = "https://api.enablebanking.com"

# Capped at 86400s by the API (see faq.md); an hour comfortably outlives a single sync run.
TOKEN_TTL_SECONDS = 3600

# Guards against a continuation_key that never terminates (spec §B3). 2 776 real
# transactions paginated at ~100/page; this leaves generous headroom.
MAX_TRANSACTION_PAGES = 1000


def _build_jwt(application_id: str, private_key: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": application_id},
    )


class EnableBankingClient:
    """
    Thin read-oriented wrapper around the Enable Banking API.

    `psu_context` carries the PSU headers (IP, user agent, ...) of the real
    request that triggered the call — it is never fabricated by this client,
    only forwarded. The API treats it as all-or-nothing, so it is attached to
    a request in full or not at all, never partially.

    `transport` is test-only: it lets tests substitute `httpx.MockTransport`
    for the real network. Production callers leave it unset.
    """

    def __init__(
        self,
        application_id: str,
        private_key: str,
        psu_context: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._psu_context = psu_context
        token = _build_jwt(application_id, private_key)
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EnableBankingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Application / catalogue
    # ------------------------------------------------------------------

    def get_application(self) -> dict[str, Any]:
        """GET /application — validates the key and lists declared redirect URLs."""
        return self._request("GET", "/application")

    def list_aspsps(self, country: str) -> dict[str, Any]:
        """GET /aspsps — banks available in a country, with their required PSU headers."""
        return self._request("GET", "/aspsps", params={"country": country})

    # ------------------------------------------------------------------
    # Authorization journey
    # ------------------------------------------------------------------

    def start_authorization(
        self,
        aspsp_name: str,
        aspsp_country: str,
        redirect_url: str,
        state: str,
        valid_until: str,
        psu_type: str = "personal",
    ) -> dict[str, Any]:
        """POST /auth — opens the authorization journey, returns the bank's redirect URL."""
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        return self._request("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict[str, Any]:
        """POST /sessions — exchanges the callback code for a session and its accounts."""
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict[str, Any]:
        """GET /sessions/{id} — session status and accounts."""
        return self._request("GET", f"/sessions/{session_id}", headers=self._psu_headers())

    def close_session(self, session_id: str) -> None:
        """DELETE /sessions/{id} — closes the session, and the PSU's bank consent
        with it when the ASPSP allows it. R3: never exercised against the real
        service outside an explicit user-initiated disconnect."""
        response = self._http.request("DELETE", f"/sessions/{session_id}")
        if response.status_code >= 400:
            raise error_from_response(response)

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    def get_balances(self, uid: str) -> dict[str, Any]:
        """GET /accounts/{uid}/balances — raw balance list, accounting vs real-time included."""
        return self._request("GET", f"/accounts/{uid}/balances", headers=self._psu_headers())

    def iter_transactions(
        self,
        uid: str,
        date_from: date | str | None = None,
        strategy: str = "default",
    ) -> Iterator[dict[str, Any]]:
        """
        Walk the paginated transaction feed for an account, yielding raw transaction dicts.

        Three rules, all measured against Boursorama (spec §B3): an empty page is
        not the end — only the absence of `continuation_key` is; the key must
        travel alongside the original params, never alone; and the walk is
        bounded so a repeating key can't loop forever.
        """
        params: dict[str, str] = {"strategy": strategy}
        if date_from is not None:
            params["date_from"] = (
                date_from.isoformat() if isinstance(date_from, date) else date_from
            )

        headers = self._psu_headers()
        for _ in range(MAX_TRANSACTION_PAGES):
            data = self._request(
                "GET", f"/accounts/{uid}/transactions", params=params, headers=headers
            )
            yield from data.get("transactions", [])
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                return
            params = {**params, "continuation_key": continuation_key}

        raise PaginationLimitExceededError(
            f"account {uid}: continuation_key still present after {MAX_TRANSACTION_PAGES} pages"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _psu_headers(self) -> dict[str, str]:
        return dict(self._psu_context) if self._psu_context else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise error_from_response(response)
        return response.json()


def build_client(
    application_id: str,
    private_key: str,
    psu_context: dict[str, str] | None = None,
) -> EnableBankingClient:
    """Module-level factory (ruling R2) — later tasks call this, tests monkeypatch it."""
    return EnableBankingClient(application_id, private_key, psu_context=psu_context)
