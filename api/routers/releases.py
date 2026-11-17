from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.deps import get_current_user, get_session, require_role
from api.security import decode_token
from db.session import get_session as get_new_session
from models.auth import User
from models.release import Release, ReleaseCreate
from models.submission import Submission
from services.release_service import (
    publish_release,
    rollback_release,
    list_releases,
    get_current_release,
)
from services.progress_service import recompute_progress


router = APIRouter(tags=["releases"])


@router.post("/publish", status_code=201)
def release_publish(
    payload: ReleaseCreate,
    user: User = Depends(require_role("faculty", "hod", "admin")),
    session: Session = Depends(get_session),
):
    """Publish a versioned release of an approved artifact.

    Requires trusted faculty approval, successful validation, and
    required evidence. A passing Lab 11 build is not approval.
    """
    # Use the provided course_code from payload; ensure the user has
    # scope over this course. Faculty can only publish for courses they
    # own; HoD/admin can publish for any course.
    course_code = payload.course_code

    # Scope check: faculty may only publish for courses they created
    if user.role == "faculty":
        from models.assignment import Assignment
        taught = session.exec(
            select(Assignment.course_code).where(
                Assignment.created_by == user.id
            )
        ).all()
        taught_codes = list(set(taught))
        if course_code not in taught_codes:
            raise HTTPException(
                status_code=403,
                detail="Faculty may only publish releases for their own courses",
            )

    # Attempt publication; returns None if preconditions not met
    rel = publish_release(
        course_code=course_code,
        artifact_ref=payload.artifact_ref,
        evidence_ref=payload.evidence_ref,
        actor_id=user.id,
        session=session,
    )

    if rel is None:
        raise HTTPException(
            status_code=409,
            detail="Publication rejected: all grades must be approved, "
                   "validation must pass, and required evidence must be present",
        )

    return {"id": rel.id, "version": rel.version, "course_code": rel.course_code,
            "status": rel.status, "created_at": rel.created_at}


@router.post("/rollback/{release_id}", status_code=200)
def release_rollback(
    release_id: int,
    user: User = Depends(require_role("faculty", "hod", "admin")),
    session: Session = Depends(get_session),
):
    """Roll back to a prior release, making it the current release.

    Grades, submission timestamps, and approval timestamps remain
    unchanged. Audit history is preserved.
    """
    success = rollback_release(release_id, user.id, session)
    if not success:
        raise HTTPException(
            status_code=404,
            detail="Release not found or not eligible for rollback",
        )
    return {"detail": "rollback completed", "release_id": release_id}


@router.get("/")
def list_releases_endpoint(
    course_code: str | None = None,
    session: Session = Depends(get_session),
):
    """List releases, optionally filtered by course code."""
    return list_releases(course_code=course_code, session=session)


@router.get("/current/{course_code}")
def current_release(
    course_code: str,
    session: Session = Depends(get_session),
):
    """Get the current release for a course, if any."""
    rel = get_current_release(course_code, session)
    if rel is None:
        return {"detail": "no current release for this course"}
    return {
        "id": rel.id,
        "version": rel.version,
        "course_code": rel.course_code,
        "status": rel.status,
        "artifact_ref": rel.artifact_ref,
        "evidence_ref": rel.evidence_ref,
        "created_by": rel.created_by,
        "created_at": rel.created_at,
    }