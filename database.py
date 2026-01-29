"""
Database configuration and engine setup.
"""
from sqlmodel import SQLModel, create_engine, Session
from functools import lru_cache

from .config import get_settings


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
    from .models import *  # noqa: F401, F403 - Import all models to register them
    
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
