from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlmodel import Session, select
from db.session import get_session
from models.assignment import Assignment
from models.auth import User
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.file_serve import file_response_for

router = APIRouter()


@router.get("/")
def list_assignments(course_code: str | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Assignment).where(Assignment.is_published == True)
    cc = course_code or user.course_code
    if user.role == "student":
        cc = user.course_code
    q = q.where(Assignment.course_code == cc)
    return session.exec(q).all()


@router.post("/")
def create_assignment(
    course_code: str = Form(...),
    unit_number: str | None = Form(None),
    title: str = Form(...),
    description: str | None = Form(None),
    max_marks: float = Form(20.0),
    due_date: str | None = Form(None),
    rubric_text: str | None = Form(None),
    file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "admin")),
):
    meta_path, meta_name, meta_ct, meta_sz = None, None, None, 0
    if file:
        m = save_upload(file, "assignments")
        meta_path, meta_name, meta_ct, meta_sz = m["file_path"], m["file_name"], m["content_type"], m["size_bytes"]
    ad = Assignment(
        course_code=course_code,
        unit_number=unit_number,
        title=title,
        description=description,
        max_marks=max_marks,
        due_date=date.fromisoformat(due_date) if due_date else None,
        rubric_text=rubric_text,
        file_path=meta_path,
        file_name=meta_name,
        created_by=user.id or 0,
        is_published=True,
    )
    session.add(ad)
    session.commit()
    session.refresh(ad)
    return ad


@router.get("/{assignment_id}/download")
def download_assignment(assignment_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    ad = session.get(Assignment, assignment_id)
    if not ad:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if user.role == "student" and ad.course_code != user.course_code:
        raise HTTPException(status_code=403, detail="Forbidden")
    return file_response_for(ad.file_path, ad.file_name, None)
