from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from db.session import get_session
from models.course import Course, Unit
from api.deps import get_current_user
from models.auth import User

router = APIRouter()


@router.get("/")
def list_courses(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    q = select(Course)
    if user.role == "student":
        q = q.where(Course.course_code == user.course_code)
    return session.exec(q).all()


@router.get("/{course_code}/units")
def list_units(course_code: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    cc = user.course_code if user.role == "student" else course_code
    return session.exec(select(Unit).where(Unit.course_code == cc)).all()
