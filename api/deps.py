from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User
from api.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

def get_current_user(token: str = Depends(oauth2_scheme), session: Session = Depends(get_session)) -> User:
    payload = decode_token(token)
    username_or_email = payload.get("sub") or payload.get("email")
    user = session.exec(select(User).where((User.email == username_or_email) | (User.username == username_or_email))).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive or invalid user")
    return user

def require_role(*roles):
    def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user
    return checker
