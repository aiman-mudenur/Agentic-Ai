from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class OpenClawApproval(SQLModel, table=True):
    """A proposed grade/publish change from the AI assistant, pending human approval.

    The assistant never applies these directly. A faculty/HoD must explicitly
    approve (or reject) the row; only then does the backend perform the change
    through the existing grade/approve/release services.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    requested_by: int = Field(index=True)  # user who asked the assistant
    action: str = Field(index=True)  # e.g. set_grade, approve_grade, publish_release
    entity_type: str = Field(default="submission")  # submission | release
    entity_id: Optional[int] = None
    payload: Optional[str] = None  # JSON args for the action
    reason: Optional[str] = None  # assistant-provided explanation
    status: str = Field(default="pending")  # pending | approved | rejected
    decided_by: Optional[int] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
