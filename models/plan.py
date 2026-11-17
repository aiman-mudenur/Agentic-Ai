from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Plan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: str = Field(index=True, unique=True)  # e.g. PLAN-CS201-ABCD
    plan_hash: str = Field(index=True)  # SHA-256 of plan content
    course_code: str = Field(index=True)
    status: str = Field(default="draft")  # draft | approved | locked
    created_by: int = Field(index=True)  # user who created the plan
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    revision_count: int = Field(default=0)
    content: str = Field(default="")  # JSON serialized plan outline
    open_questions: str = Field(default="")  # comma-separated open questions
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
