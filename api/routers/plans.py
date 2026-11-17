from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.deps import get_current_user, require_role
from db.session import get_session
from models.auth import User, AuditLog
from models.plan import Plan

router = APIRouter()


@router.post("/", status_code=201)
def save_plan(
    plan_id: str,
    plan_hash: str,
    course_code: str,
    content: str = "",
    open_questions: str = "",
    revision_count: int = 0,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Save (or update) a draft plan. Only the creator can update a draft."""
    existing = session.exec(select(Plan).where(Plan.plan_id == plan_id)).first()
    if existing:
        if existing.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your plan")
        if existing.status != "draft":
            raise HTTPException(status_code=409, detail="Plan is already approved or locked")
        # Update content/hash if changed
        existing.plan_hash = plan_hash
        existing.content = content
        existing.open_questions = open_questions
        existing.revision_count = revision_count
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return {"id": existing.id, "plan_id": existing.plan_id, "status": existing.status,
                "plan_hash": existing.plan_hash, "course_code": existing.course_code,
                "revision_count": existing.revision_count, "created_at": existing.created_at.isoformat()}

    plan = Plan(
        plan_id=plan_id,
        plan_hash=plan_hash,
        course_code=course_code,
        content=content,
        open_questions=open_questions,
        revision_count=revision_count,
        created_by=user.id,
        status="draft",
    )
    session.add(plan)
    session.commit()
    session.refresh(plan)
    session.add(AuditLog(actor_id=user.id, action="plan_saved", entity_type="plan",
                        entity_id=plan.id or 0, details=f"plan_id={plan_id} course={course_code}"))
    session.commit()
    return {"id": plan.id, "plan_id": plan.plan_id, "status": plan.status,
            "plan_hash": plan.plan_hash, "course_code": plan.course_code,
            "revision_count": plan.revision_count, "created_at": plan.created_at.isoformat()}


@router.get("/")
def list_plans(
    course_code: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """List plans, optionally filtered by course code and/or status."""
    q = select(Plan)
    if user.role == "faculty":
        q = q.where(Plan.created_by == user.id)
    if course_code:
        q = q.where(Plan.course_code == course_code)
    if status:
        q = q.where(Plan.status == status)
    q = q.order_by(Plan.created_at.desc())
    plans = session.exec(q).all()
    return [
        {"id": p.id, "plan_id": p.plan_id, "plan_hash": p.plan_hash, "course_code": p.course_code,
         "status": p.status, "created_by": p.created_by, "approved_by": p.approved_by,
         "approved_at": p.approved_at.isoformat() if p.approved_at else None,
         "revision_count": p.revision_count, "created_at": p.created_at.isoformat(),
         "open_questions": p.open_questions}
        for p in plans
    ]


@router.get("/{plan_id}")
def get_plan(plan_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Get a single plan by plan_id."""
    plan = session.exec(select(Plan).where(Plan.plan_id == plan_id)).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    # Scope: faculty can only see their own; hod/admin see all; students cannot
    if user.role == "student":
        raise HTTPException(status_code=403, detail="Students cannot view plans")
    if user.role == "faculty" and plan.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your plan")
    return {
        "id": plan.id, "plan_id": plan.plan_id, "plan_hash": plan.plan_hash,
        "course_code": plan.course_code, "status": plan.status, "content": plan.content,
        "open_questions": plan.open_questions, "revision_count": plan.revision_count,
        "created_by": plan.created_by, "approved_by": plan.approved_by,
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "created_at": plan.created_at.isoformat(), "updated_at": plan.updated_at.isoformat(),
    }


@router.post("/{plan_id}/approve", status_code=200)
def approve_plan(
    plan_id: str,
    plan_hash: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Approve a plan. Requires the plan hash to match (prevents stale approval).

    Records an audit event. Does NOT lock or publish — those are separate steps.
    """
    plan = session.exec(select(Plan).where(Plan.plan_id == plan_id)).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if user.role == "faculty" and plan.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your plan")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail=f"Plan is already {plan.status}; cannot approve")
    if plan.plan_hash != plan_hash:
        raise HTTPException(status_code=409, detail="Plan hash mismatch — plan has changed since it was reviewed. Re-review before approving.")

    plan.status = "approved"
    plan.approved_by = user.id
    plan.approved_at = datetime.now(timezone.utc)
    plan.updated_at = datetime.now(timezone.utc)
    session.add(plan)
    session.commit()
    session.refresh(plan)

    audit = AuditLog(
        actor_id=user.id,
        action="plan_approved",
        entity_type="plan",
        entity_id=plan.id or 0,
        details=f"plan_id={plan_id} hash={plan_hash[:16]} revision_count={plan.revision_count}",
        timestamp=datetime.now(timezone.utc),
    )
    session.add(audit)
    session.commit()

    return {"status": "approved", "plan_id": plan_id, "approved_by": user.id,
            "approved_at": plan.approved_at.isoformat()}


@router.post("/{plan_id}/lock", status_code=200)
def lock_plan(
    plan_id: str,
    plan_hash: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    """Lock a plan (Lab 3). Requires approved status and matching hash."""
    plan = session.exec(select(Plan).where(Plan.plan_id == plan_id)).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if user.role == "faculty" and plan.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your plan")
    if plan.status != "approved":
        raise HTTPException(status_code=409, detail=f"Plan must be approved before locking (current: {plan.status})")
    if plan.plan_hash != plan_hash:
        raise HTTPException(status_code=409, detail="Plan hash mismatch — plan has changed since approval.")

    plan.status = "locked"
    plan.updated_at = datetime.now(timezone.utc)
    session.add(plan)
    session.commit()
    session.refresh(plan)

    audit = AuditLog(
        actor_id=user.id,
        action="plan_locked",
        entity_type="plan",
        entity_id=plan.id or 0,
        details=f"plan_id={plan_id} hash={plan_hash[:16]}",
        timestamp=datetime.now(timezone.utc),
    )
    session.add(audit)
    session.commit()

    return {"status": "locked", "plan_id": plan_id, "locked_by": user.id,
            "locked_at": plan.updated_at.isoformat()}
