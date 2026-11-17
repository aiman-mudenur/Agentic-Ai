from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment
from models.assignment import Assignment
from models.submission import Submission
from models.course import Note
from models.progress import StudentProgress
from api.deps import get_current_user, require_role
from services.progress_service import recompute_progress

router = APIRouter()


@router.get("/dashboard")
def student_dashboard(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    sid = user.id
    # Get enrolled courses
    enrollments = session.exec(select(Enrollment).where(Enrollment.student_id == sid)).all()
    course_codes = [e.course_code for e in enrollments]
    
    if not course_codes:
        return {
            "enrolled_courses": [],
            "assignments": 0,
            "submissions": 0,
            "notes": 0,
            "progress": None,
        }
    
    # Get published assignments for enrolled courses
    assignments = session.exec(
        select(Assignment).where(Assignment.course_code.in_(course_codes), Assignment.is_published == True)
    ).all()
    
    # Get student's submissions
    submissions = session.exec(select(Submission).where(Submission.student_id == sid)).all()
    
    # Get notes for enrolled courses (current versions only)
    notes = session.exec(
        select(Note).where(Note.course_code.in_(course_codes), Note.is_current == True)
    ).all()
    
    # Progress
    progress = session.exec(
        select(StudentProgress).where(StudentProgress.student_id == sid, StudentProgress.course_code.in_(course_codes))
    ).all()
    
    # Per-course progress
    course_progress = {}
    for p in progress:
        if p.unit_number is None and p.assignment_id is None:
            course_progress[p.course_code] = {
                "status": p.status,
                "completion_pct": p.completion_pct,
                "marks_obtained": p.marks_obtained,
                "max_marks": p.max_marks,
            }
    
    # Per-assignment status for UI
    assignment_status = {}
    for s in submissions:
        a = session.get(Assignment, s.assignment_id)
        if a:
            assignment_status[s.assignment_id] = {
                "submitted": True,
                "is_late": s.is_late,
                "status": s.status,
                "awarded_marks": s.awarded_marks if s.status == "graded" and s.approved_at else None,
                "feedback": s.feedback if s.status == "graded" and s.approved_at else None,
            }
    
    return {
        "student_id": sid,
        "student_name": user.full_name,
        "enrolled_courses": course_codes,
        "assignments_count": len(assignments),
        "submissions_count": len(submissions),
        "notes_count": len(notes),
        "course_progress": course_progress,
        "assignment_status": assignment_status,
    }


@router.get("/progress")
def student_progress(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    sid = user.id
    enrollments = session.exec(select(Enrollment).where(Enrollment.student_id == sid)).all()
    course_codes = [e.course_code for e in enrollments]
    
    if not course_codes:
        return []
    
    try:
        for cc in course_codes:
            recompute_progress(session, sid, cc)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    
    try:
        return session.exec(
            select(StudentProgress).where(StudentProgress.student_id == sid, StudentProgress.course_code.in_(course_codes))
        ).all()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return []


@router.get("/assignments")
def student_assignments(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    sid = user.id
    enrollments = session.exec(select(Enrollment).where(Enrollment.student_id == sid)).all()
    course_codes = [e.course_code for e in enrollments]
    
    if not course_codes:
        return []
    
    assignments = session.exec(
        select(Assignment).where(Assignment.course_code.in_(course_codes), Assignment.is_published == True)
    ).all()
    
    # Get submission status for each
    submissions = session.exec(select(Submission).where(Submission.student_id == sid)).all()
    sub_map = {s.assignment_id: s for s in submissions}
    
    result = []
    for a in assignments:
        s = sub_map.get(a.id)
        result.append({
            "id": a.id,
            "title": a.title,
            "course_code": a.course_code,
            "unit_number": a.unit_number,
            "description": a.description,
            "max_marks": a.max_marks,
            "due_date": a.due_date.isoformat() if a.due_date else None,
            "rubric_text": a.rubric_text,
            "submitted": s is not None,
            "submission_id": s.id if s else None,
            "is_late": s.is_late if s else None,
            "status": s.status if s else "not_submitted",
            "awarded_marks": s.awarded_marks if s and s.status == "graded" and s.approved_at else None,
        })
    return result


@router.get("/notes")
def student_notes(course_code: str | None = None, unit_number: str | None = None, session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    sid = user.id
    enrollments = session.exec(select(Enrollment).where(Enrollment.student_id == sid)).all()
    enrolled_codes = [e.course_code for e in enrollments]
    
    if course_code and course_code not in enrolled_codes:
        return []
    
    q = select(Note).where(Note.course_code.in_(enrolled_codes), Note.is_current == True)
    if course_code:
        q = q.where(Note.course_code == course_code)
    if unit_number:
        q = q.where(Note.unit_number == unit_number)
    
    return session.exec(q).all()