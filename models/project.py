from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True)
    title: str
    description: Optional[str] = None
    created_by: int = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectGroup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    group_id: str = Field(index=True)
    members: str = Field(default="")  # comma-separated student ids
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Milestone(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    group_id: str = Field(index=True)
    checkpoint_id: str
    name: str
    due_date: datetime
    expected_deliverable: str = Field(default="")
    status: str = Field(default="not_started")  # not_started|in_progress|submitted|accepted|overdue|blocked
    evidence: str = Field(default="")  # link or description of submitted evidence
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_by: Optional[int] = None
