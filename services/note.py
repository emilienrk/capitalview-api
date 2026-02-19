"""Note service."""

from typing import List, Optional
from sqlmodel import Session, select

from models import Note
from dtos import NoteCreate, NoteUpdate, NoteResponse
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_note_to_response(note: Note, master_key: str) -> NoteResponse:
    """Decrypt and map Note to response DTO."""
    name = decrypt_data(note.name_enc, master_key)
    description = decrypt_data(note.description_enc, master_key)
    
    return NoteResponse(
        id=note.uuid,
        name=name,
        description=description,
        position=note.position,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def create_note(
    session: Session, 
    data: NoteCreate, 
    user_uuid: str, 
    master_key: str
) -> NoteResponse:
    """Create a new encrypted note."""
    user_bidx = hash_index(user_uuid, master_key)
    
    # Set position to max + 1 for ordering
    existing = session.exec(
        select(Note).where(Note.user_uuid_bidx == user_bidx)
    ).all()
    max_pos = max((n.position for n in existing), default=-1)
    
    name_enc = encrypt_data(data.name, master_key)
    desc_enc = encrypt_data(data.description or "", master_key)
    
    note = Note(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        description_enc=desc_enc,
        position=max_pos + 1,
    )
    
    session.add(note)
    session.commit()
    session.refresh(note)
    
    return _map_note_to_response(note, master_key)


def update_note(
    session: Session,
    note: Note,
    data: NoteUpdate,
    master_key: str
) -> NoteResponse:
    """Update an existing note."""
    if data.name is not None:
        note.name_enc = encrypt_data(data.name, master_key)
        
    if data.description is not None:
        note.description_enc = encrypt_data(data.description, master_key)
        
    session.add(note)
    session.commit()
    session.refresh(note)
    
    return _map_note_to_response(note, master_key)


def delete_note(
    session: Session,
    note_uuid: str
) -> bool:
    """Delete a note."""
    note = session.get(Note, note_uuid)
    if not note:
        return False
        
    session.delete(note)
    session.commit()
    return True


def get_note(
    session: Session,
    note_uuid: str,
    user_uuid: str,
    master_key: str
) -> Optional[NoteResponse]:
    """Get a single note."""
    note = session.get(Note, note_uuid)
    if not note:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if note.user_uuid_bidx != user_bidx:
        return None
        
    return _map_note_to_response(note, master_key)


def get_user_notes(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> List[NoteResponse]:
    """Get all notes for a user, ordered by position."""
    user_bidx = hash_index(user_uuid, master_key)
    
    notes = session.exec(
        select(Note).where(Note.user_uuid_bidx == user_bidx).order_by(Note.position)
    ).all()
    
    return [_map_note_to_response(n, master_key) for n in notes]


def reorder_notes(
    session: Session,
    note_ids: List[str],
    user_uuid: str,
    master_key: str,
) -> List[NoteResponse]:
    """Reorder notes by updating position based on provided order."""
    user_bidx = hash_index(user_uuid, master_key)

    notes = session.exec(
        select(Note).where(Note.user_uuid_bidx == user_bidx)
    ).all()
    note_map = {n.uuid: n for n in notes}

    for idx, nid in enumerate(note_ids):
        if nid in note_map:
            note_map[nid].position = idx
            session.add(note_map[nid])

    session.commit()
    return get_user_notes(session, user_uuid, master_key)