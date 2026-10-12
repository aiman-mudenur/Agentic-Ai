from __future__ import annotations

from datetime import datetime, date, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Assignment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True)
    unit_number: Optional[str] = None
    title: str
    description: Optional[str] = None
    max_marks: float = Field(default=20.0)
    due_date: Optional[date] = None
    rubric_text: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    created_by: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_published: bool = Field(default=True)
