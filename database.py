"""Database configuration and engine setup."""

from functools import lru_cache

from sqlmodel import Session, SQLModel, create_engine

from config import get_settings

# Import all models to register them with SQLModel
import models  # noqa: F401


@lru_cache
def get_engine():
    """Create and cache database engine."""
    settings = get_settings()
    # pre_ping so a connection the server closed under us (Postgres restart,
    # long idle) is discovered and replaced here rather than as a 500 on the
    # first request that draws it from the pool.
    return create_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def get_session():
    """Dependency for FastAPI to get a database session."""
    engine = get_engine()
    with Session(engine) as session:
        yield session


def init_db():
    """Initialize database tables (for development only)."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
