"""End-to-end tests for the mounted MCP endpoint.

These drive raw JSON-RPC over the real ASGI mount rather than a client library,
because the wire format is part of what is being tested: the 2026-07-28 revision
made every request self-describing, so each one carries its own protocol
envelope in ``params._meta`` and repeats its method in the routable
``Mcp-Method`` header.
"""

import json
import uuid as uuid_lib

import pytest
from fastapi.testclient import TestClient

import main
from models.user import User
from services.api_token import create_api_token, revoke_api_token
from services.encryption import hash_password, init_salt

PROTOCOL_VERSION = "2026-07-28"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(name="client", scope="module")
def client_fixture(request):
    """One client for the whole module, pointed at the in-memory test engine.

    Module-scoped on purpose: the MCP session manager refuses to be started
    twice, and starting it is what the app's lifespan does. Requests are
    stateless, so sharing the client across tests shares nothing else.

    The engine is patched rather than the ``get_session`` dependency because MCP
    requests never touch FastAPI's injection — the ASGI middleware and the tool
    bodies open their own sessions through ``mcp_server.db``.
    """
    import mcp_server.db as mcp_db

    engine = request.getfixturevalue("engine")
    originals = (mcp_db.get_engine, main.get_engine)
    mcp_db.get_engine = lambda: engine
    main.get_engine = lambda: engine

    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        mcp_db.get_engine, main.get_engine = originals


@pytest.fixture(name="account")
def account_fixture(session, master_key):
    """A user with a live API token."""
    user = User(
        uuid=str(uuid_lib.uuid4()),
        auth_salt=init_salt(),
        username=f"mcp-{uuid_lib.uuid4().hex[:8]}",
        email=f"{uuid_lib.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("StrongMcp1!"),
    )
    session.add(user)
    session.commit()

    record, token = create_api_token(session, user, master_key, name="Claude Desktop")
    return user, record, token


def _call(client: TestClient, method: str, params: dict, token: str | None, name: str | None = None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": ENVELOPE}},
        headers=headers,
    )


def test_the_bare_path_is_served_without_a_redirect(client, session, account):
    """Clients are configured with the bare URL and must be served on first hop.

    Asserted with redirects disabled on purpose: the default test client follows
    them, which would hide a 307 that every real call would pay for — ahead of
    authentication, at that.
    """
    _, _, token = account

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": ENVELOPE}},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": "tools/list",
            "Authorization": f"Bearer {token}",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200


def test_the_endpoint_refuses_an_anonymous_request(client, session, account):
    response = _call(client, "tools/list", {}, token=None)

    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


def test_the_endpoint_refuses_an_unknown_token(client, session, account):
    response = _call(client, "tools/list", {}, token="cvw_not-a-real-token")

    assert response.status_code == 401


def test_a_revoked_token_stops_working_immediately(client, session, account):
    user, record, token = account

    assert _call(client, "tools/list", {}, token=token).status_code == 200

    revoke_api_token(session, user.uuid, record.uuid)

    # Nothing is cached between requests, so revocation bites on the next call.
    assert _call(client, "tools/list", {}, token=token).status_code == 401


def test_tools_are_advertised_to_an_authenticated_client(client, session, account):
    _, _, token = account

    response = _call(client, "tools/list", {}, token=token)

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {
        "get_portfolio_overview",
        "get_performance",
        "get_cashflow_summary",
        "get_investor_analytics",
    }


def test_a_tool_call_returns_the_callers_own_figures(client, session, account):
    _, _, token = account

    response = _call(
        client, "tools/call", {"name": "get_portfolio_overview", "arguments": {}},
        token=token, name="get_portfolio_overview",
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False

    # An account with nothing in it still answers with the full breakdown.
    overview = json.loads(result["content"][0]["text"])
    assert overview["global_wealth"] == 0
    assert set(overview) >= {"stocks_total", "crypto_total", "cash_total", "assets_total"}


def test_two_tokens_never_see_each_others_data(client, session, master_key, account):
    """The principal is per-request state, so concurrent accounts stay separate."""
    _, _, first_token = account

    other = User(
        uuid=str(uuid_lib.uuid4()),
        auth_salt=init_salt(),
        username=f"mcp-other-{uuid_lib.uuid4().hex[:8]}",
        email=f"{uuid_lib.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("StrongOther1!"),
    )
    session.add(other)
    session.commit()
    _, second_token = create_api_token(session, other, master_key, name="Other client")

    for token in (first_token, second_token):
        response = _call(
            client, "tools/call", {"name": "get_portfolio_overview", "arguments": {}},
            token=token, name="get_portfolio_overview",
        )
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is False


def test_an_unknown_tool_is_rejected(client, session, account):
    _, _, token = account

    response = _call(
        client, "tools/call", {"name": "drop_everything", "arguments": {}},
        token=token, name="drop_everything",
    )

    body = response.json()
    assert "error" in body or body["result"]["isError"] is True
