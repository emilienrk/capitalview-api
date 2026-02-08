import pytest
from sqlmodel import Session

from services.note import (
    create_note,
    get_user_notes,
    get_note,
    update_note,
    delete_note,
)
from dtos.note import NoteCreate, NoteUpdate
from models.note import Note
from services.encryption import hash_index


def test_create_note(session: Session, master_key: str):
    user_uuid = "user_1"
    data = NoteCreate(name="My Strategy", description="Buy low, sell high")
    
    resp = create_note(session, data, user_uuid, master_key)
    
    assert resp.name == "My Strategy"
    assert resp.description == "Buy low, sell high"
    
    # Verify DB
    note_db = session.get(Note, resp.id)
    assert note_db is not None
    assert note_db.name_enc != "My Strategy"
    assert note_db.user_uuid_bidx == hash_index(user_uuid, master_key)


def test_get_user_notes(session: Session, master_key: str):
    user_1 = "user_1"
    user_2 = "user_2"
    
    create_note(session, NoteCreate(name="Note 1"), user_1, master_key)
    create_note(session, NoteCreate(name="Note 2"), user_1, master_key)
    create_note(session, NoteCreate(name="Note 3"), user_2, master_key)
    
    notes_u1 = get_user_notes(session, user_1, master_key)
    assert len(notes_u1) == 2
    
    notes_u2 = get_user_notes(session, user_2, master_key)
    assert len(notes_u2) == 1
    assert notes_u2[0].name == "Note 3"


def test_get_note(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_note(session, NoteCreate(name="Test Note"), user_uuid, master_key)
    
    # Success
    fetched = get_note(session, created.id, user_uuid, master_key)
    assert fetched.name == "Test Note"
    
    # Wrong User
    assert get_note(session, created.id, "user_2", master_key) is None
    
    # Not Found
    assert get_note(session, "non_existent", user_uuid, master_key) is None


def test_update_note(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_note(session, NoteCreate(name="Old Note"), user_uuid, master_key)
    note_db = session.get(Note, created.id)
    
    updated = update_note(session, note_db, NoteUpdate(name="New Note", description="Updated"), master_key)
    
    assert updated.name == "New Note"
    assert updated.description == "Updated"


def test_delete_note(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_note(session, NoteCreate(name="To Delete"), user_uuid, master_key)
    
    assert delete_note(session, created.id) is True
    assert session.get(Note, created.id) is None
    
    assert delete_note(session, "non_existent") is False
