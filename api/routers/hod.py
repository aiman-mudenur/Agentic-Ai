from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment, AuditLog
from models.assignment import Assignment
from models.submission import Submission
from models.course import Course, Note
from api.deps import get_current_user, require_role

router = APIRouter()


@router.get("/dashboard")
def hod_dashboard(session: Session = Depends(get_session), user: User = Depends(require_role("hod", "admin"))):
    # Read-only overview across all courses
    total_courses = len(session.exec(select(Course)).all())
    total_students = len(session.exec(select(User).where(User.role == "student")).all())
    total_faculty = len(session.exec(select(User).where(User.role == "faculty")).all())
    total_assignments = len(session.exec(select(Assignment)).all())
    total_submissions = len(session.exec(select(Submission)).all())
    
    # Submission status breakdown
    all_subs = session.exec(select(Submission)).all()
    submitted = sum(1 for s in all_subs if s.status == "submitted")
    draft_graded = sum(1 for s in all_subs if s.status == "draft_graded")
    approved = sum(1 for s in all_subs if s.status == "graded" and s.approved_at)
    late = sum(1 for s in all_subs if s.is_late)
    similarity_flagged = sum(1 for s in all_subs if s.similarity_flagged)
    
    # Per-course breakdown
    courses = session.exec(select(Course)).all()
    course_stats = []
    for c in courses:
        c_assignments = session.exec(select(Assignment).where(Assignment.course_code == c.course_code)).all()
        c_assignment_ids = [a.id for a in c_assignments]
        c_subs = session.exec(select(Submission).where(Submission.assignment_id.in_(c_assignment_ids))).all() if c_assignment_ids else []
        course_stats.append({
            "course_code": c.course_code,
            "title": c.title,
            "assignments": len(c_assignments),
            "total_submissions": len(c_subs),
            "submitted": sum(1 for s in c_subs if s.status == "submitted"),
            "draft_graded": sum(1 for s in c_subs if s.status == "draft_graded"),
            "approved": sum(1 for s in c_subs if s.status == "graded" and s.approved_at),
            "late_rate": round(sum(1 for s in c_subs if s.is_late) / len(c_subs) * 100, 1) if c_subs else 0,
            "similarity_flagged": sum(1 for s in c_subs if s.similarity_flagged),
        })
    
    # Grade approval counts
    pending_approval = sum(1 for s in all_subs if s.status == "draft_graded")
    
    return {
        "hod_id": user.id,
        "hod_name": user.full_name,
        "total_courses": total_courses,
        "total_students": total_students,
        "total_faculty": total_faculty,
        "total_assignments": total_assignments,
        "total_submissions": total_submissions,
        "submission_breakdown": {
            "submitted": submitted,
            "draft_graded_awaiting_approval": draft_graded,
            "approved_published": approved,
        },
        "late_submissions": late,
        "similarity_flagged": similarity_flagged,
        "pending_approval": pending_approval,
        "courses": course_stats,
    }


@router.get("/audit-log")
def hod_audit_log(
    limit: int = 100,
    entity_type: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("hod", "admin")),
):
    """Read-only audit log view."""
    q = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    return session.exec(q).all()


@router.get("/submissions/pending-approval")
def hod_pending_approval(session: Session = Depends(get_session), user: User = Depends(require_role("hod", "admin"))):
    """All draft-graded submissions awaiting approval."""
    subs = session.exec(select(Submission).where(Submission.status == "draft_graded")).all()
    assignments_map = {a.id: a for a in session.exec(select(Assignment)).all()}
    students_map = {u.id: u for u in session.exec(select(User).where(User.role == "student")).all()}
    
    return [
        {
            "id": s.id,
            "assignment_id": s.assignment_id,
            "assignment_title": assignments_map[s.assignment_id].title if s.assignment_id in assignments_map else "Unknown",
            "student_id": s.student_id,
            "student_name": students_map[s.student_id].full_name if s.student_id in students_map else "Unknown",
            "course_code": s.course_code,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            "is_late": s.is_late,
            "awarded_marks": s.awarded_marks,
            "max_marks": s.max_marks,
            "graded_by": s.graded_by,
            "graded_at": s.graded_at.isoformat() if s.graded_at else None,
            "similarity_flagged": s.similarity_flagged,
        }
        for s in subs
    ]


@router.post("/submissions/{submission_id}/approve")
def hod_approve_grade(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("hod", "admin")),
):
    """HoD can approve any draft grade."""
    from datetime import timezone
    
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    if sub.status != "draft_graded":
        raise HTTPException(status_code=409, detail="No draft grade to approve")
    
    a = session.get(Assignment, sub.assignment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    sub.status = "graded"
    sub.approved_by = user.id
    sub.approved_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    
    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="grade_approved",
        entity_type="submission",
        entity_id=sub.id,
        details=f"HoD {user.email} approved grade {sub.awarded_marks}/{a.max_marks} for submission {submission_id}"
    ))
    session.commit()
    
    from services.progress_service import recompute_progress
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    
    return {"status": "approved", "submission_id": submission_id}


# Need to import HTTPException and datetime
from fastapi import HTTPException
from datetime import datetime, timezone