"""Restricted backend tools exposed to the OpenClaw AI Course Assistant.

Security model:
- Only faculty/hod/admin may call these (enforced by the router).
- Faculty are scoped to assignments/notes/submissions they own (course-level
  permission). HoD/admin have department-wide read access.
- Only the explicit allowlist below is callable. There is no raw SQL, no shell,
  and no arbitrary file access.
- Every call writes an AuditLog row.
- Grade/publish tools only create a pending OpenClawApproval; they never mutate.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlmodel import Session, select

from models.assignment import Assignment
from models.auth import AuditLog, User
from models.course import Note
from models.openclaw import OpenClawApproval
from models.submission import Submission
from services.openclaw_config import APPROVAL_REQUIRED_TOOLS, CONFIG


class ToolDenied(Exception):
    """Raised when a tool is not allowed or the user lacks course permission."""


# --------------------------------------------------------------------------- #
# Permission helpers
# --------------------------------------------------------------------------- #
def _can_access_assignment(session: Session, user: User, assignment_id: int) -> bool:
    a = session.get(Assignment, assignment_id)
    if not a:
        return False
    if user.role in ("hod", "admin"):
        return True
    return a.created_by == user.id


def _can_access_submission(session: Session, user: User, submission: Submission) -> bool:
    if user.role in ("hod", "admin"):
        return True
    a = session.get(Assignment, submission.assignment_id)
    return bool(a and a.created_by == user.id)


def _can_access_course(session: Session, user: User, course_code: str) -> bool:
    if user.role in ("hod", "admin"):
        return True
    # Faculty can access courses where they created an assignment or uploaded a note.
    owns_assignment = session.exec(
        select(Assignment.id).where(Assignment.created_by == user.id, Assignment.course_code == course_code)
    ).first()
    if owns_assignment:
        return True
    owns_note = session.exec(
        select(Note.id).where(Note.course_code == course_code, Note.uploaded_by == user.id)
    ).first()
    return bool(owns_note)


def _audit(session: Session, user: User, action: str, entity_type: str, entity_id: int | None, details: str) -> None:
    session.add(AuditLog(
        actor_id=user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id or 0,
        details=details[:1000],
    ))
    session.commit()


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #
def list_assignments(session: Session, user: User, course_code: str | None = None) -> dict[str, Any]:
    q = select(Assignment).where(Assignment.is_published == True)
    if user.role == "faculty":
        q = q.where(Assignment.created_by == user.id)
        if course_code:
            if not _can_access_course(session, user, course_code):
                raise ToolDenied("You do not have access to this course")
            q = q.where(Assignment.course_code == course_code)
    elif course_code:
        if not _can_access_course(session, user, course_code):
            raise ToolDenied("You do not have access to this course")
        q = q.where(Assignment.course_code == course_code)

    rows = session.exec(q).all()
    _audit(session, user, "openclaw_tool_list_assignments", "assignment", None,
           f"course_code={course_code or 'all'} count={len(rows)}")
    return {
        "assignments": [
            {
                "id": a.id,
                "course_code": a.course_code,
                "title": a.title,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "max_marks": a.max_marks,
                "is_published": a.is_published,
            }
            for a in rows
        ]
    }


def get_submission_status(session: Session, user: User, assignment_id: int | None = None, submission_id: int | None = None) -> dict[str, Any]:
    if submission_id:
        sub = session.get(Submission, submission_id)
        if not sub:
            raise ToolDenied("Submission not found")
        if not _can_access_submission(session, user, sub):
            raise ToolDenied("You do not have access to this submission")
        subs = [sub]
    else:
        if not assignment_id:
            raise ToolDenied("assignment_id or submission_id is required")
        if not _can_access_assignment(session, user, assignment_id):
            raise ToolDenied("You do not have access to this assignment")
        subs = session.exec(select(Submission).where(Submission.assignment_id == assignment_id)).all()

    _audit(session, user, "openclaw_tool_get_submission_status", "submission", submission_id,
           f"assignment_id={assignment_id} count={len(subs)}")
    return {
        "submissions": [
            {
                "id": s.id,
                "assignment_id": s.assignment_id,
                "student_id": s.student_id,
                "status": s.status,
                "awarded_marks": s.awarded_marks,
                "max_marks": s.max_marks,
                "is_late": s.is_late,
                "submission_type": s.submission_type,
                "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            }
            for s in subs
        ]
    }


def find_late_submissions(session: Session, user: User, course_code: str | None = None) -> dict[str, Any]:
    q = select(Submission).where(Submission.is_late == True)
    if user.role == "faculty":
        owned = session.exec(select(Assignment.id).where(Assignment.created_by == user.id)).all()
        if not owned:
            return {"late_submissions": []}
        q = q.where(Submission.assignment_id.in_(owned))
    if course_code:
        if not _can_access_course(session, user, course_code):
            raise ToolDenied("You do not have access to this course")
        q = q.where(Submission.course_code == course_code)

    rows = session.exec(q).all()
    _audit(session, user, "openclaw_tool_find_late_submissions", "submission", None,
           f"course_code={course_code or 'all'} count={len(rows)}")
    return {
        "late_submissions": [
            {
                "id": s.id,
                "assignment_id": s.assignment_id,
                "course_code": s.course_code,
                "student_id": s.student_id,
                "status": s.status,
                "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            }
            for s in rows
        ]
    }


def read_github_activity(session: Session, user: User, assignment_id: int | None = None, repo_url: str | None = None) -> dict[str, Any]:
    q = select(Submission).where(Submission.submission_type == "github")
    if user.role == "faculty":
        owned = session.exec(select(Assignment.id).where(Assignment.created_by == user.id)).all()
        if not owned:
            return {"github_activity": []}
        q = q.where(Submission.assignment_id.in_(owned))
    if assignment_id:
        if not _can_access_assignment(session, user, assignment_id):
            raise ToolDenied("You do not have access to this assignment")
        q = q.where(Submission.assignment_id == assignment_id)
    if repo_url:
        q = q.where(Submission.github_repo_url == repo_url)

    rows = session.exec(q).all()
    _audit(session, user, "openclaw_tool_read_github_activity", "submission", None,
           f"assignment_id={assignment_id or 'all'} repo={repo_url or 'all'} count={len(rows)}")
    return {
        "github_activity": [
            {
                "submission_id": s.id,
                "assignment_id": s.assignment_id,
                "student_id": s.student_id,
                "repo_url": s.github_repo_url,
                "commit_hash": s.commit_hash,
                "branch": s.branch,
                "status": s.status,
            }
            for s in rows
        ]
    }


def summarize_course_notes(session: Session, user: User, course_code: str, max_notes: int = 5) -> dict[str, Any]:
    if not _can_access_course(session, user, course_code):
        raise ToolDenied("You do not have access to this course")
    rows = session.exec(
        select(Note).where(Note.course_code == course_code, Note.is_current == True).limit(max_notes)
    ).all()
    _audit(session, user, "openclaw_tool_summarize_course_notes", "note", None,
           f"course_code={course_code} count={len(rows)}")
    return {
        "course_code": course_code,
        "notes": [
            {
                "id": n.id,
                "title": n.title,
                "unit_number": n.unit_number,
                "file_name": n.file_name,
                "source_url": n.source_url,
                "version": n.version,
                "uploaded_by": n.uploaded_by,
            }
            for n in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Approval-only tools (never mutate)
# --------------------------------------------------------------------------- #
def _create_approval(
    session: Session,
    user: User,
    action: str,
    entity_type: str,
    entity_id: int | None,
    payload: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    approval = OpenClawApproval(
        requested_by=user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=json.dumps(payload),
        reason=reason,
        status="pending",
    )
    session.add(approval)
    session.commit()
    session.refresh(approval)
    _audit(session, user, "openclaw_approval_requested", entity_type, entity_id,
           f"action={action} approval_id={approval.id}")
    return {
        "approval_id": approval.id,
        "action": action,
        "status": "pending",
        "message": "Change proposed. A human must approve it before it is applied.",
    }


def set_grade(session: Session, user: User, submission_id: int, awarded_marks: float, feedback: str | None = None) -> dict[str, Any]:
    sub = session.get(Submission, submission_id)
    if not sub:
        raise ToolDenied("Submission not found")
    if not _can_access_submission(session, user, sub):
        raise ToolDenied("You do not have access to this submission")
    marks = float(awarded_marks)
    if marks < 0:
        raise ToolDenied("awarded_marks cannot be negative")
    if sub.max_marks is not None and marks > float(sub.max_marks):
        raise ToolDenied(f"awarded_marks cannot exceed max_marks ({sub.max_marks})")
    return _create_approval(
        session, user, "set_grade", "submission", submission_id,
        {"awarded_marks": marks, "feedback": feedback},
        f"Proposed grade {marks} for submission {submission_id}",
    )


def approve_grade(session: Session, user: User, submission_id: int) -> dict[str, Any]:
    sub = session.get(Submission, submission_id)
    if not sub:
        raise ToolDenied("Submission not found")
    if not _can_access_submission(session, user, sub):
        raise ToolDenied("You do not have access to this submission")
    return _create_approval(
        session, user, "approve_grade", "submission", submission_id, {},
        f"Proposed approval for submission {submission_id}",
    )


def publish_release(session: Session, user: User, course_code: str) -> dict[str, Any]:
    if not _can_access_course(session, user, course_code):
        raise ToolDenied("You do not have access to this course")
    return _create_approval(
        session, user, "publish_release", "release", None,
        {"course_code": course_code},
        f"Proposed release publish for {course_code}",
    )


TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_assignments": list_assignments,
    "get_submission_status": get_submission_status,
    "find_late_submissions": find_late_submissions,
    "read_github_activity": read_github_activity,
    "summarize_course_notes": summarize_course_notes,
    "set_grade": set_grade,
    "approve_grade": approve_grade,
    "publish_release": publish_release,
}


def allowed_tool_names() -> tuple[str, ...]:
    """Return the effective allowlist: configured read tools + approval tools."""
    names: list[str] = []
    for name in tuple(CONFIG.allowed_tools) + tuple(APPROVAL_REQUIRED_TOOLS):
        if name in TOOLS and name not in names:
            names.append(name)
    return tuple(names)


def invoke_tool(session: Session, user: User, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a restricted tool by name with strict allowlisting."""
    if name not in TOOLS:
        raise ToolDenied(f"Unknown or disallowed tool: {name}")
    if name not in allowed_tool_names():
        raise ToolDenied(f"Tool not permitted: {name}")
    args = args or {}
    return TOOLS[name](session, user, **args)
