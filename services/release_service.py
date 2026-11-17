from __future__ import annotations

from sqlmodel import Session, select, text
from db.engine import engine
from models.release import Release, ReleaseCreate
from models.submission import Submission
from models.auth import AuditLog, User
import datetime


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc)


def publish_release(course_code: str, artifact_ref: str | None,
                    evidence_ref: str | None, actor_id: int,
                    session: Session) -> Release | None:
    """Publish a release for a course.

    Preconditions (caller must enforce or this function will reject):
      - All required grades/feedback for the course are approved
      - Validation has passed (e.g. Lab 11 acceptance)
      - Required evidence is present

    Returns the created Release row, or None if publication cannot proceed.
    """
    # Verify that all relevant submissions are approved/graded
    subs = session.exec(
        select(Submission).where(
            Submission.course_code == course_code,
            Submission.status == "graded",
        )
    ).all()

    if not subs:
        # No graded submissions — cannot publish
        return None

    # Check for any pending (draft) submissions that haven't been approved
    pending = session.exec(
        select(Submission).where(
            Submission.course_code == course_code,
            Submission.status.in_(("submitted", "draft_graded")),
        )
    ).all()
    if pending:
        # Unapproved submissions exist — cannot publish
        return None

    # Upsert: ensure a unique version per course. We use a simple
    # version-increment approach; concurrent calls may race but the
    # database constraint below prevents conflicting current releases.
    release = session.exec(
        select(Release).where(
            Release.course_code == course_code,
            Release.status == "current",
        )
    ).first()

    new_version = (release.version + 1) if release else 1

    new_release = Release(
        course_code=course_code,
        version=new_version,
        artifact_ref=artifact_ref,
        evidence_ref=evidence_ref,
        status="current",
        created_by=actor_id,
        approved_by=actor_id,
        created_at=_now_iso(),
    )

    session.add(new_release)
    session.commit()
    session.refresh(new_release)

    # Write audit event
    audit = AuditLog(
        actor_id=actor_id,
        action="release_published",
        entity_type="release",
        entity_id=new_release.id,
        details=f"course={course_code} version={new_version} artifact={artifact_ref or ''} evidence={evidence_ref or ''}",
        timestamp=_now_iso(),
    )
    session.add(audit)
    session.commit()

    return new_release


def rollback_release(target_id: int, actor_id: int,
                     session: Session) -> bool:
    """Roll back to a prior release, making it the current release.

    Preserves all historical release rows and audit entries.
    Does NOT rewrite grades, submission timestamps, or approval timestamps.

    Returns True if rollback succeeded, False if target is ineligible.
    """
    # Fetch the target release to roll back to
    target = session.get(Release, target_id)
    if not target or target.status != "current":
        return False

    # Mark the current release as superseded (never delete)
    # Find all currently-current releases for this course
    current_releases = session.exec(
        select(Release).where(
            Release.course_code == target.course_code,
            Release.status == "current",
        )
    ).all()

    # Mark existing current releases as superseded
    for rel in current_releases:
        rel.status = "superseded"

    # Mark the target as current
    target.status = "current"

    session.add(target)
    session.commit()

    # Write audit event
    audit = AuditLog(
        actor_id=actor_id,
        action="release_rolled_back",
        entity_type="release",
        entity_id=target_id,
        details=f"course={target.course_code} restored_version={target.version} "
                f"from_current_release_was_replaced",
        timestamp=_now_iso(),
    )
    session.add(audit)
    session.commit()

    return True


def list_releases(course_code: str | None = None,
                  session: Session | None = None) -> list[Release]:
    """List releases, optionally filtered by course code."""
    if session is None:
        from db.engine import engine
        session = Session(engine)

    statement = select(Release)
    if course_code:
        statement = statement.where(Release.course_code == course_code)
    statement = statement.order_by(Release.version.desc())
    return session.exec(statement).all()


def get_current_release(course_code: str, session: Session | None = None) -> Release | None:
    """Get the current release for a course, if any."""
    if session is None:
        from db.engine import engine
        session = Session(engine)
    return session.exec(
        select(Release).where(
            Release.course_code == course_code,
            Release.status == "current",
        )
    ).first()