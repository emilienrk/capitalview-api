"""Note schemas."""

from datetime import datetime
from pydantic import BaseModel


class NoteCreate(BaseModel):
    """Create a note."""
    name: str
    description: str | None = None


class NoteUpdate(BaseModel):
    """Update a note."""
    name: str | None = None
    description: str | None = None


class NoteReorder(BaseModel):
    """Reorder notes."""
    note_ids: list[str]  # ordered list of note UUIDs


class NoteResponse(BaseModel):
    """Note response."""
    model_config = {"from_attributes": True}
    
    id: str
    name: str
    description: str | None = None
    position: int = 0
    created_at: datetime
    updated_at: datetime
