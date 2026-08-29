import json
from datetime import date
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.banking.client import EnableBankingClient, build_client
from services.banking.errors import (
    BankingApiError,
    InvalidPeriodError,
    PaginationLimitExceededError,
    SessionInvalidError,
)

VENDOR_SPIKE_DIR = Path(__file__).resolve().parents[2].parent / "vendor-docs" / "spike"


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    """A throwaway RSA key, just to exercise real RS256 signing — never used to talk to Enable Banking."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


def _client(
    private_key: str, handler, psu_context: dict[str, str] | None = None
) -> EnableBankingClient:
    return EnableBankingClient(
        "app-123", private_key, psu_context=psu_context, transport=httpx.MockTransport(handler)
    )


# ---------------------------------------------------------------------------
# Token shape (spec §B1)
# ---------------------------------------------------------------------------


def test_token_carries_kid_iss_aud_and_bounded_expiry(rsa_private_key_pem):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"redirect_urls": []})

    client = _client(rsa_private_key_pem, handler)
    client.get_application()

    token = captured[0].headers["authorization"].removeprefix("Bearer ")
    header = jwt.get_unverified_header(token)
    payload = jwt.decode(token, options={"verify_signature": False})

    assert header["kid"] == "app-123"
    assert header["alg"] == "RS256"
    assert payload["iss"] == "enablebanking.com"
    assert payload["aud"] == "api.enablebanking.com"
    assert 0 < payload["exp"] - payload["iat"] <= 86400


# ---------------------------------------------------------------------------
# Request shapes — list_aspsps, start_authorization, create_session
# ---------------------------------------------------------------------------


def test_list_aspsps_sends_country_query_param(rsa_private_key_pem):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"aspsps": []})

    client = _client(rsa_private_key_pem, handler)
    client.list_aspsps("FR")

    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == "/aspsps"
    assert dict(request.url.params) == {"country": "FR"}


def test_start_authorization_builds_request_per_start_authorization_request_schema(
    rsa_private_key_pem,
):
    """Body shape matches StartAuthorizationRequest (enablebanking-api.yaml:4080):
    access.valid_until, aspsp{name,country}, state, redirect_url, psu_type."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url": "https://bank.example/authorize"})

    client = _client(rsa_private_key_pem, handler)
    client.start_authorization(
        aspsp_name="Boursorama Banque",
        aspsp_country="FR",
        redirect_url="https://capitalview.app/banking/callback",
        state="a-random-state",
        valid_until="2027-02-13T00:00:00+00:00",
    )

    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/auth"
    assert json.loads(request.content) == {
        "access": {"valid_until": "2027-02-13T00:00:00+00:00"},
        "aspsp": {"name": "Boursorama Banque", "country": "FR"},
        "state": "a-random-state",
        "redirect_url": "https://capitalview.app/banking/callback",
        "psu_type": "personal",
    }


def test_create_session_sends_code_only(rsa_private_key_pem):
    """Body shape matches AuthorizeSessionRequest (enablebanking-api.yaml:1971): {code}."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"session_id": "sess-1", "accounts": []})

    client = _client(rsa_private_key_pem, handler)
    client.create_session("the-callback-code")

    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/sessions"
    assert json.loads(request.content) == {"code": "the-callback-code"}


def test_start_authorization_and_create_session_never_send_psu_headers(rsa_private_key_pem):
    """/auth and /sessions (POST) carry no Psu-* parameters in the contract — only
    GET /sessions/{id}, /accounts/{uid}/balances and /accounts/{uid}/transactions do.
    A supplied psu_context must stay confined to those, never leak onto these two."""
    captured = []
    psu_context = {"Psu-Ip-Address": "203.0.113.5", "Psu-User-Agent": "Mozilla/5.0"}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/auth":
            return httpx.Response(200, json={"url": "https://bank.example/authorize"})
        return httpx.Response(200, json={"session_id": "sess-1", "accounts": []})

    client = _client(rsa_private_key_pem, handler, psu_context=psu_context)
    client.start_authorization(
        aspsp_name="Boursorama Banque",
        aspsp_country="FR",
        redirect_url="https://capitalview.app/banking/callback",
        state="a-random-state",
        valid_until="2027-02-13T00:00:00+00:00",
    )
    client.create_session("the-callback-code")

    assert len(captured) == 2
    for request in captured:
        assert not any(name.lower().startswith("psu-") for name in request.headers)


# ---------------------------------------------------------------------------
# Pagination (spec §B3)
# ---------------------------------------------------------------------------


def test_iter_transactions_does_not_stop_on_empty_page_with_continuation_key(rsa_private_key_pem):
    pages = iter(
        [
            (200, {"transactions": [], "continuation_key": "k1"}),
            (200, {"transactions": [{"entry_reference": "a"}, {"entry_reference": "b"}]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = next(pages)
        return httpx.Response(status, json=body)

    client = _client(rsa_private_key_pem, handler)
    result = list(client.iter_transactions("uid-1"))

    assert [tx["entry_reference"] for tx in result] == ["a", "b"]


def test_continuation_key_travels_with_original_params(rsa_private_key_pem):
    captured = []
    pages = iter(
        [
            (200, {"transactions": [], "continuation_key": "k1"}),
            (200, {"transactions": [{"entry_reference": "a"}]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        status, body = next(pages)
        return httpx.Response(status, json=body)

    client = _client(rsa_private_key_pem, handler)
    list(client.iter_transactions("uid-1", date_from=date(2022, 1, 1), strategy="longest"))

    first_params = dict(captured[0].url.params)
    second_params = dict(captured[1].url.params)

    assert first_params == {"strategy": "longest", "date_from": "2022-01-01"}
    assert second_params == {
        "strategy": "longest",
        "date_from": "2022-01-01",
        "continuation_key": "k1",
    }


def test_pagination_is_bounded(rsa_private_key_pem, monkeypatch):
    monkeypatch.setattr("services.banking.client.MAX_TRANSACTION_PAGES", 3)
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"transactions": [], "continuation_key": "always-more"})

    client = _client(rsa_private_key_pem, handler)

    with pytest.raises(PaginationLimitExceededError):
        list(client.iter_transactions("uid-1"))

    assert call_count == 3


# ---------------------------------------------------------------------------
# Errors (spec §B5) — branch on business code, never HTTP status
# ---------------------------------------------------------------------------


def test_error_raised_with_business_code_not_http_status(rsa_private_key_pem):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"message": "Session is expired", "code": 401, "error": "EXPIRED_SESSION"}
        )

    client = _client(rsa_private_key_pem, handler)

    with pytest.raises(SessionInvalidError) as exc_info:
        client.get_session("sess-1")

    assert exc_info.value.code == "EXPIRED_SESSION"


def test_same_http_status_different_business_code_is_a_different_family(rsa_private_key_pem):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "Unauthorized access", "code": 401, "error": "UNAUTHORIZED_ACCESS"},
        )

    client = _client(rsa_private_key_pem, handler)

    with pytest.raises(BankingApiError) as exc_info:
        client.get_session("sess-1")

    assert exc_info.value.code == "UNAUTHORIZED_ACCESS"
    assert not isinstance(exc_info.value, SessionInvalidError)


def test_wrong_transactions_period_surfaces_earliest_allowed_date(rsa_private_key_pem):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "You can not request transactions more than 90 days in the past",
                "code": 422,
                "error": "WRONG_TRANSACTIONS_PERIOD",
                "detail": "Earliest allowed date_from is 2026-05-19",
            },
        )

    client = _client(rsa_private_key_pem, handler)

    with pytest.raises(InvalidPeriodError) as exc_info:
        list(client.iter_transactions("uid-1", date_from=date(2022, 1, 1)))

    assert exc_info.value.earliest_allowed_date == date(2026, 5, 19)


# ---------------------------------------------------------------------------
# PSU context headers (spec §B2) — all or nothing
# ---------------------------------------------------------------------------


def test_psu_headers_sent_all_together_when_context_provided(rsa_private_key_pem):
    captured = []
    psu_context = {
        "Psu-Ip-Address": "203.0.113.5",
        "Psu-User-Agent": "Mozilla/5.0",
        "Psu-Accept": "text/html",
        "Psu-Accept-Charset": "utf-8",
        "Psu-Accept-Encoding": "gzip",
        "Psu-Accept-Language": "fr-FR",
        "Psu-Referer": "https://capitalview.app/banking",
        "Psu-Geo-Location": "48.8566,2.3522",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"balances": []})

    client = _client(rsa_private_key_pem, handler, psu_context=psu_context)
    client.get_balances("uid-1")

    sent = captured[0].headers
    for name, value in psu_context.items():
        assert sent[name] == value


def test_no_psu_headers_sent_when_context_absent(rsa_private_key_pem):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"balances": []})

    client = _client(rsa_private_key_pem, handler)
    client.get_balances("uid-1")

    sent = captured[0].headers
    assert not any(name.lower().startswith("psu-") for name in sent)


# ---------------------------------------------------------------------------
# Factory (ruling R2)
# ---------------------------------------------------------------------------


def test_build_client_returns_enable_banking_client(rsa_private_key_pem):
    client = build_client("app-1", rsa_private_key_pem)
    assert isinstance(client, EnableBankingClient)


# ---------------------------------------------------------------------------
# Real captured payloads — skip cleanly, vendor-docs/ lives outside this repo
# ---------------------------------------------------------------------------


def _page(transactions: list[dict], page_size: int = 100):
    for i in range(0, len(transactions), page_size):
        yield transactions[i : i + page_size]


@pytest.mark.parametrize(
    "filename,expected_count",
    [("tx_courant.json", 297), ("tx_carte.json", 202)],
)
def test_iter_transactions_replays_real_captured_export(
    rsa_private_key_pem, filename, expected_count
):
    path = VENDOR_SPIKE_DIR / filename
    if not path.exists():
        pytest.skip(f"{path} not available outside the vendor-docs checkout")

    all_transactions = json.loads(path.read_text())
    pages = list(_page(all_transactions))

    def handler(request: httpx.Request) -> httpx.Response:
        idx = handler.calls
        handler.calls += 1
        body = {"transactions": pages[idx]}
        if idx < len(pages) - 1:
            body["continuation_key"] = f"page-{idx + 1}"
        return httpx.Response(200, json=body)

    handler.calls = 0

    client = _client(rsa_private_key_pem, handler)
    result = list(client.iter_transactions("uid-1"))

    assert len(result) == expected_count == len(all_transactions)
    assert result == all_transactions
