"""OpenClaw AI Course Assistant API.

This router is an optional add-on. When OpenClaw is disabled or unconfigured it
still responds safely (health = not_configured, chat = clear error), so the rest
of the app is unaffected.

Endpoints:
- GET  /openclaw/health                 connection/config status
- GET  /openclaw/tools                   restricted tool allowlist
- POST /openclaw/chat                    ask the assistant (Gateway chat/agent RPC)
- POST /openclaw/tools/invoke            invoke one restricted backend tool
- GET  /openclaw/approvals               pending approvals (faculty/hod/admin)
- POST /openclaw/approvals/{id}/approve  human approves -> apply change
- POST /openclaw/approvals/{id}/reject   human rejects
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from api.deps import get_current_user, require_role
from db.session import get_session
from models.assignment import Assignment
from models.auth import AuditLog, User
from models.openclaw import OpenClawApproval
from models.submission import Submission
from services.openclaw_client import chat_sync, get_health_sync
from services.openclaw_config import APPROVAL_REQUIRED_TOOLS, CONFIG
from services.openclaw_intents import try_answer
from services.openclaw_tools import ToolDenied, allowed_tool_names, invoke_tool

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str
    session_key: str | None = None


class ToolInvokeRequest(BaseModel):
    name: str
    args: dict | None = None


# --------------------------------------------------------------------------- #
# Health / metadata
# --------------------------------------------------------------------------- #
@router.get("/health")
def openclaw_health(user: User = Depends(get_current_user)):
    """Return OpenClaw connection health. Any authenticated user may check it."""
    health = get_health_sync()
    health["config"] = CONFIG.redacted()
    return health


@router.get("/tools")
def openclaw_tools(user: User = Depends(require_role("faculty", "hod", "admin"))):
    """List the restricted tools the assistant may use."""
    return {
        "allowed_tools": list(allowed_tool_names()),
        "approval_required": list(APPROVAL_REQUIRED_TOOLS),
        "assistant_prompt": CONFIG.assistant_prompt,
        "notes": "No raw database or shell access is exposed. Grade/publish tools "
                 "only create a pending approval for a human to accept or reject.",
    }


# --------------------------------------------------------------------------- #
# Assistant chat (Gateway RPC)
# --------------------------------------------------------------------------- #
@router.post("/chat")
def openclaw_chat(
    payload: ChatRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Send a prompt to the OpenClaw assistant.

    The Gateway may call back into the restricted tools via its own MCP/plugin
    bridge. This endpoint never passes the user's JWT to the Gateway and never
    exposes arbitrary tools.
    """
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(message) > 4000:
        raise HTTPException(status_code=400, detail="Message too long (max 4000 chars)")

    # Audit the assistant prompt (do not store the response verbatim).
    session.add(AuditLog(
        actor_id=user.id,
        action="openclaw_chat",
        entity_type="openclaw",
        entity_id=0,
        details=f"Assistant prompt by {user.email} (role={user.role}): {message[:300]}",
    ))
    session.commit()

    # Common teacher questions are answered directly from the restricted,
    # audited backend tools so the reply is plain text, not a JSON blob, and
    # does not depend on the Gateway agent's own (unavailable) tools/memory.
    canned = try_answer(session, user, message)
    if canned:
        return {"ok": True, "reply": canned, "state": "final"}

    result = chat_sync(message, session_key=payload.session_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "Assistant is unavailable right now.",
        )

    # Only conversational fields reach end users; never expose run ids, raw
    # tool payloads, or JSON blobs to the faculty UI.
    # The Gateway agent sometimes reports its own tool failures instead of an
    # answer; never show those internals to a teacher.
    answer = str(result.get("reply") or "").strip()
    lowered = answer.lower()
    useless = (
        not answer
        or "memory_search" in lowered
        or "memory search" in lowered
        or "can't access" in lowered
        or "cannot access" in lowered
        or "aren't available" in lowered
        or "are not available" in lowered
    )
    if useless:
        answer = (
            "I could not answer that with the Course Organizer tools. "
            "Try a specific question such as 'List my assignments', "
            "'Which submissions are late?', 'Show GitHub activity', or "
            "'Summarize notes for CS201'. For other tasks, use the relevant "
            "Course Organizer page."
        )

    return {"ok": True, "reply": answer, "state": result.get("state") or "final"}


# --------------------------------------------------------------------------- #
# Restricted tool invocation (backend-side, audited)
# --------------------------------------------------------------------------- #
@router.post("/tools/invoke")
def openclaw_invoke_tool(
    payload: ToolInvokeRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Invoke one restricted backend tool directly (also used by tests)."""
    try:
        return invoke_tool(session, user, payload.name, payload.args)
    except ToolDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid tool arguments: {exc}")


# --------------------------------------------------------------------------- #
# Human approvals
# --------------------------------------------------------------------------- #
def _get_pending_approval(session: Session, approval_id: int) -> OpenClawApproval:
    approval = session.get(OpenClawApproval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="Approval already decided")
    return approval


def _can_decide(session: Session, user: User, approval: OpenClawApproval) -> bool:
    if user.role in ("hod", "admin"):
        return True
    if approval.action == "publish_release":
        try:
            payload = json.loads(approval.payload or "{}")
        except Exception:
            payload = {}
        course_code = payload.get("course_code")
        if course_code:
            owns = session.exec(
                select(Assignment.id).where(Assignment.created_by == user.id, Assignment.course_code == course_code)
            ).first()
            return bool(owns)
        return False
    if approval.entity_type == "submission" and approval.entity_id:
        sub = session.get(Submission, approval.entity_id)
        if not sub:
            return False
        a = session.get(Assignment, sub.assignment_id)
        return bool(a and a.created_by == user.id)
    return False


@router.get("/approvals")
def list_openclaw_approvals(
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    q = select(OpenClawApproval).where(OpenClawApproval.status == "pending").order_by(OpenClawApproval.created_at.desc())
    if user.role == "faculty":
        q = q.where(OpenClawApproval.requested_by == user.id)
    rows = session.exec(q).all()
    return [
        {
            "id": a.id,
            "requested_by": a.requested_by,
            "action": a.action,
            "entity_type": a.entity_type,
            "entity_id": a.entity_id,
            "payload": a.payload,
            "reason": a.reason,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/approvals/{approval_id}/approve")
def approve_openclaw_approval(
    approval_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    approval = _get_pending_approval(session, approval_id)
    if not _can_decide(session, user, approval):
        raise HTTPException(status_code=403, detail="You cannot approve this change")

    try:
        payload = json.loads(approval.payload or "{}")
    except Exception:
        payload = {}

    if approval.action == "set_grade":
        sub = session.get(Submission, approval.entity_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        marks = float(payload.get("awarded_marks") or 0)
        if marks < 0:
            raise HTTPException(status_code=409, detail="Grade cannot be negative")
        if sub.max_marks is not None and marks > float(sub.max_marks):
            raise HTTPException(status_code=409, detail=f"Grade exceeds max_marks ({sub.max_marks})")
        sub.awarded_marks = marks
        sub.feedback = payload.get("feedback")
        sub.status = "draft_graded"
        sub.graded_by = user.id
        sub.graded_at = datetime.now(timezone.utc)
        session.add(sub)
    elif approval.action == "approve_grade":
        sub = session.get(Submission, approval.entity_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        if sub.status != "draft_graded":
            raise HTTPException(status_code=409, detail="No draft grade to approve")
        sub.status = "graded"
        sub.approved_by = user.id
        sub.approved_at = datetime.now(timezone.utc)
        session.add(sub)
    elif approval.action == "publish_release":
        from services.release_service import publish_release
        course_code = payload.get("course_code")
        rel = publish_release(
            course_code=course_code,
            artifact_ref=payload.get("artifact_ref"),
            evidence_ref=payload.get("evidence_ref"),
            actor_id=user.id,
            session=session,
        )
        if rel is None:
            raise HTTPException(status_code=409, detail="Publication preconditions not met")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown approval action: {approval.action}")

    approval.status = "approved"
    approval.decided_by = user.id
    approval.decided_at = datetime.now(timezone.utc)
    session.add(approval)
    session.add(AuditLog(
        actor_id=user.id,
        action="openclaw_approval_approved",
        entity_type=approval.entity_type,
        entity_id=approval.entity_id or 0,
        details=f"Approved {approval.action} (approval_id={approval.id})",
    ))
    session.commit()
    return {"status": "approved", "approval_id": approval.id, "action": approval.action}


@router.post("/approvals/{approval_id}/reject")
def reject_openclaw_approval(
    approval_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    approval = _get_pending_approval(session, approval_id)
    if not _can_decide(session, user, approval):
        raise HTTPException(status_code=403, detail="You cannot reject this change")
    approval.status = "rejected"
    approval.decided_by = user.id
    approval.decided_at = datetime.now(timezone.utc)
    session.add(approval)
    session.add(AuditLog(
        actor_id=user.id,
        action="openclaw_approval_rejected",
        entity_type=approval.entity_type,
        entity_id=approval.entity_id or 0,
        details=f"Rejected {approval.action} (approval_id={approval.id})",
    ))
    session.commit()
    return {"status": "rejected", "approval_id": approval.id}
