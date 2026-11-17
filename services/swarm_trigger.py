from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


def _enabled() -> bool:
    return os.getenv("AUTO_SWARM_ON_SUBMIT", "true").lower() in ("1", "true", "yes")


def maybe_run_swarm_for_submission(course_code: str, assignment_id: int, submission_id: int) -> dict[str, Any] | None:
    """Run Lab 8 swarm in backend when a submission arrives. No UI needed.

    Best-effort: never raises; returns summary dict or None when disabled/failing.
    """
    if not _enabled():
        return None
    try:
        from services.graph_service import run_parallel_swarm
    except Exception:
        return None
    run_id = f"auto-submit-a{assignment_id}-s{submission_id}-{int(time.time())}"
    try:
        return run_parallel_swarm(course_code=course_code, run_id=run_id, parallel=True)
    except Exception:
        return None


def _auto_grade_enabled() -> bool:
    return os.getenv("AUTO_GRADE_ON_SUBMIT", "true").lower() in ("1", "true", "yes")


def auto_draft_grade_for_submission(submission_id: int, session_factory=None) -> dict[str, Any] | None:
    """Lab 2 grading agent -> draft grade on a fresh submission (faculty approves later).

    Runs in the submit background pipeline with its own DB session.
    Best-effort: never raises; None when disabled/failing/already graded.
    """
    if not _auto_grade_enabled():
        return None
    try:
        from sqlmodel import Session, select

        from models.assignment import Assignment
        from models.auth import AuditLog
        from models.submission import Submission
        from services.auto_grade import extract_text, grade_with_agent, similarity_vs_others
        from services.progress_service import recompute_progress
    except Exception:
        return None
    if session_factory is None:
        from db.engine import engine

        session_factory = lambda: Session(engine)  # noqa: E731
    try:
        with session_factory() as session:
            sub = session.get(Submission, submission_id)
            if not sub or sub.status != "submitted":
                return None
            a = session.get(Assignment, sub.assignment_id)
            if not a:
                return None
            text = extract_text(sub.file_path, sub.file_name)
            others = session.exec(
                select(Submission).where(
                    Submission.assignment_id == sub.assignment_id, Submission.id != sub.id
                )
            ).all()
            sim = similarity_vs_others(text, [extract_text(o.file_path, o.file_name) for o in others])
            verdict = grade_with_agent(
                submission_text=text or (sub.file_name or ""),
                max_marks=a.max_marks,
                rubric_text=a.rubric_text,
                description=a.description,
                similarity_score=sim,
            )
            from datetime import datetime, timezone

            sub.awarded_marks = verdict.awarded_marks
            sub.max_marks = verdict.max_marks
            sub.feedback = (verdict.feedback or "") + " [auto-draft by grading agent on submit]"
            sub.similarity_score = verdict.similarity_score
            sub.similarity_flagged = verdict.similarity_flagged
            sub.status = "draft_graded"
            sub.graded_at = datetime.now(timezone.utc)
            session.add(sub)
            session.commit()
            session.refresh(sub)
            try:
                session.add(AuditLog(
                    actor_id=0, action="auto_draft_grade_entered", entity_type="submission",
                    entity_id=sub.id,
                    details=f"Grading agent drafted {verdict.awarded_marks}/{a.max_marks} for submission {sub.id}",
                ))
                session.commit()
            except Exception:
                pass
            try:
                recompute_progress(session, sub.student_id, sub.course_code)
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
            return {"submission_id": sub.id, "awarded_marks": verdict.awarded_marks,
                    "similarity_flagged": verdict.similarity_flagged}
    except Exception:
        return None


def run_submit_pipeline(course_code: str, assignment_id: int, submission_id: int, session_factory=None) -> dict[str, Any]:
    """One backend pipeline per submit: Lab 8 swarm + Lab 2 auto-grade draft.

    Best-effort: never raises; each stage is independently guarded.
    Agent evidence is recorded on the submission (run_id) and the audit log
    so the Agent Activity view can prove what ran for every submit.
    """
    out: dict[str, Any] = {"swarm": None, "auto_grade": None}
    try:
        out["swarm"] = maybe_run_swarm_for_submission(course_code, assignment_id, submission_id)
    except Exception:
        pass
    try:
        out["auto_grade"] = auto_draft_grade_for_submission(submission_id, session_factory)
    except Exception:
        pass
    try:
        _record_swarm_evidence(submission_id, out.get("swarm"), session_factory)
    except Exception:
        pass
    return out


def _record_swarm_evidence(submission_id: int, swarm: dict[str, Any] | None, session_factory=None) -> None:
    """Stamp the Lab 8 run_id + summary onto the submission and audit log."""
    if not swarm or not isinstance(swarm, dict):
        return
    try:
        from sqlmodel import Session

        from models.auth import AuditLog
        from models.submission import Submission
    except Exception:
        return
    if session_factory is None:
        from db.engine import engine

        session_factory = lambda: Session(engine)  # noqa: E731
    try:
        with session_factory() as session:
            sub = session.get(Submission, submission_id)
            if not sub:
                return
            sub.run_id = str(swarm.get("run_id") or sub.run_id or "")
            session.add(sub)
            timing = (swarm.get("swarm_timing") or {})
            session.add(AuditLog(
                actor_id=0, action="swarm_completed", entity_type="submission",
                entity_id=sub.id,
                details=(
                    f"Lab 8 swarm run {sub.run_id}: status={swarm.get('status')}, "
                    f"stage_ms={timing.get('stage_ms')}, "
                    f"nodes={','.join(swarm.get('nodes_ran') or [])}"
                )[:500],
            ))
            session.commit()
    except Exception:
        pass
