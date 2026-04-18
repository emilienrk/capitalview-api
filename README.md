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
