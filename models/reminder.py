from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    assignment_id: int = Field(index=True)
    student_id: int = Field(index=True)
    interval_days: int = Field(default=1)  # days before deadline
    status: str = Field(default="pending")  # pending | sent | failed | skipped
    sent_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
