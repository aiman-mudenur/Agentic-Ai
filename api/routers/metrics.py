from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, func

from api.deps import get_current_user, get_session, require_role
from models.auth import User, AuditLog
from models.release import Release
from models.submission import Submission


router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/overview")
def metrics_overview(
    user: User = Depends(require_role("faculty", "hod", "admin")),
    session: Session = Depends(get_session),
):
    """Read-only overview of release and grade metrics.

    Aggregate-only — no individual student identifiers, emails, or
    submissions are included. Returns ``unavailable`` for any metric
    that cannot be computed reliably.
    """
    # Total releases across all courses
    total_releases = session.exec(
        select(func.count(Release.id))
    ).one_or_none() or 0

    # Releases by status
    by_status = session.exec(
        select(Release.status, func.count(Release.id))
        .group_by(Release.status)
    ).all()
    status_counts = {status: count for status, count in by_status}

    # Total graded submissions (aggregate)
    total_graded = session.exec(
        select(func.count(Submission.id))
        .where(Submission.status == "graded")
    ).one_or_none() or 0

    # Total audit events
    total_audit = session.exec(
        select(func.count(AuditLog.id))
    ).one_or_none() or 0

    # Per-course release count (aggregated, no student data)
    course_release_rows = session.exec(
        select(Release.course_code, func.count(Release.id))
        .group_by(Release.course_code)
    ).all()
    course_release_counts = {
        course: count for course, count in course_release_rows
    }

    # Helper: safe division returning unavailable when denominator is 0
    def safe_div(numerator: int, denominator: int):
        if denominator == 0:
            return "unavailable"
        return round(numerator / denominator, 2)

    # Release pass rate: graded submissions / total releases (if any)
    pass_rate = safe_div(int(total_graded), int(total_releases))

    return {
        "total_releases": int(total_releases),
        "releases_by_status": status_counts,
        "total_graded_submissions": int(total_graded),
        "total_audit_events": int(total_audit),
        "course_release_counts": course_release_counts,
        "release_pass_rate": pass_rate,
    }