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
