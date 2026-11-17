from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment, AuditLog
from models.course import Note
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.file_serve import file_response_for
from datetime import datetime, timezone

router = APIRouter()


@router.get("/")
def list_notes(course_code: str | None = None, unit_number: str | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Note).where(Note.is_current == True)
    
    if user.role == "student":
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id)).all()
        if not enrolled:
            return []
        q = q.where(Note.course_code.in_(enrolled))
        if course_code and course_code not in enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
        if course_code:
            q = q.where(Note.course_code == course_code)
    elif user.role == "faculty":
        if course_code:
            q = q.where(Note.course_code == course_code)
        else:
            # Faculty sees notes for courses they teach
            from models.assignment import Assignment
            taught = session.exec(select(Assignment.course_code).where(Assignment.created_by == user.id)).all()
            if not taught:
                return []
            q = q.where(Note.course_code.in_(taught))
    # HoD sees all
    
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
    user: User = Depends(require_role("faculty")),
):
    # Open upload: any faculty may upload notes for any course. The course
    # row is auto-created so it appears in the catalog for student enrollment.
    from models.course import Course
    if not session.exec(select(Course).where(Course.course_code == course_code)).first():
        session.add(Course(course_code=course_code, title=course_code, description=None))
        session.commit()
    meta = save_upload(file, "notes")
    
    # Check if updating existing note (versioning)
    existing_note = session.exec(select(Note).where(Note.course_code == course_code, Note.title == title, Note.is_current == True)).first()
    version = 1
    parent_id = None
    
    if existing_note:
        # Create new version
        version = existing_note.version + 1
        parent_id = existing_note.id
        # Mark old as not current
        existing_note.is_current = False
        session.add(existing_note)
    
    note = Note(
        course_code=course_code,
        unit_number=unit_number,
        title=title,
        file_path=meta["file_path"],
        file_name=meta["file_name"],
        content_type=meta["content_type"],
        size_bytes=meta["size_bytes"],
        uploaded_by=user.id,
        version=version,
        parent_note_id=parent_id,
        is_current=True,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    
    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="note_uploaded" if not existing_note else "note_updated",
        entity_type="note",
        entity_id=note.id,
        details=f"Faculty {user.email} {'uploaded' if not existing_note else 'updated'} note '{title}' v{version} for course {course_code}"
    ))
    session.commit()
    
    return note


@router.get("/{note_id}/download")
def download_note(note_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    if user.role == "student":
        enrolled = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == note.course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty":
        from models.assignment import Assignment
        teaches = session.exec(select(Assignment).where(Assignment.created_by == user.id, Assignment.course_code == note.course_code)).first()
        if not teaches:
            raise HTTPException(status_code=403, detail="You don't teach this course")
    
    return file_response_for(note.file_path, note.file_name, note.content_type)


@router.get("/{note_id}/versions")
def list_note_versions(note_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """List all versions of a note.

    Faculty and HoD can view versions for courses they teach/manage.
    Students can view versions only for notes in courses they are enrolled in.
    """
    note = session.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    if user.role == "student":
        enrolled = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == note.course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty":
        from models.assignment import Assignment
        teaches = session.exec(select(Assignment).where(Assignment.created_by == user.id, Assignment.course_code == note.course_code)).first()
        if not teaches:
            raise HTTPException(status_code=403, detail="You don't teach this course")
    # HoD/admin can view all versions

    # Get all versions in the chain
    versions = []
    current = note
    while current:
        versions.append({
            "id": current.id,
            "version": current.version,
            "title": current.title,
            "file_name": current.file_name,
            "uploaded_by": current.uploaded_by,
            "uploaded_at": current.uploaded_at.isoformat() if current.uploaded_at else None,
            "is_current": current.is_current,
        })
        if current.parent_note_id:
            current = session.get(Note, current.parent_note_id)
        else:
            break

    return versions