from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.assignment import Assignment
from models.submission import Submission
from models.course import Note
from models.progress import StudentProgress
from models.auth import User
from api.deps import get_current_user, require_role
from services.progress_service import recompute_progress

router = APIRouter()


@router.get("/dashboard")
def student_dashboard(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    cc = user.course_code
    sid = user.id or 0
    assignments = session.exec(select(Assignment).where(Assignment.course_code == cc, Assignment.is_published == True)).all()
    submissions = session.exec(select(Submission).where(Submission.student_id == sid)).all()
    notes = session.exec(select(Note).where(Note.course_code == cc)).all()
    progress = session.exec(select(StudentProgress).where(StudentProgress.student_id == sid, StudentProgress.course_code == cc, StudentProgress.unit_number.is_(None), StudentProgress.assignment_id.is_(None))).first()
    if not progress:
        try:
            progress = recompute_progress(session, sid, cc)
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            progress = None
    return {
        "course_code": cc,
        "assignments": len(assignments),
        "submissions": len(submissions),
        "notes": len(notes),
        "progress": progress,
    }


@router.get("/progress")
def student_progress(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    sid = user.id or 0
    cc = user.course_code
    try:
        recompute_progress(session, sid, cc)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    try:
        return session.exec(select(StudentProgress).where(StudentProgress.student_id == sid, StudentProgress.course_code == cc)).all()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return []
