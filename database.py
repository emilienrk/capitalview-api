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
    return create_engine(settings.database_url, echo=settings.debug)


def get_session():
    """Dependency for FastAPI to get a database session."""
    engine = get_engine()
    with Session(engine) as session:
        yield session


def init_db():
    """Initialize database tables (for development only)."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
