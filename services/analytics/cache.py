"""The finished analysis, kept between reads.

Building the investor report means decrypting every stock row and every daily
snapshot, backfilling the market history of every line held and the benchmark
(a network round trip per asset), and running thousands of permutation draws.
Behaviour moves on the scale of months, so paying that on every visit buys
nothing — the figures come back identical until a row changes.

So the payload is kept, following services/banking/transfer_patterns.py: one
encrypted row per user and kind, stamped with a digest of what it was built
from. The digest is cheap on purpose — counts and timestamps, nothing
decrypted — and any row added, rewritten or removed moves it, as does a change
of benchmark or plan. What the digest cannot see is the market: prices drift
without touching a single row of the user's. That is what the validity bounds:
past it the payload is rebuilt whatever the digest says, and the response
carries `computed_at` so the page can date what it shows and offer a rebuild.

Written on read, never by a job. Every user row is encrypted under a Master
Key the server never holds outside a request, so a scheduler could neither
compute a payload nor open one — a Redis or cron warm-up is not an option in
this architecture, and this row is the long-term cache instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from pydantic import BaseModel
from sqlmodel import Session, select

from config import get_settings
from models.analytics_cache import InvestorAnalyticsCache
from models.crypto import CryptoAccount, CryptoTransaction
from models.placement import PlacementAccount, PlacementEntry
from models.stock import StockAccount, StockTransaction
from models.user import UserSettings
from services.encryption import decrypt_data, encrypt_data, hash_index

KIND_INVESTOR = "investor"
KIND_YEARLY = "yearly"

# Bumped whenever what is derived changes shape or meaning, so every stored
# payload is rebuilt rather than served under a contract it no longer meets.
_VERSION = "2"


def cache_days() -> int:
    return max(int(get_settings().analytics_cache_days), 0)


def _ledger_signature(
    session: Session, user_bidx: str, master_key: str, account_model, row_model
) -> list:
    """Count and latest timestamps of one ledger, read without decrypting a row."""
    uuids = session.exec(
        select(account_model.uuid).where(account_model.user_uuid_bidx == user_bidx)
    ).all()
    if not uuids:
        return [0, None, None, []]
    # The account list is part of the signature in its own right: a deleted
    # account takes its rows along, and an empty new one changes nothing else.
    hashed = sorted(hash_index(uuid, master_key) for uuid in uuids)
    counted = session.exec(
        select(
            sa.func.count(),
            sa.func.max(row_model.updated_at),
            sa.func.max(row_model.created_at),
        ).where(row_model.account_id_bidx.in_(hashed))  # type: ignore[attr-defined]
    ).one()
    return [*counted, hashed]


def source_digest(session: Session, user_uuid: str, master_key: str, kind: str) -> str:
    """A fingerprint of every row and setting the payload of `kind` is derived from.

    The daily snapshots are left out on purpose: a nightly job appends one per
    day, which would move the digest every morning and turn the validity into
    the only thing the cache ever honoured. They are rebuilt from the very rows
    and prices this digest and the validity already cover.
    """
    user_bidx = hash_index(user_uuid, master_key)
    parts: list = [
        _VERSION,
        kind,
        _ledger_signature(session, user_bidx, master_key, StockAccount, StockTransaction),
    ]
    settings = session.exec(
        select(UserSettings.benchmark_asset_key, UserSettings.investment_plan_enc).where(
            UserSettings.user_uuid_bidx == user_bidx
        )
    ).first()
    benchmark, plan_blob = settings if settings else (None, None)
    parts.append(benchmark)
    if kind == KIND_INVESTOR:
        # The ciphertext, not the plan: a fresh nonce on every save means a
        # rewrite always moves the digest, which errs on the side of rebuilding.
        parts.append(plan_blob)
    if kind == KIND_YEARLY:
        parts.append(
            _ledger_signature(session, user_bidx, master_key, CryptoAccount, CryptoTransaction)
        )
        placements = session.exec(
            select(PlacementAccount.uuid).where(PlacementAccount.user_uuid_bidx == user_bidx)
        ).all()
        entries = [0, None, None]
        if placements:
            entries = list(
                session.exec(
                    select(
                        sa.func.count(),
                        sa.func.max(PlacementEntry.updated_at),
                        sa.func.max(PlacementEntry.created_at),
                    ).where(PlacementEntry.account_uuid.in_(placements))  # type: ignore[attr-defined]
                ).one()
            )
        parts.append([sorted(placements), entries])
    return hash_index(json.dumps(parts, default=str), master_key)


def _encode(value):
    """JSON for what the report carries: exact decimals, ISO dates."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not serialisable: {type(value).__name__}")


def cached_payload(
    session: Session,
    user_uuid: str,
    master_key: str,
    kind: str,
    model: type[BaseModel],
    build: Callable[[], dict],
    *,
    refresh: bool = False,
    now: datetime | None = None,
) -> dict:
    """The payload of `kind`, from the cache when it still holds, else rebuilt.

    `model` is the response DTO: a stored payload goes through it on the way
    back so that every Decimal and date comes out as the type the route and the
    MCP tool expect, not as the strings JSON kept them as. A stale or missing
    row is rebuilt by `build` and stored; `refresh` rebuilds regardless.

    Every returned payload carries `computed_at`, `valid_until` and `cached`.
    """
    now = now or datetime.now(UTC)
    days = cache_days()
    user_bidx = hash_index(user_uuid, master_key)
    digest = source_digest(session, user_uuid, master_key, kind) if days else None

    if days and not refresh:
        row = session.get(InvestorAnalyticsCache, (user_bidx, kind))
        if row is not None and row.source_bidx == digest and _aware(row.valid_until) > now:
            try:
                stored = json.loads(decrypt_data(row.content_enc, master_key))
                payload = model.model_validate(stored).model_dump()
            except Exception:
                payload = None  # unreadable row: rebuilt below, never served
            if payload is not None:
                payload["cached"] = True
                return payload

    payload = dict(build())
    payload["computed_at"] = now
    payload["valid_until"] = now + timedelta(days=days) if days else None
    payload["cached"] = False
    if not days:
        return payload

    content = encrypt_data(json.dumps(payload, default=_encode), master_key)
    row = session.get(InvestorAnalyticsCache, (user_bidx, kind))
    if row is None:
        session.add(
            InvestorAnalyticsCache(
                user_uuid_bidx=user_bidx,
                kind=kind,
                source_bidx=digest,
                content_enc=content,
                built_at=now,
                valid_until=payload["valid_until"],
            )
        )
    else:
        row.source_bidx = digest
        row.content_enc = content
        row.built_at = now
        row.valid_until = payload["valid_until"]
        session.add(row)
    session.commit()
    return payload


def _aware(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were stored in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
