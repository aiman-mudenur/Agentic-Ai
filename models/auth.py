from __future__ import annotations

from typing import Optional
from sqlmodel import Field, SQLModel


class UserBase(SQLModel):
    username: str = Field(index=True, unique=True)
    email: str = Field(index=True, unique=True)
    full_name: Optional[str] = None
    role: str = Field(default="student")  # "faculty" | "student" | "admin"
    course_code: str = Field(default="CS201")
    is_active: bool = Field(default=True)


class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
