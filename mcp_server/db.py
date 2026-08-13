"""Database access for the MCP layer.

MCP requests do not go through FastAPI's routing, so they cannot use the
``Depends(get_session)`` injection the REST routes rely on. Every entry point
here opens its own short-lived session instead — and does it through this single
function, so there is one place to point at a test engine rather than one per
call site.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session

from database import get_engine


@contextmanager
def open_session() -> Iterator[Session]:
    """Open a database session scoped to one MCP request."""
    with Session(get_engine()) as session:
        yield session
