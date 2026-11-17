from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


class Submission(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    assignment_id: int = Field(index=True)
    student_id: int = Field(index=True)
    course_code: str = Field(index=True)
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: int = Field(default=0)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Write-once: submitted_at should never be updated after creation
    is_late: bool = Field(default=False)
    status: str = Field(default="submitted")  # submitted|draft_graded|graded|returned
    # draft_graded = faculty entered grade but not yet approved
    # graded = approved and published
    awarded_marks: Optional[float] = None
    max_marks: Optional[float] = None
    similarity_score: Optional[float] = None
    similarity_flagged: bool = Field(default=False)
    feedback: Optional[str] = None
    graded_by: Optional[int] = None
    graded_at: Optional[datetime] = None
    approved_by: Optional[int] = None  # HoD or faculty who approved
    approved_at: Optional[datetime] = None
    run_id: Optional[str] = None  # links to graph run

    # GitHub submission support (file uploads remain the default path)
    submission_type: str = Field(default="file")  # "file" | "github"
    github_repo_url: Optional[str] = None
    commit_hash: Optional[str] = None
    branch: Optional[str] = None
