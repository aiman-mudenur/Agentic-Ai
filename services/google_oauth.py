"""Google OAuth for realtime Gmail login + per-user Classroom tokens.

Uses the repo's OAuth client file (``credentials.json``, installed-type) with an
explicit redirect URI. Secrets never touch the repo: per-user tokens live under
``data/tokens/`` (gitignored). When the client file is absent every entry point
fails closed with setup instructions instead of a traceback.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_DIR = REPO_ROOT / "data" / "tokens"

# Google may return a slightly different granted-scope set than requested
# (previously granted scopes get merged in). Relax the strict equality check
# so the exchange succeeds; API calls still fail cleanly per-scope if needed.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Same read-only scopes Lab 5's GoogleWorkspaceAdapter was verified against,
# plus the three the import flow additionally needs: student-side coursework
# (.me.readonly — a student token gets 403 on the teacher .students scope),
# topics, and announcements (teachers often share materials as announcements).
CLASSROOM_SCOPES = LOGIN_SCOPES + [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    # Drive file bytes for import (read-only; native Docs/Sheets/Slides are exported to PDF).
    "https://www.googleapis.com/auth/drive.readonly",
]

# One-time login codes: code -> (jwt, expires_at). Single-process dev store.
_CODES: dict[str, tuple[str, float]] = {}

# PKCE verifiers: state -> (code_verifier, expires_at). The consent URL carries
# a code_challenge, so the callback must present the matching verifier; the
# flow object itself does not survive across the two HTTP requests.
_VERIFIERS: dict[str, tuple[str, float]] = {}


def client_file() -> Path:
    name = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials.json")
    p = Path(name)
    return p if p.is_absolute() else (REPO_ROOT / name)


def redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/google/callback")


def client_config() -> dict[str, Any] | None:
    p = client_file()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def setup_instructions() -> str:
    return (
        "Google OAuth is not configured. Steps: 1) Google Cloud Console -> APIs & Services "
        "-> enable 'Google Classroom API'. 2) Credentials -> Create OAuth client ID "
        "(Desktop app) -> download JSON as credentials.json in the project root "
        "(or set GOOGLE_OAUTH_CLIENT_FILE). 3) Add Authorized redirect URI: "
        f"{redirect_uri()} 4) Restart the API."
    )


def make_flow(scopes: list[str], state: str):
    """Build the OAuth flow. Raises RuntimeError when not configured."""
    cfg = client_config()
    if cfg is None:
        raise RuntimeError(setup_instructions())
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(cfg, scopes=scopes, state=state, redirect_uri=redirect_uri())


def auth_url(purpose: str, extra: dict[str, Any] | None = None) -> dict[str, str]:
    """Return a Google consent URL. ``extra`` is round-tripped via ``state``."""
    # Unique state per link: identical payloads would otherwise share one dict
    # entry, and a second click overwrites the first tab's PKCE verifier
    # ("Invalid code verifier" on the stale tab).
    state = json.dumps({"purpose": purpose, "nonce": secrets.token_urlsafe(16), **(extra or {})})
    scopes = CLASSROOM_SCOPES if purpose == "classroom" else LOGIN_SCOPES
    flow = make_flow(scopes, state)
    # No include_granted_scopes: ask for exactly these scopes so the granted
    # set matches, and prompt=consent so we always get a refresh token.
    url, returned_state = flow.authorization_url(
        access_type="offline", prompt="consent"
    )
    # Keep the PKCE verifier for the callback (fresh Flow objects lose it).
    try:
        if getattr(flow, "code_verifier", None):
            now = time.time()
            for k in [k for k, (_, exp) in _VERIFIERS.items() if exp < now]:
                _VERIFIERS.pop(k, None)
            _VERIFIERS[returned_state] = (flow.code_verifier, now + 600)
    except Exception:
        pass
    return {"auth_url": url, "state": returned_state}


def exchange_code(code: str, state: str) -> Any:
    """Exchange an auth code for OAuth credentials (caller persists if needed)."""
    try:
        payload = json.loads(state)
    except Exception:
        payload = {}
    purpose = payload.get("purpose", "login")
    scopes = CLASSROOM_SCOPES if purpose == "classroom" else LOGIN_SCOPES
    flow = make_flow(scopes, state)
    item = _VERIFIERS.pop(state, None)
    if item is None:
        raise RuntimeError(
            "This sign-in link expired (open a fresh 'Continue with Google' link from the app "
            "and approve again — codes are single-use and the server forgets them on restart)."
        )
    try:
        flow.code_verifier = item[0]
    except Exception:
        pass
    flow.fetch_token(code=code)
    return flow.credentials, payload


def verify_id_token(credentials) -> dict[str, Any]:
    """Return the verified ID-token claims (email, name, ...)."""
    cfg = client_config() or {}
    client_id = (cfg.get("installed") or cfg.get("web") or {}).get("client_id")
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    if not credentials.id_token:
        raise RuntimeError("Google did not return an ID token. Try signing in again.")
    return id_token.verify_oauth2_token(credentials.id_token, Request(), client_id)


def issue_code(jwt: str, ttl_s: int = 300) -> str:
    code = secrets.token_urlsafe(24)
    _CODES[code] = (jwt, time.time() + ttl_s)
    return code


def redeem_code(code: str) -> str | None:
    item = _CODES.pop(code.strip(), None)
    if not item:
        return None
    jwt, exp = item
    return jwt if time.time() <= exp else None


def token_path_for(user_id: int) -> Path:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    return TOKEN_DIR / f"classroom_{int(user_id)}.json"


def save_user_credentials(user_id: int, credentials) -> Path:
    p = token_path_for(user_id)
    p.write_text(credentials.to_json())
    return p


def load_user_credentials(user_id: int):
    """Load + refresh per-user Classroom credentials, or None when absent."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    p = token_path_for(user_id)
    if not p.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(p), CLASSROOM_SCOPES)
    except Exception:
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            p.write_text(creds.to_json())
    except Exception:
        return None
    return creds if creds.valid else None
