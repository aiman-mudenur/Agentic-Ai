from __future__ import annotations

import json
from typing import Optional

from sqlmodel import Field, SQLModel

#: Every role the application recognises, in increasing order of authority.
KNOWN_ROLES: tuple[str, ...] = ("student", "faculty", "hod", "admin")


def encode_roles(roles) -> str:
    """Normalise a role collection into the JSON stored in ``user.roles``.

    Values are lower-cased, de-duplicated (order preserved) and filtered
    against :data:`KNOWN_ROLES`, so operator tooling cannot smuggle an
    unknown string into the authorization path.
    """
    values = [str(r).strip().lower() for r in roles or []]
    values = [r for r in values if r in KNOWN_ROLES]
    if not values:
        raise ValueError(f"Need at least one known role: {', '.join(KNOWN_ROLES)}")
    return json.dumps(list(dict.fromkeys(values)))


class UserBase(SQLModel):
    username: str = Field(index=True, unique=True)
    email: str = Field(index=True, unique=True)
    full_name: Optional[str] = None
    role: str = Field(default="student")  # "student" | "faculty" | "hod" | "admin"
    roles: Optional[str] = Field(default=None)  # JSON list, e.g. '["faculty","hod"]'
    is_active: bool = Field(default=True)


class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str

    def assigned_roles(self) -> list[str]:
        """Every role the operator has granted this account.

        ``roles`` is written only by operator tooling (``scripts/provision_faculty.py``
        and ``scripts/seed_demo.py``); no network request can add to it. Accounts
        created before multi-role support have it NULL and fall back to the single
        ``role`` column, so nothing needs migrating by hand.
        """
        raw = (self.roles or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                values = [str(r).strip().lower() for r in parsed]
                values = [r for r in values if r in KNOWN_ROLES]
                if values:
                    return list(dict.fromkeys(values))
        base = (self.role or "").strip().lower()
        return [base] if base in KNOWN_ROLES else ["student"]


class Enrollment(SQLModel, table=True):
    """Links students to courses. A student can be enrolled in multiple courses."""
    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: int = Field(foreign_key="user.id", index=True)
    course_code: str = Field(foreign_key="course.course_code", index=True)
    enrolled_at: str  # ISO timestamp


class AuditLog(SQLModel, table=True):
    """Immutable audit trail for grade publishing, approvals, and critical actions."""
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: int = Field(index=True)  # user who performed the action
    action: str = Field(index=True)  # e.g., "grade_published", "grade_approved", "submission_created"
    entity_type: str = Field(index=True)  # "submission", "assignment", "note", "enrollment"
    entity_id: int = Field(index=True)
    details: Optional[str] = None  # JSON or text description
    timestamp: str = Field(default_factory=lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
