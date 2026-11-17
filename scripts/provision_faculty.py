#!/usr/bin/env python3
"""Provision a faculty / HoD / admin account in the local development database.

Public ``/auth/signup`` deliberately downgrades any privileged role request
to student (api/routers/auth.py), and Google sign-in does the same for the
role carried in the OAuth ``state`` (api/routers/google.py), so there is no
network path that creates a privileged account -- by design, not by accident.
The Lab 14 Phase 4 demo needs one faculty account to exercise the
role-guarded pipeline endpoints and the faculty dashboard, so this script
creates one directly, the same way an operator seeds an institution's first
accounts.

Safety:
- Local SQLite only (``course_organizer.db``); no external services.
- Idempotent: without ``--role`` an existing email is left untouched
  (prints its role). Only an explicit ``--role`` may change an account, so
  re-running a plain command can never demote a real HoD.
- Never prints or logs the password; it is hashed with the API's own
  pbkdf2_sha256 scheme before it touches the database.

Usage:
    # New account (password required):
    python -m scripts.provision_faculty --email fac@example.edu --password 'choose-a-pass'

    # New HoD / admin / student account:
    python -m scripts.provision_faculty --email you@gmail.com --password 'choose-a-pass' --role hod

    # Promote an account that already exists (e.g. one a Google sign-in
    # created as a student). No password needed -- it is never changed:
    python -m scripts.provision_faculty --email you@gmail.com --role faculty
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlmodel import Session, select  # noqa: E402

from api.security import get_password_hash  # noqa: E402
from db.engine import engine  # noqa: E402
from db.init_db import init_db  # noqa: E402
from models.auth import User, encode_roles  # noqa: E402


#: Every role the app understands (``models/auth.py``). ``admin`` is accepted
#: like ``hod``; the UI routes both to the HoD overview page.
ROLES = ("student", "faculty", "hod", "admin")


def provision(email: str, password: str | None = None, full_name: str = "",
              role: str | list[str] | None = None) -> int:
    """Create a local account (default role: faculty).

    ``role`` is an *operator* decision: pass a single role or a list of them,
    and the first one becomes the account's primary role while the rest are
    extra hats it may switch to at login. Left as ``None`` an existing account
    is never modified, so re-running the command stays idempotent. Passed
    explicitly on an account that already has different roles, it replaces
    them -- the only supported way to give an account (including one Google
    sign-in created as a student) a privileged role.

    ``password`` is only needed to *create* an account. It is never used to
    change an existing one, so an account that signs in through Google needs
    no local password at all.
    """
    email = email.strip().lower()
    if "@" not in email:
        print(f"[ERROR] Not a valid email: {email!r}")
        return 2
    if password is not None and len(password) < 6:
        print("[ERROR] Password must be at least 6 characters.")
        return 2

    requested = [role] if isinstance(role, str) else list(role or [])
    requested = [str(r).strip().lower() for r in requested if str(r).strip()]
    bad = [r for r in requested if r not in ROLES]
    if bad:
        print(f"[ERROR] Role must be one of: {', '.join(ROLES)} (got {', '.join(bad)})")
        return 2
    granted = list(dict.fromkeys(requested)) or ["faculty"]
    primary = granted[0]

    # Same idempotent bootstrap the API runs at startup, so this works on a
    # database the API has never opened.
    init_db()

    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == email)).first()
        if existing:
            # Only an explicit --role may change an account. The default never
            # does, so re-running the command cannot silently demote a HoD.
            if requested and existing.assigned_roles() != granted:
                previous = existing.assigned_roles()
                existing.role = primary
                existing.roles = encode_roles(granted)
                session.add(existing)
                session.commit()
                print(f"[OK] {email} roles changed: {', '.join(previous)} -> {', '.join(granted)}.")
                return 0
            print(f"[OK] {email} already exists with roles="
                  f"[{', '.join(existing.assigned_roles())}]; nothing changed.")
            return 0 if existing.role == "faculty" else 1

        if not password:
            print("[ERROR] --password is required when creating a new account (at least 6 characters).")
            return 2

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
            role=primary,
            roles=encode_roles(granted),
            hashed_password=get_password_hash(password),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        hats = f" (may also act as: {', '.join(granted[1:])})" if len(granted) > 1 else ""
        print(f"[OK] Provisioned {primary} account: {email} (username={username}, id={user.id}){hats}.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password",
        default=None,
        help="Only needed when creating a new account (min 6 chars). It is "
        "never used to change an existing one, so a Google-only account needs "
        "no password here.",
    )
    parser.add_argument("--full-name", default="")
    parser.add_argument(
        "--role",
        action="append",
        default=None,
        metavar="ROLE",
        help=(
            "Role to grant. Repeat it to grant several - the first becomes the "
            "primary role and the rest are extra hats the account may switch to "
            "at login, e.g. --role faculty --role hod. New accounts default to "
            "'faculty'. Passing --role explicitly also replaces the roles of an "
            "existing account (the only supported way to give a Google sign-in a "
            "privileged role); leaving it off never changes anything."
        ),
    )
    args = parser.parse_args()
    return provision(args.email, args.password, args.full_name, args.role)


if __name__ == "__main__":
    raise SystemExit(main())
