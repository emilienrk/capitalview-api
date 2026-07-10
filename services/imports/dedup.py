"""Duplicate detection for imports.

Data is encrypted at rest, so fingerprints are computed in memory after
decryption, scoped to the target account. A fingerprint is
(executed_at to the second, asset_key, type, normalized amount).
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import Session

Fingerprint = tuple[str, str, str, str]


def _norm_amount(value) -> str:
    try:
        return str(Decimal(str(value)).normalize())
    except (InvalidOperation, ValueError):
        return str(value)


def _norm_dt(value) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None).isoformat()
    # ISO string: truncate sub-second part
    return str(value)[:19]


def make_fingerprint(executed_at, asset_key: str, tx_type: str, amount) -> Fingerprint:
    return (_norm_dt(executed_at), str(asset_key), str(tx_type), _norm_amount(amount))


def crypto_fingerprints(session: Session, account_id: str, master_key: str) -> set[Fingerprint]:
    """Fingerprints of every existing crypto transaction on the account."""
    from services.crypto_transaction import get_account_transactions

    result: set[Fingerprint] = set()
    for tx in get_account_transactions(session, account_id, master_key):
        tx_type = tx.type.value if hasattr(tx.type, "value") else str(tx.type)
        result.add(make_fingerprint(tx.executed_at, tx.asset_key, tx_type, tx.amount))
    return result


def stock_fingerprints(session: Session, account_id: str, master_key: str) -> set[Fingerprint]:
    """Fingerprints of every existing stock transaction on the account."""
    from services.stock_transaction import get_account_transactions

    result: set[Fingerprint] = set()
    for tx in get_account_transactions(session, account_id, master_key):
        tx_type = tx.type.value if hasattr(tx.type, "value") else str(tx.type)
        result.add(make_fingerprint(tx.executed_at, tx.asset_key, tx_type, tx.amount))
    return result


def bank_existing_dates(session: Session, account_id: str, master_key: str) -> set[date]:
    """Dates that already have a history snapshot for the bank account."""
    from sqlmodel import select

    from models.account_history import AccountHistory
    from services.encryption import hash_index

    account_bidx = hash_index(account_id, master_key)
    rows = session.exec(
        select(AccountHistory.snapshot_date).where(AccountHistory.account_id_bidx == account_bidx)
    ).all()
    return set(rows)
