from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment, AuditLog
from models.assignment import Assignment
from models.submission import Submission
from api.deps import get_current_user, require_role
from services.storage import save_upload
from services.progress_service import recompute_progress
from services.file_serve import file_response_for
from services.github_submission import (
    validate_and_fetch_commit,
    get_commit_diff,
    validate_repo_accessible,
    GitHubError,
)

router = APIRouter()


def _submission_to_dict(sub: Submission, user_role: str = "student") -> dict:
    """Convert submission to dict, filtering sensitive fields based on role."""
    base = {
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
        "max_marks": sub.max_marks,
        "run_id": sub.run_id,
        "submission_type": sub.submission_type,
        "github_repo_url": sub.github_repo_url,
        "commit_hash": sub.commit_hash,
        "branch": sub.branch,
    }
    
    # Students only see approved grades, not draft grades.
    # `graded` status is only ever set by the approve path, so it implies
    # publication; approved_at is exposed (timestamp only, no leak) so the
    # progress graphs can filter reliably.
    if user_role == "student":
        base["approved_at"] = sub.approved_at.isoformat() if sub.approved_at else None
        if sub.status == "graded":
            base["awarded_marks"] = sub.awarded_marks
            base["feedback"] = sub.feedback
        else:
            base["awarded_marks"] = None
            base["feedback"] = None
        # Never show similarity flags or draft grades to students
        base["similarity_score"] = None
        base["similarity_flagged"] = None
        base["graded_by"] = None
        base["graded_at"] = None
    else:
        # Faculty and HoD see everything
        base["awarded_marks"] = sub.awarded_marks
        base["feedback"] = sub.feedback
        base["similarity_score"] = sub.similarity_score
        base["similarity_flagged"] = sub.similarity_flagged
        base["graded_by"] = sub.graded_by
        base["graded_at"] = sub.graded_at.isoformat() if sub.graded_at else None
        base["approved_by"] = sub.approved_by
        base["approved_at"] = sub.approved_at.isoformat() if sub.approved_at else None
    
    return base


@router.get("/")
def list_submissions(assignment_id: int | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Submission)
    
    if user.role == "student":
        q = q.where(Submission.student_id == user.id)
        # Only show submissions for courses student is enrolled in
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id)).all()
        if enrolled:
            # Join with assignment to filter by course
            from sqlmodel import col
            q = q.where(Submission.assignment_id.in_(
                select(Assignment.id).where(Assignment.course_code.in_(enrolled))
            ))
        else:
            q = q.where(Submission.id == -1)  # no results
    elif user.role == "faculty":
        # Faculty sees submissions for assignments they created
        q = q.where(Submission.assignment_id.in_(
            select(Assignment.id).where(Assignment.created_by == user.id)
        ))
    # HoD sees all
    
    if assignment_id:
        q = q.where(Submission.assignment_id == assignment_id)
    
    results = session.exec(q).all()
    return [_submission_to_dict(s, user.role) for s in results]


@router.get("/{submission_id}")
def get_submission(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Fetch a single submission, with role-based access checks."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    if user.role == "student":
        if sub.student_id != user.id:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif user.role == "faculty":
        a = session.get(Assignment, sub.assignment_id)
        if not a or a.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your assignment")
    elif user.role not in ("hod", "admin"):
        raise HTTPException(status_code=403, detail="Forbidden")

    return _submission_to_dict(sub, user.role)


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
    
    # Verify student is enrolled in the course
    enrolled = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == a.course_code)).first()
    if not enrolled:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")
    
    # Check for duplicate submission (write-once)
    existing = session.exec(select(Submission).where(Submission.assignment_id == assignment_id, Submission.student_id == user.id)).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already submitted. Submissions are write-once.")
    
    m = save_upload(file, "submissions")
    now = datetime.now(timezone.utc)
    is_late = bool(a.due_date and now.date() > a.due_date)
    sub = Submission(
        assignment_id=assignment_id,
        student_id=user.id,
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
    
    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="submission_created",
        entity_type="submission",
        entity_id=sub.id,
        details=f"Student {user.email} submitted assignment {assignment_id} for course {a.course_code}"
    ))
    session.commit()
    
    try:
        recompute_progress(session, user.id, a.course_code, unit_number=a.unit_number, assignment_id=a.id)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    # Lab orchestration (background, best-effort):
    # Lab 8 parallel swarm + Lab 2 grading-agent auto-draft, one pipeline.
    try:
        import threading
        from services.swarm_trigger import run_submit_pipeline
        threading.Thread(
            target=run_submit_pipeline,
            kwargs={"course_code": a.course_code, "assignment_id": a.id, "submission_id": sub.id},
            daemon=True,
        ).start()
    except Exception:
        pass
    return _submission_to_dict(sub, user.role)


@router.post("/github")
def submit_github_assignment(
    assignment_id: int = Form(...),
    repo_url: str = Form(...),
    commit_hash: str = Form(...),
    branch: str | None = Form(None),
    github_token: str | None = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_role("student")),
):
    """Submit a GitHub repository as an assignment submission.

    Validates the repository URL, verifies the commit exists, and stores the
    submission metadata. Does not clone or download repository content.
    """
    a = session.get(Assignment, assignment_id)
    if not a or not a.is_published:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Verify student is enrolled in the course
    enrolled = session.exec(
        select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == a.course_code)
    ).first()
    if not enrolled:
        raise HTTPException(status_code=403, detail="Not enrolled in this course")

    # Check for duplicate submission (write-once)
    existing = session.exec(
        select(Submission).where(Submission.assignment_id == assignment_id, Submission.student_id == user.id)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already submitted. Submissions are write-once.")

    # Validate the repository and commit via GitHub API
    try:
        commit_info = validate_and_fetch_commit(repo_url, commit_hash, branch=branch, github_token=github_token)
    except GitHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    repo_info = validate_repo_accessible(repo_url, github_token=github_token)

    now = datetime.now(timezone.utc)
    is_late = bool(a.due_date and now.date() > a.due_date)

    sub = Submission(
        assignment_id=assignment_id,
        student_id=user.id,
        course_code=a.course_code,
        submission_type="github",
        github_repo_url=repo_url,
        commit_hash=commit_hash,
        branch=branch,
        submitted_at=now,
        is_late=is_late,
        status="submitted",
        max_marks=a.max_marks,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)

    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="submission_created",
        entity_type="submission",
        entity_id=sub.id,
        details=f"Student {user.email} submitted GitHub repo {repo_url} commit {commit_hash[:7] if commit_hash else 'unknown'} for assignment {assignment_id} (course {a.course_code})",
    ))
    session.commit()

    try:
        recompute_progress(session, user.id, a.course_code, unit_number=a.unit_number, assignment_id=a.id)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass

    # Lab orchestration (background, best-effort)
    try:
        import threading
        from services.swarm_trigger import run_submit_pipeline
        threading.Thread(
            target=run_submit_pipeline,
            kwargs={"course_code": a.course_code, "assignment_id": a.id, "submission_id": sub.id},
            daemon=True,
        ).start()
    except Exception:
        pass

    return {
        "id": sub.id,
        "assignment_id": sub.assignment_id,
        "student_id": sub.student_id,
        "course_code": sub.course_code,
        "submission_type": sub.submission_type,
        "github_repo_url": sub.github_repo_url,
        "commit_hash": sub.commit_hash,
        "branch": sub.branch,
        "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
        "is_late": sub.is_late,
        "status": sub.status,
        "max_marks": sub.max_marks,
    }


@router.get("/{submission_id}/diff")
def get_submission_diff(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Fetch the diff for a GitHub submission commit."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    if user.role == "student" and sub.student_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    elif user.role == "faculty":
        a = session.get(Assignment, sub.assignment_id)
        if not a or a.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your assignment")

    if sub.submission_type != "github":
        raise HTTPException(status_code=400, detail="Submission is not a GitHub submission")

    try:
        diff = get_commit_diff(sub.github_repo_url, sub.commit_hash)
    except GitHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "commit_sha": diff.sha,
        "files": [
            {
                "filename": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "changes": f.changes,
                "patch": f.patch,
            }
            for f in diff.files
        ],
        "total_additions": diff.total_additions,
        "total_deletions": diff.total_deletions,
        "total_changes": diff.total_changes,
    }


@router.get("/{submission_id}/commits")
def get_submission_commits(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """List recent commits for a GitHub submission repository."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    if user.role == "student" and sub.student_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    elif user.role == "faculty":
        a = session.get(Assignment, sub.assignment_id)
        if not a or a.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your assignment")

    if sub.submission_type != "github":
        raise HTTPException(status_code=400, detail="Submission is not a GitHub submission")

    try:
        repo = validate_repo_accessible(sub.github_repo_url)
    except GitHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        import requests
        resp = requests.get(
            f"https://api.github.com/repos/{repo.owner}/{repo.repo}/commits",
            params={"per_page": 10},
            timeout=15,
        )
        if resp.status_code != 200:
            raise GitHubError(f"GitHub API returned status {resp.status_code}")
    except requests.RequestException as exc:
        raise GitHubError(f"Could not reach GitHub API: {exc}")

    commits = []
    for item in resp.json():
        commits.append({
            "sha": item["sha"],
            "short_sha": item["sha"][:7],
            "message": item["commit"]["message"],
            "author_name": item["commit"]["author"]["name"],
            "author_date": item["commit"]["author"]["date"],
            "html_url": item["html_url"],
        })

    return {"commits": commits}


@router.post("/{submission_id}/grade")
def grade_submission(
    submission_id: int,
    awarded_marks: float,
    feedback: str | None = None,
    similarity_score: float | None = None,
    similarity_flagged: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty")),
):
    """Faculty enters a draft grade. Status becomes 'draft_graded'. Needs approval to become 'graded'."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    # Verify faculty owns the assignment
    a = session.get(Assignment, sub.assignment_id)
    if not a or a.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your assignment")
    
    if sub.status == "graded" and sub.approved_at:
        raise HTTPException(status_code=409, detail="Already approved. Cannot modify published grade.")
    
    sub.awarded_marks = awarded_marks
    sub.feedback = feedback
    sub.similarity_score = similarity_score
    sub.similarity_flagged = similarity_flagged
    sub.status = "draft_graded"
    sub.graded_by = user.id
    sub.graded_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    
    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="draft_grade_entered",
        entity_type="submission",
        entity_id=sub.id,
        details=f"Faculty {user.email} entered draft grade {awarded_marks}/{a.max_marks} for submission {submission_id}"
    ))
    session.commit()
    
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        recompute_progress(session, sub.student_id, sub.course_code)
    return _submission_to_dict(sub, user.role)


@router.post("/{submission_id}/approve")
def approve_grade(
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod")),
):
    """Approve a draft grade, making it visible to students. Faculty can approve their own; HoD can approve any."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    if sub.status != "draft_graded":
        raise HTTPException(status_code=409, detail="No draft grade to approve")
    
    a = session.get(Assignment, sub.assignment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Faculty can only approve their own; HoD can approve any
    if user.role == "faculty" and a.created_by != user.id:
        raise HTTPException(status_code=403, detail="Can only approve grades for your own assignments")
    
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
        details=f"{user.role.title()} {user.email} approved grade {sub.awarded_marks}/{a.max_marks} for submission {submission_id}"
    ))
    session.commit()
    
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        recompute_progress(session, sub.student_id, sub.course_code)
    return _submission_to_dict(sub, user.role)


@router.get("/{submission_id}/download")
def download_submission(submission_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    if user.role == "student" and sub.student_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    elif user.role == "faculty":
        a = session.get(Assignment, sub.assignment_id)
        if not a or a.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your assignment")

    if sub.submission_type == "github":
        raise HTTPException(status_code=400, detail="GitHub submissions do not have a downloadable file. View the repository/commit or diff instead.")
    
    return file_response_for(sub.file_path, sub.file_name, sub.content_type)


@router.post("/{submission_id}/auto-grade")
def auto_grade_submission(submission_id: int, session: Session = Depends(get_session), user: User = Depends(require_role("faculty"))):
    """Grade with the Lab agent. Always recorded as a draft grade."""
    from datetime import timezone
    from services.auto_grade import extract_text, grade_with_agent, similarity_vs_others
    
    sub = session.get(Submission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    a = session.get(Assignment, sub.assignment_id)
    if not a or a.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your assignment")

    if sub.submission_type == "github":
        raise HTTPException(status_code=400, detail="Auto-grading is not available for GitHub submissions. Please grade manually.")

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
    sub.status = "draft_graded"
    sub.graded_by = user.id
    sub.graded_at = datetime.now(timezone.utc)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    
    # Audit log
    session.add(AuditLog(
        actor_id=user.id,
        action="auto_draft_grade_entered",
        entity_type="submission",
        entity_id=sub.id,
        details=f"Faculty {user.email} ran auto-grade agent for submission {submission_id}, got {verdict.awarded_marks}/{a.max_marks}"
    ))
    session.commit()
    
    try:
        recompute_progress(session, sub.student_id, sub.course_code)
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    return _submission_to_dict(sub, user.role)