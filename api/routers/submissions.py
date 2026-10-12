from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlmodel import Session, select
from db.session import get_session
from models.assignment import Assignment
from models.submission import Submission
from models.auth import User
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.progress_service import recompute_progress
from services.file_serve import file_response_for

router = APIRouter()


def _submission_to_dict(sub: Submission) -> dict:
    return {
        "id": sub.id,
        "assignment_id": sub.assignment_id,
        "student_id": sub.student_id,
        "course_code": sub.course_code,
        "file_path": sub.file_path,
        "file_name": sub.file_name,
        "content_type": sub.content_type,
        "size_bytes": sub.size_bytes,
        "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
        "is_late": sub.is_late,
        "status": sub.status,
        "awarded_marks": sub.awarded_marks,
        "max_marks": sub.max_marks,
        "similarity_score": sub.similarity_score,
        "similarity_flagged": sub.similarity_flagged,
        "feedback": sub.feedback,
        "graded_by": sub.graded_by,
        "graded_at": sub.graded_at.isoformat() if sub.graded_at else None,
        "run_id": sub.run_id,
    }


@router.get("/")
def list_submissions(assignment_id: int | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Submission)
    if user.role == "student":
        q = q.where(Submission.student_id == user.id)
    if assignment_id:
        q = q.where(Submission.assignment_id == assignment_id)
    return session.exec(q).all()


@router.post("/submit")
def submit_assignment(
    assignment_id: int = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_role("student")),
):
    a = session.get(Assignment, assignment_id)
    if not a or not a.is_published:
        raise HTTPException(status_code=404, detail="Assignment not found")
    m = save_upload(file, "submissions")
    now = datetime.now(timezone.utc)
    is_late = bool(a.due_date and now.date() > a.due_date)
    sub = Submission(
        assignment_id=assignment_id,
        student_id=user.id or 0,
        course_code=a.course_code,
        file_path=m["file_path"],
        file_name=m["file_name"],
        content_type=m["content_type"],
        size_bytes=m["size_bytes"],
        submitted_at=now,
        is_late=is_late,
        status="submitted",
        max_marks=a.max_marks,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    try:
        recompute_progress(session, user.id or 0, a.course_code, unit_number=a.unit_number, assignment_id=a.id)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    try:
        import threading

        from services.swarm_trigger import maybe_run_swarm_for_submission

        threading.Thread(
            target=maybe_run_swarm_for_submission,
            kwargs={"course_code": a.course_code, "assignment_id": a.id, "submission_id": sub.id},
            daemon=True,
        ).start()
    except Exception:
        pass
    return _submission_to_dict(sub)


@router.post("/{submission_id}/grade")
def grade_submission(
    submission_id: int,
    awarded_marks: float,
    feedback: str | None = None,
    similarity_score: float | None = None,
    similarity_flagged: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "admin")),
):
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    sub.awarded_marks = awarded_marks
    sub.feedback = feedback
    sub.similarity_score = similarity_score
    sub.similarity_flagged = similarity_flagged
    sub.status = "graded"
    sub.graded_by = user.id
    sub.graded_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        recompute_progress(session, sub.student_id, sub.course_code)
    return _submission_to_dict(sub)


@router.get("/{submission_id}/download")
def download_submission(submission_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if user.role == "student" and sub.student_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return file_response_for(sub.file_path, sub.file_name, sub.content_type)


@router.post("/{submission_id}/auto-grade")
def auto_grade_submission(submission_id: int, session: Session = Depends(get_session), user: User = Depends(require_role("faculty", "admin"))):
    """Grade with the Lab agent (offline heuristic on scripted backend, LLM on live).

    Always recorded as an agent draft; faculty can override via /grade.
    """
    from datetime import timezone

    from services.auto_grade import extract_text, grade_with_agent, similarity_vs_others

    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    a = session.get(Assignment, sub.assignment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Assignment not found")
    text = extract_text(sub.file_path, sub.file_name)
    others = session.exec(
        select(Submission).where(Submission.assignment_id == sub.assignment_id, Submission.id != sub.id)
    ).all()
    sim = similarity_vs_others(text, [extract_text(o.file_path, o.file_name) for o in others])
    verdict = grade_with_agent(
        submission_text=text or (sub.file_name or ""),
        max_marks=a.max_marks,
        rubric_text=a.rubric_text,
        description=a.description,
        similarity_score=sim,
    )
    sub.awarded_marks = verdict.awarded_marks
    sub.max_marks = verdict.max_marks
    sub.feedback = (verdict.feedback or "") + f" [auto-graded by agent for {sub.file_name or 'submission'}]"
    sub.similarity_score = verdict.similarity_score
    sub.similarity_flagged = verdict.similarity_flagged
    sub.status = "graded"
    sub.graded_by = user.id
    sub.graded_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    return _submission_to_dict(sub)
