"""End-to-end tests for the mounted MCP endpoint.

These drive raw JSON-RPC over the real ASGI mount rather than a client library,
because the wire format is part of what is being tested: the 2026-07-28 revision
made every request self-describing, so each one carries its own protocol
envelope in ``params._meta`` and repeats its method in the routable
``Mcp-Method`` header.
"""

import datetime
import json
import uuid as uuid_lib
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

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


def test_money_reaches_the_model_as_numbers_from_every_tool():
    """One tool must not report euros as a string while another reports floats.

    The overview read models cast to float; the analytics report keeps Decimal,
    which the default serialiser renders as a string to protect precision. A
    model comparing figures across tools would have no way to notice.
    """
    from decimal import Decimal

    from mcp_server.tools import _jsonable

    normalised = _jsonable(
        {
            "cash_total": Decimal("12500.55"),
            "blocks": [{"fees": Decimal("12.30"), "label": "12.30"}],
            "period_start": datetime.date(2026, 8, 13),
        }
    )

    assert normalised["cash_total"] == 12500.55
    assert isinstance(normalised["cash_total"], float)
    assert isinstance(normalised["blocks"][0]["fees"], float)
    # A genuine string that merely looks numeric must survive untouched.
    assert normalised["blocks"][0]["label"] == "12.30"
    assert normalised["period_start"] == "2026-08-13"


def _curve(days: int) -> list[dict]:
    """A daily wealth curve, oldest first, as build_wealth_history returns it."""
    start = datetime.date(2026, 1, 1)
    return [
        {
            "snapshot_date": start + datetime.timedelta(days=offset),
            "total_wealth": Decimal(offset),
            "stock_value": Decimal(offset),
            "crypto_value": Decimal(0),
            "bank_value": Decimal(0),
            "assets_value": Decimal(0),
        }
        for offset in range(days)
    ]


def test_a_long_history_is_summarised_instead_of_flooding_the_conversation():
    """Three years of daily points would spend the budget on a single call."""
    from mcp_server.tools import MAX_HISTORY_POINTS, _downsample, _resolve_granularity

    assert _resolve_granularity("auto", days=90) == "day"
    assert _resolve_granularity("auto", days=365) == "week"
    assert _resolve_granularity("auto", days=1095) == "month"
    # An explicit choice is honoured over the automatic one.
    assert _resolve_granularity("month", days=30) == "month"

    monthly = _downsample(_curve(1095), "month")
    assert len(monthly) == 36
    assert len(_downsample(_curve(1095), "day")) == MAX_HISTORY_POINTS


def test_each_period_reports_its_closing_value_not_a_total():
    """Wealth is a level: summing a week's snapshots would invent money."""
    from mcp_server.tools import _downsample

    weekly = _downsample(_curve(14), "week")

    # 2026-01-01 is a Thursday, so the first ISO week closes on the 4th.
    assert weekly[0]["snapshot_date"] == datetime.date(2026, 1, 4)
    assert weekly[0]["total_wealth"] == Decimal(3)


def test_the_window_is_measured_from_the_data_not_from_today():
    """A portfolio whose history stopped must still answer, not return nothing."""
    from mcp_server.tools import _within_days

    stale = _curve(400)  # ends in early 2027, long before "now"

    assert len(_within_days(stale, 30)) == 30
    assert _within_days(stale, 30)[-1]["snapshot_date"] == stale[-1]["snapshot_date"]


def test_a_malformed_date_bound_is_refused_rather_than_guessed():
    from mcp_server.tools import _as_date

    assert _as_date(None) is None
    assert _as_date("2026-03-01") == datetime.date(2026, 3, 1)
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        _as_date("01/03/2026")


def test_an_unknown_account_type_is_refused_rather_than_answered_empty():
    """"bank" is the obvious guess, and an empty ledger would read as an answer."""
    from mcp_server.tools import _as_account_type

    assert _as_account_type("all") == "all"
    with pytest.raises(ToolError, match="'stock', 'crypto' ou 'all'"):
        _as_account_type("bank")


def test_an_unknown_flow_type_is_refused_rather_than_ignored():
    from mcp_server.tools import _as_flow_type

    assert _as_flow_type(None) is None
    assert _as_flow_type("inflow") == "inflow"
    with pytest.raises(ToolError, match="'inflow' ou 'outflow'"):
        _as_flow_type("revenus")


def test_asking_for_bank_movements_says_so_instead_of_reporting_none(client, session, account):
    """The refusal has to reach the caller as an error, not as an empty ledger."""
    _, _, token = account

    response = _call(
        client, "tools/call",
        {"name": "list_recent_transactions", "arguments": {"account_type": "bank"}},
        token=token, name="list_recent_transactions",
    )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "account_type" in result["content"][0]["text"]


def test_a_malformed_overview_date_is_refused_like_any_other_bound(client, session, account):
    _, _, token = account

    response = _call(
        client, "tools/call",
        {"name": "get_portfolio_overview", "arguments": {"date": "hier"}},
        token=token, name="get_portfolio_overview",
    )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "YYYY-MM-DD" in result["content"][0]["text"]


def test_a_long_projection_is_reported_in_yearly_milestones():
    """Ten years of monthly points is noise; the horizon itself is the answer."""
    from mcp_server.tools import _as_months, _projection_points, _projection_step

    assert _projection_step(24) == 1
    assert _projection_step(120) == 12
    assert _as_months(0) == 1
    assert _as_months(10_000) == 600

    class _Point:
        def __init__(self, index):
            self.date = datetime.date(2026, 1, 1) + datetime.timedelta(days=30 * index)
            self.total_value = float(index)
            self.asset_values = {}

    curve = [_Point(index) for index in range(1, 122)]  # 121 months, not a round year
    kept = _projection_points(curve, 12)

    assert len(kept) == 11  # ten yearly marks, plus the horizon
    assert kept[-1]["total_value"] == 121.0


def test_a_projection_states_the_contribution_it_assumed(client, session, account):
    """The curve is worthless to a reader who cannot see what it assumed."""
    _, _, token = account

    response = _call(
        client, "tools/call",
        {"name": "project_wealth", "arguments": {"months": 24, "monthly_bank": 500}},
        token=token, name="project_wealth",
    )

    result = response.json()["result"]
    assert result["isError"] is False

    body = json.loads(result["content"][0]["text"])
    assert body["months"] == 24
    assert body["assumptions"]["assets"]["BANK"]["monthly_injection"] == 500


def test_an_explicit_rate_and_contribution_are_honoured_to_the_cent(client, session, account):
    """The arithmetic is checkable, so check it rather than trusting the shape.

    500 a month for 12 months at 0% is 6 000 — contributions land at the end of
    each month and the last one earns nothing, which is the convention the tool
    reports in `contribution_timing`.
    """
    _, _, token = account

    response = _call(
        client, "tools/call",
        {
            "name": "project_wealth",
            "arguments": {"months": 12, "monthly_bank": 500, "annual_return_bank": 0},
        },
        token=token, name="project_wealth",
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])

    assert body["assumptions"]["contribution_timing"] == "end_of_month"
    assert body["assumptions"]["assets"]["BANK"]["annual_return_rate"] == 0
    assert body["points"][-1]["total_value"] == 6000

    # Nothing was earned, so every euro of it is contribution.
    assert body["outcome"] == {
        "starting_value": 0.0,
        "contributed": 6000.0,
        "growth": 0.0,
        "final_value": 6000.0,
        "total_invested": 6000.0,
        "growth_share": 0.0,
    }


def test_the_outcome_separates_what_was_paid_in_from_what_was_earned(client, session, account):
    """100 a month at 12% for 10 years: 12 000 paid in, ~11 200 earned.

    The closed form for an ordinary annuity at 12% nominal — 0.9489% monthly,
    compounded — lands just over 23 000. What matters is that the two halves are
    told apart and still add up.
    """
    _, _, token = account

    response = _call(
        client, "tools/call",
        {
            "name": "project_wealth",
            "arguments": {"months": 120, "monthly_bank": 100, "annual_return_bank": 0.12},
        },
        token=token, name="project_wealth",
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])
    outcome = body["outcome"]

    assert outcome["contributed"] == 12000
    assert 10_000 < outcome["growth"] < 12_000
    assert outcome["final_value"] == pytest.approx(
        outcome["starting_value"] + outcome["contributed"] + outcome["growth"], abs=0.01
    )
    assert outcome["growth_share"] == pytest.approx(
        outcome["growth"] / outcome["final_value"], abs=0.0001
    )


def test_every_point_adds_up_to_the_value_it_reports(client, session, account):
    """The split is only useful if it is exact at every step, not just the last."""
    _, _, token = account

    response = _call(
        client, "tools/call",
        {
            "name": "project_wealth",
            "arguments": {"months": 24, "monthly_bank": 250, "annual_return_bank": 0.05},
        },
        token=token, name="project_wealth",
    )

    points = json.loads(response.json()["result"]["content"][0]["text"])["points"]

    assert points[0]["contributed"] == 0  # nothing paid in before the first month
    for point in points:
        assert point["total_value"] == pytest.approx(
            point["starting_value"] + point["contributed"] + point["growth"], abs=0.01
        )


def test_the_assumptions_carry_the_measurement_behind_them(client, session, account):
    """A rate with no provenance invites being quoted as fact."""
    _, _, token = account

    response = _call(
        client, "tools/call", {"name": "project_wealth", "arguments": {"months": 12}},
        token=token, name="project_wealth",
    )

    stock = json.loads(response.json()["result"]["content"][0]["text"])["assumptions"]["assets"]["STOCK"]

    # An empty account has nothing to measure, and says so rather than
    # reporting a rate it cannot support.
    assert stock["basis"]["return"] == "unavailable"
    assert stock["basis"]["contribution"] == "unavailable"
    assert stock["annual_return_rate"] == 0.0


def test_a_projection_that_ends_below_its_contributions_says_so(client, session, account, monkeypatch):
    """An empty curve reads as "unavailable" when it means "you end up down"."""
    from dtos.projection import (
        ProjectionAssetParametersUsed,
        ProjectionParametersUsed,
        ProjectionResponse,
    )
    from models.enums import AccountCategory

    _, _, token = account
    losing = ProjectionResponse(
        parameters_used=ProjectionParametersUsed(
            months_to_project=120,
            assets={
                AccountCategory.STOCK: ProjectionAssetParametersUsed(
                    monthly_injection=100.0, return_rate=-0.4
                )
            },
        ),
        data=[],
    )
    monkeypatch.setattr("mcp_server.tools.build_projection", lambda *a, **k: losing)

    response = _call(
        client, "tools/call", {"name": "project_wealth", "arguments": {}},
        token=token, name="project_wealth",
    )

    body = json.loads(response.json()["result"]["content"][0]["text"])
    assert body["ends_below_contributions"] is True
    assert "sous la somme des versements" in body["note"]
    assert body["points"] == []


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
        "get_observed_flows",
        "get_wealth_history",
        "list_recent_transactions",
        "project_wealth",
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


def test_the_overview_reports_cost_basis_alongside_value(client, session, account):
    """Holdings without a cost basis cannot say whether the user is up or down."""
    _, _, token = account

    response = _call(
        client, "tools/call", {"name": "get_portfolio_overview", "arguments": {"details": True}},
        token=token, name="get_portfolio_overview",
    )

    assert response.status_code == 200
    overview = json.loads(response.json()["result"]["content"][0]["text"])
    assert set(overview) >= {
        "invested_total",
        "stocks_invested",
        "crypto_invested",
        "unrealized_profit_loss",
    }


def test_observed_flows_report_transfers_apart_from_spending(client, session, account, master_key):
    """Money moved between the user's own accounts is neither income nor
    spending — but it is not hidden either: it is counted and named."""
    from datetime import date as _date

    from dtos.bank import BankAccountCreate
    from models.enums import BankAccountType
    from services.bank import create_bank_account
    from services.banking.transactions import store_transactions

    user, _, token = account
    accounts = [
        create_bank_account(
            session,
            BankAccountCreate(name=name, balance="0", account_type=kind),
            user.uuid, master_key,
        ).id
        for name, kind in (("Courant", BankAccountType.CHECKING),
                           ("Livret A", BankAccountType.LIVRET_A))
    ]
    today = _date.today()
    day = today.replace(day=1).isoformat()
    for account_id, direction, amount, ref in (
        (accounts[0], "DBIT", "400.00", "to-savings"),
        (accounts[1], "CRDT", "400.00", "from-current"),
        (accounts[0], "DBIT", "30.00", "groceries"),
    ):
        store_transactions(session, master_key, account_id, [{
            "entry_reference": ref,
            "transaction_amount": {"currency": "EUR", "amount": amount},
            "credit_debit_indicator": direction,
            "status": "BOOK",
            "booking_date": day,
            "remittance_information": ["peu importe"],
        }])

    response = _call(
        client, "tools/call", {"name": "get_observed_flows", "arguments": {"months": 1}},
        token=token, name="get_observed_flows",
    )

    assert response.status_code == 200
    flows = json.loads(response.json()["result"]["content"][0]["text"])
    assert flows["outflow"] == 30.0
    assert flows["internal_transfers_excluded"] == 1
    assert flows["internal_transfers_amount"] == 400.0
    assert sorted(flows["account_names"]) == ["Courant", "Livret A"]


def test_the_wealth_curve_answers_on_an_empty_account(client, session, account):
    """No history is an empty series, not an error the agent has to interpret."""
    _, _, token = account

    response = _call(
        client, "tools/call", {"name": "get_wealth_history", "arguments": {"days": 30}},
        token=token, name="get_wealth_history",
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["granularity"] == "day"
    assert payload["points"] == []


def test_listing_transactions_answers_on_an_empty_account(client, session, account):
    _, _, token = account

    response = _call(
        client, "tools/call",
        {"name": "list_recent_transactions", "arguments": {"account_type": "all"}},
        token=token, name="list_recent_transactions",
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"count": 0, "transactions": []}


def test_a_caller_cannot_lift_the_transaction_cap(client, session, account):
    """A limit argument is a request, not an instruction."""
    from mcp_server.tools import MAX_TRANSACTIONS

    _, _, token = account

    response = _call(
        client, "tools/call",
        {"name": "list_recent_transactions", "arguments": {"limit": 10_000}},
        token=token, name="list_recent_transactions",
    )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert MAX_TRANSACTIONS == 200


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
