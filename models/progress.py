from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class StudentProgress(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    student_id: int = Field(index=True)
    course_code: str = Field(index=True)
    unit_number: Optional[str] = None
    assignment_id: Optional[int] = None
    status: str = Field(default="not_started")  # not_started|in_progress|submitted|graded|completed
    marks_obtained: Optional[float] = None
    max_marks: Optional[float] = None
    completion_pct: float = Field(default=0.0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
