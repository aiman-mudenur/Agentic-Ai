from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Release(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    course_code: str = Field(index=True)
    version: int = Field(default=1, index=True)
    artifact_ref: Optional[str] = None  # e.g. document_id, run_id, or path
    evidence_ref: Optional[str] = None  # reference to evidence/run metadata
    status: str = Field(default="current")  # current | superseded | rolled_back
    created_by: Optional[int] = Field(default=None, index=True)  # user who published
    approved_by: Optional[int] = Field(default=None, index=True)  # user who approved
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    audit_id: Optional[int] = Field(default=None, index=True)  # linked audit row


class ReleaseCreate(SQLModel):
    course_code: str
    version: int
    artifact_ref: Optional[str] = None
    evidence_ref: Optional[str] = None