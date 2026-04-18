# AI Coding Agent Instructions (Backend)

## Overview

Personal wealth management and investment tracking application allowing users to:
- Track cash flows (inflows/outflows).
- Monitor investment evolution (Crypto, Stock accounts, PEA, Real Estate, etc.).
- Document investment strategies and personal notes.
- Visualize global wealth distribution and performance.

**Security**: All sensitive data must be encrypted (client and/or server side).

---

## Tech Stack & Architecture

### Backend
- **Framework**: FastAPI (Python)
- **ORM**: SQLModel
- **API**: RESTful JSON API

### Database
- **DBMS**: PostgreSQL
- **Migrations**: Alembic

---

## Folder Structure

```
capitalview-api/
├── .github/
│   └── copilot-instructions.md
├── main.py
├── config.py
├── database.py
├── models/
├── routes/
├── services/
└── alembic/
```

---

## Code Conventions

### General
- **Language**: English (variables, functions, classes, comments).

### Backend (Python / FastAPI)
- Follow **PEP 8**.
- Use Python **Type Hints**.
- Structure routes by domain.
- Business logic should be in `services/`.
- Use **SQLModel** for models and validation.

### Database
- Tables: **snake_case** plural (`investments`).
- Columns: **snake_case** (`created_at`).
- Mandatory columns: `id`, `created_at`, `updated_at`.

---

## AI Agent Rules

1. **Always use Python Type Hints**.
2. **Strictly type** all functions and variables.
3. **Never store** sensitive data in plain text.
4. **Keep it simple**: Favor simple, maintainable code over complex abstractions.
5. **Comment** complex logic in English.