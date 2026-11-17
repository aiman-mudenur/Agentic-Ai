from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment, AuditLog
from models.course import Course, Unit
from api.deps import get_current_user, require_role

router = APIRouter()


class UnitIn(BaseModel):
    unit_number: str
    title: str
    description: str | None = None


class CourseCreate(BaseModel):
    course_code: str
    title: str
    description: str | None = None
    units: list[UnitIn] = []


@router.get("/")
def list_courses(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if user.role == "student":
        # Return only courses the student is enrolled in
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id)).all()
        if not enrolled:
            return []
        return session.exec(select(Course).where(Course.course_code.in_(enrolled))).all()
    elif user.role == "faculty":
        # Return courses the faculty teaches (has created assignments for)
        from models.assignment import Assignment
        taught = session.exec(select(Assignment.course_code).where(Assignment.created_by == user.id)).all()
        if not taught:
            return []
        return session.exec(select(Course).where(Course.course_code.in_(taught))).all()
    else:
        # HoD sees all courses
        return session.exec(select(Course)).all()


@router.get("/catalog")
def course_catalog(session: Session = Depends(get_session)):
    """Full semester course catalog — public (no login needed to browse).
    Students register after logging in; faculty create courses after login."""
    courses = session.exec(select(Course)).all()
    return [
        {
            "course_code": c.course_code,
            "title": c.title,
            "description": c.description,
            "enrolled": False,
        }
        for c in courses
    ]


@router.get("/my/enrollments")
def my_enrollments(session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    rows = session.exec(select(Enrollment).where(Enrollment.student_id == user.id)).all()
    return [{"course_code": e.course_code, "enrolled_at": e.enrolled_at} for e in rows]


@router.post("/")
def create_course(payload: CourseCreate, session: Session = Depends(get_session), user: User = Depends(require_role("faculty"))):
    """Open course creation for faculty — no restrictions."""
    code = (payload.course_code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="course_code is required")
    if session.exec(select(Course).where(Course.course_code == code)).first():
        raise HTTPException(status_code=409, detail="Course already exists")
    course = Course(course_code=code, title=(payload.title or "").strip() or code, description=(payload.description or "").strip() or None)
    session.add(course)
    for u in payload.units:
        session.add(Unit(course_code=code, unit_number=u.unit_number.strip(), title=u.title.strip(), description=(u.description or "").strip() or None))
    session.commit()
    session.refresh(course)
    try:
        session.add(AuditLog(actor_id=user.id, action="course_created", entity_type="course", entity_id=course.id or 0, details=f"Faculty {user.email} created course {code}"))
        session.commit()
    except Exception:
        pass
    return {"course_code": course.course_code, "title": course.title, "description": course.description}


@router.post("/{course_code}/enroll")
def enroll(course_code: str, session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    """Self-enrollment — students may register for any number of listed courses."""
    course = session.exec(select(Course).where(Course.course_code == course_code)).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found in catalog")
    existing = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == course_code)).first()
    if existing:
        return {"course_code": course_code, "enrolled": True, "already": True}
    session.add(Enrollment(student_id=user.id, course_code=course_code, enrolled_at=datetime.now(timezone.utc).isoformat()))
    session.commit()
    try:
        session.add(AuditLog(actor_id=user.id, action="enrolled", entity_type="course", entity_id=0, details=f"Student {user.email} enrolled in {course_code}"))
        session.commit()
    except Exception:
        pass
    return {"course_code": course_code, "enrolled": True, "already": False}


@router.delete("/{course_code}/enroll")
def unenroll(course_code: str, session: Session = Depends(get_session), user: User = Depends(require_role("student"))):
    row = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == course_code)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not enrolled in this course")
    session.delete(row)
    session.commit()
    return {"course_code": course_code, "enrolled": False}


@router.get("/{course_code}/units")
def list_units(course_code: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    # Verify access to this course
    _verify_course_access(user, course_code, session)
    return session.exec(select(Unit).where(Unit.course_code == course_code)).all()


def _verify_course_access(user: User, course_code: str, session: Session) -> None:
    if user.role == "student":
        enrolled = session.exec(select(Enrollment).where(Enrollment.student_id == user.id, Enrollment.course_code == course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty":
        # Open access: any existing course (faculty create freely, import freely)
        course = session.exec(select(Course).where(Course.course_code == course_code)).first()
        if not course:
            raise HTTPException(status_code=404, detail="Course not found")
    # HoD has access to all
