import pytest
import sys
from unittest.mock import MagicMock
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
    transaction.rollback()
    connection.close()

@pytest.fixture(name="master_key")
def master_key_fixture() -> str:
    """Returns a dummy master key for encryption (valid base64)."""
    import base64
    return base64.b64encode(b"0" * 32).decode("utf-8") 
