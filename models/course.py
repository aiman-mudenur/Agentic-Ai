from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Course(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(unique=True, index=True)
    title: str
    description: Optional[str] = None


class Unit(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True)
    unit_number: str
    title: str
    description: Optional[str] = None


class Note(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True)
    unit_number: Optional[str] = None
    title: str
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: int = Field(default=0)
    uploaded_by: int = Field(index=True)
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_url: Optional[str] = None  # Classroom/original link when there is no file
    version: int = Field(default=1)
    parent_note_id: Optional[int] = Field(default=None, foreign_key="note.id")
    is_current: bool = Field(default=True)
