"""Note routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User, Note
from services.auth import get_current_user, get_master_key
from dtos import NoteCreate, NoteUpdate, NoteReorder, NoteResponse
from services.note import (
    create_note,
    update_note,
    delete_note,
    get_note,
    get_user_notes,
    reorder_notes,
)

router = APIRouter(prefix="/notes", tags=["Notes"])


@router.post("", response_model=NoteResponse, status_code=201)
def create_entry(
    note_data: NoteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new note."""
    return create_note(session, note_data, current_user.uuid, master_key)


@router.get("", response_model=list[NoteResponse])
def get_all(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all notes for current user."""
    return get_user_notes(session, current_user.uuid, master_key)


@router.put("/reorder", response_model=list[NoteResponse])
def reorder(
    data: NoteReorder,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Reorder notes by providing an ordered list of note IDs."""
    return reorder_notes(session, data.note_ids, current_user.uuid, master_key)


@router.get("/{note_id}", response_model=NoteResponse)
def get_entry(
    note_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a specific note."""
    note = get_note(session, note_id, current_user.uuid, master_key)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.put("/{note_id}", response_model=NoteResponse)
def update_entry(
    note_id: str,
    note_data: NoteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a note."""
    existing = get_note(session, note_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Note not found")
    
    note_model = session.get(Note, note_id)
    return update_note(session, note_model, note_data, master_key)


@router.delete("/{note_id}", status_code=204)
def delete_entry(
    note_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a note."""
    existing = get_note(session, note_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Note not found")
        
    delete_note(session, note_id)
    return None
