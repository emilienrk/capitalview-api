"""Note routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import Note
from schemas import NoteCreate, NoteUpdate, NoteResponse

router = APIRouter(prefix="/notes", tags=["Notes"])


@router.post("", response_model=NoteResponse, status_code=201)
def create_note(note_data: NoteCreate, session: Session = Depends(get_session)):
    """Create a new note."""
    new_note = Note(
        user_id=note_data.user_id,
        name=note_data.name,
        description=note_data.description,
    )
    session.add(new_note)
    session.commit()
    session.refresh(new_note)
    return NoteResponse.model_validate(new_note)


@router.get("", response_model=list[NoteResponse])
def get_all_notes(session: Session = Depends(get_session)):
    """Get all notes."""
    notes = session.exec(select(Note)).all()
    return [NoteResponse.model_validate(note) for note in notes]


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(note_id: int, session: Session = Depends(get_session)):
    """Get a specific note."""
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return NoteResponse.model_validate(note)


@router.get("/user/{user_id}", response_model=list[NoteResponse])
def get_user_notes(user_id: int, session: Session = Depends(get_session)):
    """Get all notes for a specific user."""
    notes = session.exec(select(Note).where(Note.user_id == user_id)).all()
    return [NoteResponse.model_validate(note) for note in notes]


@router.put("/{note_id}", response_model=NoteResponse)
def update_note(
    note_id: int, note_data: NoteUpdate, session: Session = Depends(get_session)
):
    """Update a note."""
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    # Update only provided fields
    if note_data.name is not None:
        note.name = note_data.name
    if note_data.description is not None:
        note.description = note_data.description
    
    session.add(note)
    session.commit()
    session.refresh(note)
    return NoteResponse.model_validate(note)


@router.delete("/{note_id}", status_code=204)
def delete_note(note_id: int, session: Session = Depends(get_session)):
    """Delete a note."""
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    session.delete(note)
    session.commit()
    return None
