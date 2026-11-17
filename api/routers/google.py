from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment
from api.deps import get_current_user
from api.security import create_access_token

router = APIRouter()


class ExchangeIn(BaseModel):
    code: str


def _oauth_unavailable(exc: Exception) -> HTTPException:
    from services import google_oauth as go

    msg = str(exc) or go.setup_instructions()
    return HTTPException(status_code=503, detail=msg)


@router.get("/login-url")
def google_login_url(role: str = "student"):
    """Consent URL for realtime Gmail login. Public (state carries the role)."""
    from services import google_oauth as go

    role = (role or "student").strip().lower()
    if role not in ("student", "faculty", "hod", "admin"):
        raise HTTPException(status_code=400, detail="Role must be student, faculty, hod or admin")
    try:
        return go.auth_url("login", {"role": role})
    except Exception as exc:
        raise _oauth_unavailable(exc)


@router.get("/callback", response_class=HTMLResponse)
def google_callback(code: str = "", state: str = ""):
    """Google redirects here. Login purpose mints a one-time code; the UI
    exchanges it for a JWT. Classroom purpose stores the token server-side."""
    import json as _json

    from services import google_oauth as go

    if not code:
        return HTMLResponse("<h3>Missing code.</h3><p>Please start again from the app.</p>", status_code=400)
    try:
        creds, payload = go.exchange_code(code, state)
    except Exception as exc:
        raise _oauth_unavailable(exc)

    purpose = payload.get("purpose", "login")
    if purpose == "classroom":
        uid = payload.get("uid")
        if not uid:
            return HTMLResponse("<h3>Missing user.</h3><p>Start the connect flow from the app while logged in.</p>", status_code=400)
        try:
            go.save_user_credentials(int(uid), creds)
        except Exception as exc:
            return HTMLResponse(f"<h3>Could not save token.</h3><p>{str(exc)[:300]}</p>", status_code=500)
        return HTMLResponse(
            "<h3>Google Classroom connected.</h3>"
            "<p>You can close this tab and return to the app. Press Refresh there.</p>"
        )

    # login purpose: verify identity, find-or-create local user, issue code
    try:
        claims = go.verify_id_token(creds)
    except Exception as exc:
        return HTMLResponse(f"<h3>Verification failed.</h3><p>{str(exc)[:300]}</p>", status_code=400)
    email = (claims.get("email") or "").strip().lower()
    if not email:
        return HTMLResponse("<h3>No email returned by Google.</h3>", status_code=400)
    # The role carried in the OAuth `state` is attacker-controlled (it is
    # editable in the address bar), so it can only ever *select* among the
    # roles an operator granted this account -- it can never grant one. A
    # brand-new account is always created as a student.
    requested_role = (payload.get("role") or "").strip().lower()

    from db.engine import engine
    from models.auth import User as UserModel, encode_roles

    with Session(engine) as session:
        user = session.exec(select(UserModel).where(UserModel.email == email)).first()
        if not user:
            from api.security import get_password_hash
            import secrets as _secrets

            username = email.split("@")[0]
            base, i = username, 1
            while session.exec(select(UserModel).where(UserModel.username == username)).first():
                i += 1
                username = f"{base}{i}"
            user = UserModel(
                username=username,
                email=email,
                full_name=(claims.get("name") or username),
                role="student",
                roles=encode_roles(["student"]),
                hashed_password=get_password_hash(_secrets.token_urlsafe(24)),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        assigned = user.assigned_roles()
        active = requested_role if requested_role in assigned else assigned[0]
        token = create_access_token(
            {"sub": user.email, "email": user.email, "username": user.username,
             "role": active, "uid": user.id, "course_codes": []}
        )
        one_time = go.issue_code(token)
    # Deliver the code to the user's Gmail when SMTP is configured;
    # otherwise fall back to showing it on this page.
    emailed = False
    try:
        from services.mailer import send_login_code, smtp_configured

        if smtp_configured():
            send_login_code(email, one_time)
            emailed = True
    except Exception:
        emailed = False
    if emailed:
        return HTMLResponse(
            "<h3>Signed in with Google as " + email + ".</h3>"
            "<p>A verification code was sent to your Gmail. "
            "Enter it in the app within 5 minutes. You can close this tab.</p>"
        )
    return HTMLResponse(
        "<h3>Signed in with Google as " + email + ".</h3>"
        "<p>Email sending is not configured, so here is your code. Copy it into the app:</p>"
        f"<h2><code>{one_time}</code></h2>"
        "<p>The code expires in 5 minutes. You can close this tab.</p>"
    )


@router.post("/exchange")
def google_exchange(payload: ExchangeIn):
    """Trade the one-time code from the callback page for our JWT."""
    from services import google_oauth as go

    jwt = go.redeem_code(payload.code or "")
    if not jwt:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    from api.security import decode_token

    claims = decode_token(jwt)
    out = {
        "access_token": jwt,
        "token_type": "bearer",
        "role": claims.get("role"),
        "username": claims.get("username"),
        "email": claims.get("email"),
        "user_id": claims.get("uid"),
    }
    # Tell the client which roles this account may act as, so a multi-role
    # account can offer a switcher. The list comes from the database, never
    # from the OAuth state.
    from db.engine import engine
    from models.auth import User as UserModel

    with Session(engine) as session:
        user = session.exec(select(UserModel).where(UserModel.email == claims.get("email"))).first()
        if user:
            out["roles"] = user.assigned_roles()
    return out
