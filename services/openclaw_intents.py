"""Deterministic, teacher-friendly answers for the most common assistant questions.

The Gateway agent has its own tool set (memory, shell, etc.) which is *not* the
Course Organizer's restricted tool layer. To guarantee that ordinary faculty
questions produce readable, correct answers, we answer the core read-only
intents directly from the audited backend tools and only fall back to the LLM
for everything else.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlmodel import Session

from models.auth import User
from services.openclaw_tools import (
    ToolDenied,
    find_late_submissions,
    get_submission_status,
    list_assignments,
    read_github_activity,
    summarize_course_notes,
)

_COURSE_RE = re.compile(r"\b([A-Za-z]{2,5}\s?-?\d{3,4})\b")


def _course_code(message: str) -> Optional[str]:
    match = _COURSE_RE.search(message)
    return match.group(1).replace(" ", "").upper() if match else None


def _fmt_assignments(data: dict) -> str:
    rows = data.get("assignments", [])
    if not rows:
        return "There are no published assignments to show."
    lines = [f"Published assignments ({len(rows)}):"]
    for a in rows:
        due = a.get("due_date") or "no due date"
        lines.append(
            f"- {a['course_code']} — {a['title']} (due {due}, max marks {a['max_marks']}, id {a['id']})"
        )
    return "\n".join(lines)


def _fmt_late(data: dict) -> str:
    rows = data.get("late_submissions", [])
    if not rows:
        return "There are no late submissions."
    lines = [f"Late submissions ({len(rows)}):"]
    for s in rows:
        lines.append(
            f"- Submission #{s['id']} for assignment #{s['assignment_id']} "
            f"({s['course_code']}), student #{s['student_id']}, status: {s['status']}"
        )
    return "\n".join(lines)


def _fmt_github(data: dict) -> str:
    rows = data.get("github_activity", [])
    if not rows:
        return "There are no GitHub submissions to show."
    lines = [f"GitHub submissions ({len(rows)}):"]
    for s in rows:
        lines.append(
            f"- Submission #{s['submission_id']} (assignment #{s['assignment_id']}, "
            f"student #{s['student_id']}): {s['repo_url']} · commit {s['commit_hash'] or 'n/a'} · "
            f"branch {s['branch'] or 'n/a'}"
        )
    return "\n".join(lines)


def _fmt_notes(data: dict) -> str:
    rows = data.get("notes", [])
    code = data.get("course_code", "the course")
    if not rows:
        return f"No course notes are available for {code}."
    lines = [f"Notes for {code} ({len(rows)}):"]
    for n in rows:
        lines.append(
            f"- {n['title']}" + (f" (unit {n['unit_number']})" if n.get("unit_number") else "")
        )
    return "\n".join(lines)


def try_answer(session: Session, user: User, message: str) -> Optional[str]:
    """Return a plain-language answer for a known read-only intent, else None."""
    text = (message or "").strip().lower()
    if not text:
        return None
    course = _course_code(message)

    try:
        if "late" in text and "submission" in text:
            return _fmt_late(find_late_submissions(session, user, course_code=course))
        if "github" in text or "commit" in text or "repo" in text:
            return _fmt_github(read_github_activity(session, user))
        if "note" in text and ("summar" in text or "list" in text or "show" in text or course):
            if not course:
                return "Please include a course code, for example 'Summarize notes for CS201'."
            return _fmt_notes(summarize_course_notes(session, user, course, max_notes=10))
        if "status" in text and "submission" in text:
            return _fmt_late(get_submission_status(session, user))
        if "assignment" in text or "due" in text:
            return _fmt_assignments(list_assignments(session, user, course_code=course))
    except ToolDenied as exc:
        return (
            f"{exc}. I can only show assignments, submissions, and notes you own "
            "or are published to your courses. Please check the course code, or use "
            "the relevant Course Organizer page."
        )
    except Exception:
        # Permission or data issues: fall back to the assistant rather than
        # leaking an internal error to the teacher.
        return None
    return None
