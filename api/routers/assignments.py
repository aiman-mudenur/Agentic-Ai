from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment
from models.assignment import Assignment
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.file_serve import file_response_for

router = APIRouter()


@router.get("/")
def list_assignments(course_code: str | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Assignment).where(Assignment.is_published == True)
    
    if user.role == "student":
        # Only assignments for courses the student is enrolled in
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id)).all()
        if not enrolled:
            return []
        q = q.where(Assignment.course_code.in_(enrolled))
        if course_code:
            if course_code not in enrolled:
                raise HTTPException(status_code=403, detail="Not enrolled in this course")
            q = q.where(Assignment.course_code == course_code)
    elif user.role == "faculty":
        # Faculty sees assignments they created (or all in their courses if specified)
        if course_code:
            # Verify faculty teaches this course
            teaches = session.exec(select(Assignment).where(Assignment.created_by == user.id, Assignment.course_code == course_code)).first()
            if not teaches:
                raise HTTPException(status_code=403, detail="You don't teach this course")
            q = q.where(Assignment.course_code == course_code)
        else:
            # All assignments faculty created
            q = q.where(Assignment.created_by == user.id)
    else:
        # HoD sees all, can filter by course_code
        if course_code:
            q = q.where(Assignment.course_code == course_code)
    
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
    user: User = Depends(require_role("faculty")),
):
    # Open creation: any faculty may create for any course. The course row
    # is auto-created so it appears in the catalog for student enrollment.
    from models.course import Course
    if not session.exec(select(Course).where(Course.course_code == course_code)).first():
        session.add(Course(course_code=course_code, title=course_code, description=None))
        session.commit()
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
        created_by=user.id,
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
    
    if user.role == "student":
        enrolled = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == ad.course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty" and ad.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your assignment")
    
    return file_response_for(ad.file_path, ad.file_name, None)