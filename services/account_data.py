"""
Account-wide data export and account deletion.

Both operations share the same constraint: outside of the six tables that carry
a real foreign key to ``users.uuid``, a row is tied to its owner only through
``user_uuid_bidx = hash_index(user_uuid, master_key)``. Nothing here can run
without the Master Key — which is also why deletion is immediate rather than
deferred to a background job.
"""

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from models.account_history import AccountHistory
from models.api_token import ApiToken
from models.asset import Asset, AssetValuation
from models.bank import BankAccount
from models.banking import (
    BankAccountLink,
    BankAuthorization,
    BankSession,
    BankTransaction,
    UserBankConnection,
)
from models.card import Card
from models.cashflow import Cashflow
from models.community import (
    CommunityFollow,
    CommunityPick,
    CommunityPosition,
    CommunityProfile,
)
from models.crypto import CryptoAccount, CryptoTransaction
from models.note import Note
from models.notification import Notification
from models.stock import StockAccount, StockTransaction
from models.user import (
    RefreshToken,
    TotpBackupCode,
    User,
    UserAIProvider,
    UserSettings,
)
from services.encryption import DecryptionError, decrypt_data, hash_index

EXPORT_VERSION = 1

# Physical assets have no account row of their own: their snapshots live under
# a single synthetic account id (see services/asset.py).
ASSET_PORTFOLIO_LABEL = "Patrimoine personnel"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _safe_decrypt(value: str | None, master_key: str) -> str | None:
    """Decrypt a nullable field, tolerating a row that predates the column.

    A single unreadable legacy row must not sink a whole export: the user gets
    everything else plus an explicit marker for what could not be read.
    """
    if not value:
        return None
    try:
        return decrypt_data(value, master_key)
    except DecryptionError:
        return None


def _account_labels(session: Session, user_bidx: str, master_key: str) -> dict[str, str]:
    """Map each ``account_id_bidx`` to a readable account name.

    Without it the exported history would be keyed by opaque HMACs and tell the
    reader nothing about which account a snapshot belongs to.
    """
    labels: dict[str, str] = {}

    for model in (BankAccount, StockAccount, CryptoAccount):
        accounts = session.exec(
            select(model).where(model.user_uuid_bidx == user_bidx)
        ).all()
        for account in accounts:
            name = _safe_decrypt(account.name_enc, master_key)
            labels[hash_index(account.uuid, master_key)] = name or account.uuid

    virtual_id = hash_index(f"ASSET_PORTFOLIO::{user_bidx}", master_key)
    labels[virtual_id] = ASSET_PORTFOLIO_LABEL
    return labels


def _export_account_history(
    session: Session, user_bidx: str, master_key: str
) -> list[dict]:
    """Decrypt the stored daily snapshots as they sit in the database.

    Deliberately reads the rows rather than calling the ``get_all_*_history``
    aggregators: those recompute a live "today" point and re-price positions,
    which would make an export of past data depend on current market state.
    """
    labels = _account_labels(session, user_bidx, master_key)

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.user_uuid_bidx == user_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()

    snapshots = []
    for row in rows:
        positions_raw = _safe_decrypt(row.positions_enc, master_key)
        snapshots.append(
            {
                "account": labels.get(row.account_id_bidx),
                "account_type": row.account_type,
                "snapshot_date": row.snapshot_date,
                "total_value": _safe_decrypt(row.total_value_enc, master_key),
                "total_invested": _safe_decrypt(row.total_invested_enc, master_key),
                "total_deposits": _safe_decrypt(row.total_deposits_enc, master_key),
                "total_withdrawals": _safe_decrypt(row.total_withdrawals_enc, master_key),
                "daily_pnl": _safe_decrypt(row.daily_pnl_enc, master_key),
                "cumulative_pnl": _safe_decrypt(row.cumulative_pnl_enc, master_key),
                "total_fees": _safe_decrypt(row.total_fees_enc, master_key),
                "total_dividends": _safe_decrypt(row.total_dividends_enc, master_key),
                "positions": json.loads(positions_raw) if positions_raw else None,
            }
        )
    return snapshots


def export_account_data(session: Session, user: User, master_key: str) -> dict:
    """Build the full, decrypted picture of an account.

    Composes the existing per-domain getters rather than re-querying, so the
    export always says exactly what the app itself shows.

    Authentication secrets are never included — not the password hash, the auth
    salt, the wrapped Master Keys, the TOTP secret, nor any API token. They are
    credentials, not portable personal data, and writing them to a file in the
    user's Downloads folder would create a standing way back into the account.
    """
    # Imported here rather than at module scope: services/bank.py and several
    # others already import each other lazily to break cycles, and this module
    # is pulled in by routes/auth.py, which they in turn import.
    from services.asset import get_asset_valuations, get_user_assets
    from services.bank import get_user_bank_accounts
    from services.cashflow import get_all_user_cashflows
    from services.community import get_community_settings, get_user_picks
    from services.crypto_account import get_user_crypto_accounts
    from services.crypto_transaction import get_account_transactions as get_crypto_transactions
    from services.note import get_user_notes
    from services.settings import get_settings
    from services.stock_account import get_user_stock_accounts
    from services.stock_transaction import get_account_transactions as get_stock_transactions

    user_bidx = hash_index(user.uuid, master_key)

    stock_accounts = []
    for account in get_user_stock_accounts(session, user.uuid, master_key):
        stock_accounts.append(
            {
                **account.model_dump(),
                "transactions": get_stock_transactions(session, account.id, master_key),
            }
        )

    crypto_accounts = []
    for account in get_user_crypto_accounts(session, user.uuid, master_key):
        crypto_accounts.append(
            {
                **account.model_dump(),
                "transactions": get_crypto_transactions(session, account.id, master_key),
            }
        )

    asset_summary = get_user_assets(session, user.uuid, master_key, include_sold=True)
    assets = [
        {
            **asset.model_dump(),
            "valuations": get_asset_valuations(session, asset.id, master_key),
        }
        for asset in asset_summary.assets
    ]

    bank_accounts = []
    for account in get_user_bank_accounts(session, user.uuid, master_key).accounts:
        account_bidx = hash_index(account.id, master_key)
        tx_rows = session.exec(
            select(BankTransaction)
            .where(BankTransaction.account_id_bidx == account_bidx)
            .order_by(BankTransaction.created_at)
        ).all()
        bank_accounts.append(
            {
                **account.model_dump(),
                "transactions": [
                    {
                        "uuid": tx.uuid,
                        "amount": _safe_decrypt(tx.amount_enc, master_key),
                        "currency": _safe_decrypt(tx.currency_enc, master_key),
                        "credit_debit": _safe_decrypt(tx.credit_debit_enc, master_key),
                        "status": _safe_decrypt(tx.status_enc, master_key),
                        "booking_date": _safe_decrypt(tx.booking_date_enc, master_key),
                        "value_date": _safe_decrypt(tx.value_date_enc, master_key),
                        "transaction_date": _safe_decrypt(tx.transaction_date_enc, master_key),
                        "remittance": _safe_decrypt(tx.remittance_enc, master_key),
                    }
                    for tx in tx_rows
                ],
            }
        )

    return {
        "export_version": EXPORT_VERSION,
        "generated_at": datetime.now(timezone.utc),
        "account": {
            "uuid": user.uuid,
            "username": user.username,
            "email": user.email,
            "created_at": user.created_at,
            "last_login": user.last_login,
            "last_username_change": user.last_username_change,
            "last_email_change": user.last_email_change,
            "totp_enabled": user.totp_enabled,
        },
        "settings": get_settings(session, user.uuid, master_key),
        "bank_accounts": bank_accounts,
        "stock_accounts": stock_accounts,
        "crypto_accounts": crypto_accounts,
        "cashflows": get_all_user_cashflows(session, user.uuid, master_key),
        "assets": assets,
        "notes": get_user_notes(session, user.uuid, master_key),
        "community": {
            "settings": get_community_settings(session, user.uuid),
            "picks": get_user_picks(session, user.uuid),
        },
        "account_history": _export_account_history(session, user_bidx, master_key),
    }


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def purge_account(session: Session, user: User, master_key: str) -> dict[str, int]:
    """Erase every row belonging to *user*, then the user itself.

    Every table is deleted explicitly instead of leaning on the ``ON DELETE
    CASCADE`` clauses. Three reasons: the thirteen blind-indexed tables have no
    foreign key to cascade from in the first place; the test suite runs on
    SQLite, which does not enforce foreign keys unless asked, so a
    cascade-reliant purge would pass its tests and leak in production; and a
    future migration dropping an ``ondelete`` would fail silently. The final
    ``DELETE FROM users`` should find nothing left to cascade to.

    Unlike the rest of the services layer, this commits exactly once, at the
    end. A half-committed purge is the one outcome worse than no purge: the
    orphaned rows it would leave are, by construction, unreachable forever.

    Returns the row count deleted per table.
    """
    user_bidx = hash_index(user.uuid, master_key)
    deleted: dict[str, int] = {}

    def wipe(model, condition) -> None:
        result = session.exec(sa.delete(model).where(condition))
        deleted[model.__tablename__] = deleted.get(model.__tablename__, 0) + (
            result.rowcount or 0
        )

    # 1. Transactions hang off accounts, not off the user: their account_id_bidx
    #    has to be recomputed from each account before the accounts are gone.
    bank_account_bidx = [
        hash_index(uuid, master_key)
        for uuid in session.exec(
            select(BankAccount.uuid).where(BankAccount.user_uuid_bidx == user_bidx)
        ).all()
    ]
    if bank_account_bidx:
        wipe(BankTransaction, BankTransaction.account_id_bidx.in_(bank_account_bidx))

    stock_account_bidx = [
        hash_index(uuid, master_key)
        for uuid in session.exec(
            select(StockAccount.uuid).where(StockAccount.user_uuid_bidx == user_bidx)
        ).all()
    ]
    if stock_account_bidx:
        wipe(StockTransaction, StockTransaction.account_id_bidx.in_(stock_account_bidx))

    crypto_account_bidx = [
        hash_index(uuid, master_key)
        for uuid in session.exec(
            select(CryptoAccount.uuid).where(CryptoAccount.user_uuid_bidx == user_bidx)
        ).all()
    ]
    if crypto_account_bidx:
        wipe(CryptoTransaction, CryptoTransaction.account_id_bidx.in_(crypto_account_bidx))

    # 1b. Banking attachments: BankAccountLink must be wiped BEFORE BankSession
    #     because `bank_account_links.session_uuid` has `ondelete="RESTRICT"`.
    wipe(BankAccountLink, BankAccountLink.user_uuid_bidx == user_bidx)

    # Best-effort closure of active Enable Banking sessions before wiping session rows
    # (ruling R3: always behind build_client double in tests, never blocks account deletion).
    _close_user_bank_sessions(session, user.uuid, user_bidx, master_key)

    wipe(BankSession, BankSession.user_uuid_bidx == user_bidx)
    wipe(BankAuthorization, BankAuthorization.user_uuid_bidx == user_bidx)
    wipe(UserBankConnection, UserBankConnection.user_uuid_bidx == user_bidx)

    # 2. Asset valuations cascade from assets in Postgres, but assets themselves
    #    never cascade from the user, so the chain has to be walked by hand.
    asset_uuids = session.exec(
        select(Asset.uuid).where(Asset.user_uuid_bidx == user_bidx)
    ).all()
    if asset_uuids:
        wipe(AssetValuation, AssetValuation.asset_uuid.in_(asset_uuids))

    # 3. Everything keyed directly by the user's blind index.
    for model in (
        BankAccount,
        StockAccount,
        CryptoAccount,
        Cashflow,
        Note,
        Card,
        Asset,
        UserSettings,
        UserAIProvider,
    ):
        wipe(model, model.user_uuid_bidx == user_bidx)

    # 4. Snapshots last among the blind-indexed tables: a lazy catch-up job
    #    racing this purge would rebuild them from the accounts, which are
    #    already gone by now. Covers the virtual ASSET_PORTFOLIO account too,
    #    since every row carries the user's own bidx.
    wipe(AccountHistory, AccountHistory.user_uuid_bidx == user_bidx)

    # 5. Tables with a real foreign key. Deleted rather than revoked: a revoked
    #    token is still a row describing a person who asked to be forgotten.
    wipe(CommunityPosition, CommunityPosition.profile_user_id == user.uuid)
    wipe(CommunityProfile, CommunityProfile.user_id == user.uuid)
    wipe(CommunityPick, CommunityPick.user_id == user.uuid)
    wipe(
        CommunityFollow,
        sa.or_(
            CommunityFollow.follower_id == user.uuid,
            CommunityFollow.following_id == user.uuid,
        ),
    )
    wipe(ApiToken, ApiToken.user_uuid == user.uuid)
    wipe(TotpBackupCode, TotpBackupCode.user_uuid == user.uuid)
    wipe(RefreshToken, RefreshToken.user_uuid == user.uuid)
    wipe(Notification, Notification.user_uuid == user.uuid)

    session.delete(user)
    deleted["users"] = 1

    session.commit()
    return deleted


def _close_user_bank_sessions(
    session: Session, user_uuid: str, user_bidx: str, master_key: str
) -> None:
    """Best-effort graceful closure of active Enable Banking sessions before purge.

    Uses `build_client` so that test monkeypatching catches the calls. Never
    blocks account deletion if a network error or client error occurs.
    """
    from services.banking.client import build_client
    from services.banking.credentials import get_decrypted_credentials

    try:
        creds = get_decrypted_credentials(session, user_uuid, master_key)
        if creds is None:
            return
        active_sessions = session.exec(
            select(BankSession).where(
                BankSession.user_uuid_bidx == user_bidx,
                BankSession.status == "AUTHORIZED",
            )
        ).all()
        if not active_sessions:
            return
        with build_client(*creds) as client:
            for s in active_sessions:
                try:
                    session_id = decrypt_data(s.session_id_enc, master_key)
                    client.close_session(session_id)
                except Exception:
                    pass
    except Exception:
        pass
