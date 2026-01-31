"""Note schemas."""

from typing import Optional

from pydantic import BaseModel


class NoteCreate(BaseModel):
    """Create a note."""
    name: str
    description: Optional[str] = None


class NoteUpdate(BaseModel):
    """Update a note."""
    name: Optional[str] = None
    description: Optional[str] = None


class NoteResponse(BaseModel):
    """Note response."""
    model_config = {"from_attributes": True}
    
    id: int
    name: str
    description: Optional[str] = None
