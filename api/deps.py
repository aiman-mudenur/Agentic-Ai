from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm.attributes import set_committed_value
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
    # The token names which of the account's roles it is acting as. That choice
    # is re-checked against the database on every request, so an operator
    # demoting an account takes effect immediately and a token can never name a
    # role the operator did not grant.
    assigned = user.assigned_roles()
    active = str(payload.get("role") or "").strip().lower() or assigned[0]
    if active not in assigned:
        raise HTTPException(
            status_code=401,
            detail="This token's role is no longer assigned to the account",
        )
    if active != user.role:
        # Reflect the active role for this request WITHOUT dirtying the column:
        # routers call session.commit() mid-request, and a dirty value would
        # overwrite the account's base role in the database.
        set_committed_value(user, "role", active)
    return user

def require_role(*roles):
    def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user
    return checker
