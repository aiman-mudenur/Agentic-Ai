from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from db.session import get_session
from models.course import Note
from models.auth import User
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.file_serve import file_response_for

router = APIRouter()


@router.get("/")
def list_notes(course_code: str | None = None, unit_number: str | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Note)
    cc = course_code or user.course_code
    if user.role == "student":
        cc = user.course_code
    q = q.where(Note.course_code == cc)
    if unit_number:
        q = q.where(Note.unit_number == unit_number)
    return session.exec(q).all()


@router.post("/upload")
def upload_note(
    course_code: str = Form(...),
    unit_number: str | None = Form(None),
    title: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "admin")),
):
    meta = save_upload(file, "notes")
    note = Note(
        course_code=course_code,
        unit_number=unit_number,
        title=title,
        file_path=meta["file_path"],
        file_name=meta["file_name"],
        content_type=meta["content_type"],
        size_bytes=meta["size_bytes"],
        uploaded_by=user.id or 0,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


@router.get("/{note_id}/download")
def download_note(note_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if user.role == "student" and note.course_code != user.course_code:
        raise HTTPException(status_code=403, detail="Forbidden")
    return file_response_for(note.file_path, note.file_name, note.content_type)
