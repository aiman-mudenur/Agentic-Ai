from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User
from api.security import verify_password, create_access_token, get_password_hash, is_valid_email

router = APIRouter()


@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    ident = (form_data.username or "").strip()
    user = session.exec(select(User).where((User.email == ident) | (User.username == ident))).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email/username or password")
    token = create_access_token(
        {
            "sub": user.email,
            "email": user.email,
            "username": user.username,
            "role": user.role,
            "uid": user.id,
            "course_code": user.course_code,
        }
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "username": user.username,
        "email": user.email,
        "user_id": user.id,
        "course_code": user.course_code,
    }


@router.post("/signup")
def signup(payload: dict, session: Session = Depends(get_session)):
    """Register a real user with a real email id (e.g. xyz@gmail.com)."""
    email = str(payload.get("email", "") or "").strip().lower()
    password = str(payload.get("password", "") or "")
    full_name = str(payload.get("full_name", "") or "").strip()
    role = str(payload.get("role", "student") or "student").strip().lower()
    course_code = str(payload.get("course_code", "CS201") or "CS201").strip()
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email id (e.g. xyz@gmail.com)")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if role not in ("student", "faculty", "admin"):
        raise HTTPException(status_code=400, detail="Role must be student, faculty or admin")
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
        course_code=course_code,
        hashed_password=get_password_hash(password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    token = create_access_token(
        {
            "sub": user.email,
            "email": user.email,
            "username": user.username,
            "role": user.role,
            "uid": user.id,
            "course_code": user.course_code,
        }
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "username": user.username,
        "email": user.email,
        "user_id": user.id,
        "course_code": user.course_code,
    }
