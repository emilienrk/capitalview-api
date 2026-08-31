import pytest
import sys
from unittest.mock import MagicMock
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool
from typing import Generator

# Mock yfinance before any imports
sys.modules["yfinance"] = MagicMock()

DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(name="engine", scope="session")
def engine_fixture():
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False
    )

    # SQLite ignores foreign keys unless asked, so ordering bugs a production
    # PostgreSQL would reject (deleting a bank_sessions row a bank_account_links
    # row still points at) used to pass here unnoticed.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Deduplicate indexes in metadata (fix for extend_existing=True with multiple imports)
    for table in SQLModel.metadata.tables.values():
        seen_indexes = set()
        unique_indexes = []
        for index in table.indexes:
            # Create a signature based on name and columns
            sig = (index.name, tuple(c.name for c in index.columns))
            if sig not in seen_indexes:
                seen_indexes.add(sig)
                unique_indexes.append(index)
        table.indexes = set(unique_indexes)

    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)

@pytest.fixture(name="session")
def session_fixture(engine) -> Generator[Session, None, None]:
    """
    Creates a new database session for a test using transaction rollback.
    """
    connection = engine.connect()
    transaction = connection.begin()
    
    session = Session(bind=connection)
    yield session
    
    session.close()
    # session.close() already rolls back the connection if the session was left
    # inactive by an uncaught flush error (e.g. a test asserting IntegrityError);
    # guard against the redundant rollback() SQLAlchemy warns about in that case.
    if transaction.is_active:
        transaction.rollback()
    connection.close()

@pytest.fixture(name="master_key")
def master_key_fixture() -> str:
    """Returns a dummy master key for encryption (valid base64)."""
    import base64
    return base64.b64encode(b"0" * 32).decode("utf-8") 


@pytest.fixture(name="rate_limit_engine", scope="session")
def rate_limit_engine_fixture():
    """A database of its own for the shared rate limiter.

    The limiter commits on its own Session — that is what makes the window
    shared across workers — and the test engine is a StaticPool, so those
    commits would land on the very connection the `session` fixture holds its
    rollback transaction on and destroy every test's isolation. In production
    the limiter genuinely holds a separate connection; this reproduces that.
    """
    from models import RateLimitHit

    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    RateLimitHit.__table__.create(engine, checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def isolate_rate_limiter(rate_limit_engine, monkeypatch):
    """Point the limiter at that database and empty its window between tests."""
    import sqlalchemy as sa
    import services.rate_limit as rate_limit
    from models import RateLimitHit

    def _clear():
        with Session(rate_limit_engine) as cleanup:
            cleanup.exec(sa.delete(RateLimitHit))
            cleanup.commit()

    monkeypatch.setattr(rate_limit, "get_engine", lambda: rate_limit_engine)
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def disable_auth_background_catchup(monkeypatch):
    """Prevent account-history background jobs from opening a real PostgreSQL connection in tests."""
    import routes.auth as auth_routes
    import routes.asset as asset_routes
    import services.account_history as account_history_service

    noop = lambda *args, **kwargs: None
    monkeypatch.setattr(auth_routes, "run_lazy_catchup", noop)
    monkeypatch.setattr(account_history_service, "run_lazy_catchup", noop)
    monkeypatch.setattr(account_history_service, "rebuild_account_history_from_date", noop)
    monkeypatch.setattr(asset_routes, "rebuild_account_history_from_date", noop)


def opt_into_open_banking(session, user_uuid: str, master_key: str) -> None:
    """Flip the open-banking opt-in for a user, as the settings screen would.

    Every route that opens or feeds a bank connection is refused without it, so
    any test driving that flow has to start here.
    """
    from services.settings import get_or_create_settings

    settings = get_or_create_settings(session, user_uuid, master_key)
    settings.open_banking_enabled = True
    session.add(settings)
    session.commit()
