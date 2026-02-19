"""Note schemas."""

from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel


class NoteCreate(BaseModel):
    """Create a note."""
    name: str
    description: Optional[str] = None


class NoteUpdate(BaseModel):
    """Update a note."""
    name: Optional[str] = None
    description: Optional[str] = None


class NoteReorder(BaseModel):
    """Reorder notes."""
    note_ids: List[str]  # ordered list of note UUIDs


class NoteResponse(BaseModel):
    """Note response."""
    model_config = {"from_attributes": True}
    
    id: str
    name: str
    description: Optional[str] = None
    position: int = 0
    created_at: datetime
    updated_at: datetime
