"""Developer observability: prove which lab agents ran for each UI operation.

No new data collection — it reads the evidence the pipeline already writes:
submission.run_id (Lab 8), draft/approved grades (Labs 2/7 gate), the audit log
(Lab 6), and the run artifact dirs under out/.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, AuditLog
from models.assignment import Assignment
from models.submission import Submission
from api.deps import require_role

router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

AGENT_ACTIONS = {
    "submission_created", "swarm_completed", "auto_draft_grade_entered",
    "draft_grade_entered", "grade_approved", "assignment_imported",
    "note_imported", "note_uploaded", "note_updated", "course_created", "enrolled",
}


@router.get("/activity")
def agent_activity(limit: int = 20, session: Session = Depends(get_session),
                   user: User = Depends(require_role("faculty", "hod", "admin"))):
    """Per-submission agent trace + recent agent audit entries."""
    subs = session.exec(select(Submission).order_by(Submission.id.desc()).limit(limit)).all()
    if user.role == "faculty":
        own_aids = set(session.exec(select(Assignment.id).where(Assignment.created_by == user.id)).all())
        subs = [s for s in subs if s.assignment_id in own_aids]
    aids = {s.assignment_id for s in subs}
    sids = {s.student_id for s in subs}
    assignments = {a.id: a for a in session.exec(select(Assignment).where(Assignment.id.in_(aids))).all()} if aids else {}
    students = {u.id: u for u in session.exec(select(User).where(User.id.in_(sids))).all()} if sids else {}
    trace = []
    for s in subs:
        a = assignments.get(s.assignment_id)
        u = students.get(s.student_id)
        trace.append({
            "submission_id": s.id,
            "assignment": (a.title if a else "?") + f" (#{s.assignment_id})",
            "student": (u.email if u else "?"),
            "status": s.status,
            "on_time": (not s.is_late),
            # Lab 8 evidence
            "swarm_run_id": s.run_id or None,
            # Lab 2 + similarity evidence
            "draft_marks": s.awarded_marks,
            "similarity": s.similarity_score,
            "similarity_flagged": bool(s.similarity_flagged),
            # Lab 7 gate evidence
            "approved_by": s.approved_by,
            "approved_at": s.approved_at.isoformat()[:19] if s.approved_at else None,
            "submitted_at": s.submitted_at.isoformat()[:19] if s.submitted_at else None,
        })
    logs = session.exec(select(AuditLog).where(AuditLog.action.in_(AGENT_ACTIONS)).order_by(
        AuditLog.id.desc()).limit(limit)).all()
    return {
        "trace": trace,
        "agent_audit": [
            {"id": l.id, "action": l.action, "entity": f"{l.entity_type}#{l.entity_id}",
             "actor_id": l.actor_id, "at": (l.timestamp or "")[:19],
             "details": (l.details or "")[:200]}
            for l in logs
        ],
    }


@router.get("/runs")
def agent_runs(session: Session = Depends(get_session),
               user: User = Depends(require_role("faculty", "hod", "admin"))):
    """Run artifact dirs written by the Lab 7/8 graph runners."""
    del session, user  # filesystem evidence only
    runs: list[dict] = []
    for kind in ("lab07", "lab08"):
        root = REPO_ROOT / "out" / kind
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
            if not child.is_dir():
                continue
            try:
                st = child.stat()
                n_files = sum(1 for _ in child.rglob("*") if _.is_file())
            except OSError:
                continue
            runs.append({
                "pipeline": kind, "run_id": child.name,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "files": n_files,
            })
    return runs


@router.get("/inventory")
def agent_inventory(session: Session = Depends(get_session),
                    user: User = Depends(require_role("faculty", "hod", "admin"))):
    """Agent repository: live counts of agents, tools, skills + framework map."""
    del session, user  # computed from code + installed packages, not the DB
    from services.agent_inventory import full_inventory

    return full_inventory()
