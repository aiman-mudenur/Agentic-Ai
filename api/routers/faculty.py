from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.submission import Submission
from models.assignment import Assignment
from models.course import Note
from models.auth import User
from api.deps import get_current_user, require_role

router = APIRouter()


@router.get("/dashboard")
def faculty_dashboard(session: Session = Depends(get_session), user: User = Depends(require_role("faculty", "admin"))):
    cc = user.course_code
    assignments = session.exec(select(Assignment).where(Assignment.course_code == cc)).all()
    submissions = session.exec(select(Submission).where(Submission.course_code == cc)).all()
    notes = session.exec(select(Note).where(Note.course_code == cc)).all()
    return {
        "course_code": cc,
        "assignments_count": len(assignments),
        "submissions_count": len(submissions),
        "notes_count": len(notes),
        "pending_grading": sum(1 for s in submissions if s.status == "submitted"),
    }
