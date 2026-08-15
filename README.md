# CapitalView API

Backend API for CapitalView (FastAPI + SQLModel + PostgreSQL).

## Stack

- Python 3.14+
- FastAPI
- SQLModel / SQLAlchemy
- Alembic
- PostgreSQL

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn main:app --reload
```

API docs:

- http://localhost:8000/docs

## Database migrations

Always let Alembic generate the migration. `env.py` wires `target_metadata` to
`SQLModel.metadata`, so autogenerate works off the models:

```bash
alembic revision --autogenerate -m "add benchmark to user settings"
alembic upgrade head
```

Review the generated file before committing — autogenerate detects added and dropped
columns reliably, but not renames or data backfills.

**Never hand-write a revision id.** Alembic picks a random one and resolves
`down_revision` to the current head; invented ids do neither. Two migrations sharing a
revision id break `upgrade`, `history` and `heads` for the whole repository, not just the
offending file — and it has already happened here, because two hand-written ids followed
the same sequential pattern and collided. If you must edit a migration by hand, keep the
generated header intact.

To check the chain is sound:

```bash
alembic heads     # must print exactly one head
alembic history   # must be linear, no branch points
```

## MCP server

The API exposes its data to agent clients (Claude Desktop, Claude Code, …) over
MCP at `/mcp`, mounted inside this same FastAPI app. It runs **stateless**: the
2026-07-28 protocol revision dropped the `initialize` handshake and
protocol-level sessions, so any worker answers any request and there is nothing
to lose on redeploy.

Tools live in `mcp_server/tools.py` and are read-only. They call the same
`services/` functions the REST routes do, so an agent can never see more than
the account holder can.

`project_wealth` is the one tool that answers about the future rather than the
past, so both figures it assumes are measured rather than guessed
(`services/analytics/projection_basis.py`):

| Assumption | Default | Override |
| --- | --- | --- |
| Monthly contribution | Net external flows from the ledger, averaged over the months they span | `monthly_stock`, `monthly_crypto`, `monthly_bank` |
| Annual return | Annualised time-weighted return (TWR) | `annual_return_stock`, `annual_return_crypto`, `annual_return_bank` |

These defaults live in `generate_wealth_projection` itself, so the dashboard,
`POST /projection/calculate` and the MCP tool all project from the same numbers.

TWR rather than value-over-cost: the latter treats a euro deposited last week as
though it had compounded since day one, which understates any portfolio fed by
regular contributions — on a four-year ledger of 300/month it reports 4.0% where
the real return is 7.6%. Money-weighted return (XIRR) is the right lens on how
the investor did, and the wrong one to project with, since it bakes an entry
sequence into every future month.

A default that would not stand up is not substituted: under a year of history
yields no rate, and deposits landing on days the series does not price
disqualify it too — time-weighting cannot neutralise those, and reads them as
performance. The category then projects flat rather than compounding an
extrapolation. BANK is never derived at all: its balances move with salary and
spending, not performance, and its obvious contribution proxy is the same money
already counted as deposits elsewhere.

Every figure comes back in `assumptions` with its provenance and any warning
attached — a rate is `annualised_twr` over so many days, or it is `unavailable`
and says why.

The answer is decomposed rather than reduced to a single number, in `outcome`
and on every point of the curve:

```
starting_value + contributed + growth = total_value
```

so "81 237 € in ten years" reads as 60 000 paid in and 21 237 earned, with
`growth_share` (26%) saying how much of the result the portfolio produced rather
than the saver.

### How a token reaches encrypted data

Every user record is encrypted under a Master Key that the server never stores
in the clear, so a bearer token that only *identifies* a user would be able to
read nothing. An API token therefore carries the key as well: at mint time the
Master Key is wrapped under a KEK derived from the token itself (`mk_wrapped`),
the same mechanism the account recovery key uses. Only the token's HMAC is
persisted — a database dump can neither be replayed nor unwrapped.

That makes a token exactly as sensitive as the account password. Tokens are
named, listed with their last use, capped per account, optionally expiring, and
revocable with immediate effect (nothing is cached between requests). Changing
the password or recovering the account revokes all of them, like the browser
sessions: the Master Key survives both, so a token would otherwise keep reading
everything after the change meant to cut access off. Clients have to be handed a
new token afterwards.

The KEK uses HKDF rather than the Argon2id of the password path: a token is 32
bytes straight from the CSPRNG, so there is nothing to brute force, and a
memory-hard KDF would only add ~100 ms and 64 MB of RAM to every tool call.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_ENABLED` | `true` | Mount the endpoint at all |
| `MCP_PATH` | `/mcp` | Mount path |
| `MCP_PUBLIC_URL` | `http://localhost:8000/mcp` | Advertised to clients and shown in the settings UI |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated Host allow-list. Empty disables the SDK's DNS-rebinding protection, which targets localhost servers and would otherwise answer 421 behind a reverse proxy. |

### Connecting a client

Generate a token in Settings → Security → *Accès agents (MCP)*, then:

```json
{
  "mcpServers": {
    "capitalview": {
      "type": "http",
      "url": "https://api.<domaine>/mcp",
      "headers": { "Authorization": "Bearer cvw_…" }
    }
  }
}
```

## Tests

```bash
pytest -v --tb=short
```

## Docker

```bash
docker build -f Dockerfile.prod -t capitalview-api:local .
```

## CI/CD

- CI workflow: `.github/workflows/ci.yml`
- CD workflow (build/push image): `.github/workflows/cd.yml`

The deployment orchestration (compose, reverse proxy, VPS rollout) should live in a dedicated infra repository.

## Secrets

Runtime/deploy secrets should be managed outside the repository (GitHub Secrets, SOPS encrypted env files).
