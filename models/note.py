"""
Note model (user notes).
"""
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from .user import User


class Note(SQLModel, table=True):
    """User notes."""
    __tablename__ = "notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(nullable=False)
    description: Optional[str] = Field(default=None)

    # Relationships
    user: Optional["User"] = Relationship(back_populates="notes")
