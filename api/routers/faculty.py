from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment
from models.assignment import Assignment
from models.submission import Submission
from models.course import Note
from api.deps import get_current_user, require_role
from services.reminder_service import send_deadline_reminders

router = APIRouter()


@router.post("/reminders/send")
def send_reminders(
    dry_run: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Trigger deadline reminders for published assignments.

    Safe to call repeatedly — duplicate reminders are prevented.
    Set dry_run=true to check what would be sent without sending.
    """
    summary = send_deadline_reminders(session, dry_run=dry_run)
    return summary


@router.get("/dashboard")
def faculty_dashboard(session: Session = Depends(get_session), user: User = Depends(require_role("faculty"))):
    # Get all assignments created by this faculty
    assignments = session.exec(select(Assignment).where(Assignment.created_by == user.id)).all()
    assignment_ids = [a.id for a in assignments]
    course_codes = list(set(a.course_code for a in assignments))
    
    # Get submissions for these assignments
    submissions = session.exec(select(Submission).where(Submission.assignment_id.in_(assignment_ids))).all() if assignment_ids else []
    
    # Get notes for these courses
    notes = session.exec(select(Note).where(Note.course_code.in_(course_codes), Note.is_current == True)).all() if course_codes else []
    
    # Count pending (submitted but not draft_graded or graded)
    pending_grading = sum(1 for s in submissions if s.status == "submitted")
    draft_graded = sum(1 for s in submissions if s.status == "draft_graded")
    approved = sum(1 for s in submissions if s.status == "graded" and s.approved_at)
    
    # Per-assignment breakdown
    assignment_stats = []
    for a in assignments:
        a_subs = [s for s in submissions if s.assignment_id == a.id]
        assignment_stats.append({
            "assignment_id": a.id,
            "title": a.title,
            "course_code": a.course_code,
            "total_submissions": len(a_subs),
            "pending": sum(1 for s in a_subs if s.status == "submitted"),
            "draft_graded": sum(1 for s in a_subs if s.status == "draft_graded"),
            "approved": sum(1 for s in a_subs if s.status == "graded" and s.approved_at),
            "on_time": sum(1 for s in a_subs if not s.is_late),
            "late": sum(1 for s in a_subs if s.is_late),
        })
    
    return {
        "faculty_id": user.id,
        "faculty_name": user.full_name,
        "courses": course_codes,
        "assignments_count": len(assignments),
        "submissions_count": len(submissions),
        "notes_count": len(notes),
        "pending_grading": pending_grading,
        "draft_graded": draft_graded,
        "approved": approved,
        "assignment_stats": assignment_stats,
    }


@router.get("/submissions")
def faculty_submissions(
    assignment_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty")),
):
    """Get submissions for faculty's assignments with filtering."""
    assignment_ids = list(session.exec(select(Assignment.id).where(Assignment.created_by == user.id)).all())
    if not assignment_ids:
        return []
    
    q = select(Submission).where(Submission.assignment_id.in_(assignment_ids))
    if assignment_id:
        if assignment_id not in assignment_ids:
            raise HTTPException(status_code=403, detail="Not your assignment")
        q = q.where(Submission.assignment_id == assignment_id)
    if status:
        q = q.where(Submission.status == status)
    
    results = session.exec(q).all()
    # Return with assignment info
    assignments_map = {a.id: a for a in session.exec(select(Assignment).where(Assignment.id.in_(assignment_ids))).all()}
    return [
        {
            "id": s.id,
            "assignment_id": s.assignment_id,
            "assignment_title": assignments_map[s.assignment_id].title if s.assignment_id in assignments_map else "Unknown",
            "student_id": s.student_id,
            "file_name": s.file_name,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            "is_late": s.is_late,
            "status": s.status,
            "awarded_marks": s.awarded_marks,
            "max_marks": s.max_marks,
            "similarity_score": s.similarity_score,
            "similarity_flagged": s.similarity_flagged,
            "feedback": s.feedback,
            "approved_at": s.approved_at.isoformat() if s.approved_at else None,
            "submission_type": s.submission_type,
            "github_repo_url": s.github_repo_url,
            "commit_hash": s.commit_hash,
            "branch": s.branch,
        }
        for s in results
    ]

# Need to import HTTPException
from fastapi import HTTPException