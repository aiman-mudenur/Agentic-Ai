from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment, encode_roles
from api.deps import get_current_user
from api.security import verify_password, create_access_token, get_password_hash, is_valid_email

router = APIRouter()


def _course_codes(session: Session, user: User, active_role: str) -> list[str]:
    """Which course codes this identity may see while acting as ``active_role``."""
    enrollments = session.exec(select(Enrollment).where(Enrollment.student_id == user.id)).all()
    enrolled_course_codes = [e.course_code for e in enrollments]

    # For faculty, also get courses they teach (created assignments for)
    taught_course_codes = []
    if active_role == "faculty":
        from models.assignment import Assignment
        taught = session.exec(select(Assignment.course_code).where(Assignment.created_by == user.id)).all()
        taught_course_codes = list(set(taught))

    # HoD has access to all courses
    all_course_codes = enrolled_course_codes + taught_course_codes
    if active_role == "hod":
        from models.course import Course
        all_course_codes = [c.course_code for c in session.exec(select(Course)).all()]
    return all_course_codes


def _token_payload(session: Session, user: User, active_role: str) -> dict:
    course_codes = _course_codes(session, user, active_role)
    token = create_access_token(
        {
            "sub": user.email,
            "email": user.email,
            "username": user.username,
            "role": active_role,
            "uid": user.id,
            "course_codes": course_codes,
        }
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": active_role,
        "roles": user.assigned_roles(),
        "username": user.username,
        "email": user.email,
        "user_id": user.id,
        "course_codes": course_codes,
    }


@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    ident = (form_data.username or "").strip()
    user = session.exec(select(User).where((User.email == ident) | (User.username == ident))).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email/username or password")

    # Sign in acting as the account's primary role. An account granted several
    # roles can move between them with POST /auth/switch-role afterwards; it
    # can never switch to a role it does not hold.
    return _token_payload(session, user, user.role)


class RoleSwitchIn(BaseModel):
    role: str


@router.post("/switch-role")
def switch_role(
    payload: RoleSwitchIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Exchange the current token for one acting as a different assigned role.

    The target is validated against the roles stored on the account, so a
    client can only choose among roles an operator granted -- it cannot widen
    its own access.
    """
    target = (payload.role or "").strip().lower()
    assigned = user.assigned_roles()
    if target not in assigned:
        raise HTTPException(status_code=403, detail="That role is not assigned to this account")
    return _token_payload(session, user, target)


@router.post("/signup")
def signup(payload: dict, session: Session = Depends(get_session)):
    """Register a real user with a real email id (e.g. xyz@gmail.com).
    
    Public signup creates student accounts only. Privileged roles
    (faculty, hod, admin) are silently overridden to student to
    prevent privilege escalation from untrusted input.
    """
    email = str(payload.get("email", "") or "").strip().lower()
    password = str(payload.get("password", "") or "")
    full_name = str(payload.get("full_name", "") or "").strip()
    # Force student role for public signup; reject privileged role injection
    requested_role = str(payload.get("role", "student") or "student").strip().lower()
    if requested_role in ("faculty", "hod", "admin"):
        # Silently override to student — no privileged account created from public input
        role = "student"
    else:
        role = requested_role
    course_code = str(payload.get("course_code", "") or "").strip()
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email id (e.g. xyz@gmail.com)")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if role not in ("student", "faculty", "hod", "admin"):
        raise HTTPException(status_code=400, detail="Role must be student, faculty, hod or admin")
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered. Please login.")
    username = email.split("@")[0]
    base = username
    i = 1
    while session.exec(select(User).where(User.username == username)).first():
        i += 1
        username = f"{base}{i}"
    user = User(
        username=username,
        email=email,
        full_name=full_name or username,
        role=role,
        roles=encode_roles([role]),
        hashed_password=get_password_hash(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    
    # Create enrollment if course_code provided (lookup by code, not PK)
    if course_code and role == "student":
        from models.course import Course
        course = session.exec(select(Course).where(Course.course_code == course_code)).first()
        if course:
            session.add(Enrollment(
                student_id=user.id,
                course_code=course_code,
                enrolled_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            ))
            session.commit()
    
    return _token_payload(session, user, role)