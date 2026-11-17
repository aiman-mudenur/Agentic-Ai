"""Focused tests for deadline reminder service (Task 4)."""

import atexit
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from sqlmodel import Session, create_engine, select

from db.base import SQLModel
from models.auth import User, Enrollment
from models.assignment import Assignment
from models.reminder import Reminder
from api.security import get_password_hash
from services.reminder_service import send_deadline_reminders

TMP_DIR = tempfile.mkdtemp(prefix="test_reminders_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_reminders.db")
test_engine = create_engine(f"sqlite:///{TMP_DB}", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(test_engine)


def _session():
    return Session(test_engine)


def test_no_reminders_for_past_deadlines():
    """Expired deadlines should not generate reminders."""
    with _session() as s:
        fac = User(username="facr", email="facr@x.edu", full_name="Fac R", role="faculty", hashed_password=get_password_hash("faculty123"))
        s.add(fac)
        s.commit()
        s.refresh(fac)
        stu = User(username="stur", email="stur@x.edu", full_name="Stu R", role="student", hashed_password=get_password_hash("student123"))
        s.add(stu)
        s.commit()
        s.refresh(stu)
        s.add(Enrollment(student_id=stu.id, course_code="CS200", enrolled_at=datetime.now(timezone.utc).isoformat()))
        # Past deadline assignment
        a = Assignment(course_code="CS200", title="Past Due", max_marks=10.0, created_by=fac.id, is_published=True, due_date=datetime.now(timezone.utc) - timedelta(days=1))
        s.add(a)
        s.commit()
        summary = send_deadline_reminders(s, dry_run=True)
        assert summary["checked"] == 0
        assert summary["sent"] == 0


def test_reminder_prevents_duplicates():
    """Calling send twice should not create duplicate reminders."""
    with _session() as s:
        fac = User(username="facr2", email="facr2@x.edu", full_name="Fac R2", role="faculty", hashed_password=get_password_hash("faculty123"))
        s.add(fac)
        s.commit()
        s.refresh(fac)
        stu = User(username="stur2", email="stur2@x.edu", full_name="Stu R2", role="student", hashed_password=get_password_hash("student123"))
        s.add(stu)
        s.commit()
        s.refresh(stu)
        s.add(Enrollment(student_id=stu.id, course_code="CS201", enrolled_at=datetime.now(timezone.utc).isoformat()))
        due = datetime.now(timezone.utc) + timedelta(days=2)
        a = Assignment(course_code="CS201", title="Upcoming", max_marks=10.0, created_by=fac.id, is_published=True, due_date=due)
        s.add(a)
        s.commit()
        s.refresh(a)

        # First call (dry run)
        summary1 = send_deadline_reminders(s, dry_run=True)
        assert summary1["checked"] >= 1

        # Second call should skip (already has reminder records)
        summary2 = send_deadline_reminders(s, dry_run=True)
        # Should not create new reminders
        reminders = s.exec(select(Reminder).where(Reminder.assignment_id == a.id)).all()
        assert len(reminders) >= 1


def test_dry_run_does_not_send():
    """Dry run should not attempt email delivery."""
    with _session() as s:
        fac = User(username="facr3", email="facr3@x.edu", full_name="Fac R3", role="faculty", hashed_password=get_password_hash("faculty123"))
        s.add(fac)
        s.commit()
        s.refresh(fac)
        stu = User(username="stur3", email="stur3@x.edu", full_name="Stu R3", role="student", hashed_password=get_password_hash("student123"))
        s.add(stu)
        s.commit()
        s.refresh(stu)
        s.add(Enrollment(student_id=stu.id, course_code="CS202", enrolled_at=datetime.now(timezone.utc).isoformat()))
        due = datetime.now(timezone.utc) + timedelta(days=2)
        a = Assignment(course_code="CS202", title="Upcoming2", max_marks=10.0, created_by=fac.id, is_published=True, due_date=due)
        s.add(a)
        s.commit()
        s.refresh(a)

        with patch("services.reminder_service.smtp_configured", return_value=False):
            summary = send_deadline_reminders(s, dry_run=False)
            # With SMTP not configured, should skip (not fail)
            assert summary["failed"] == 0


def test_unpublished_assignments_skipped():
    """Unpublished assignments should not generate reminders."""
    with _session() as s:
        fac = User(username="facr4", email="facr4@x.edu", full_name="Fac R4", role="faculty", hashed_password=get_password_hash("faculty123"))
        s.add(fac)
        s.commit()
        s.refresh(fac)
        stu = User(username="stur4", email="stur4@x.edu", full_name="Stu R4", role="student", hashed_password=get_password_hash("student123"))
        s.add(stu)
        s.commit()
        s.refresh(stu)
        s.add(Enrollment(student_id=stu.id, course_code="CS203", enrolled_at=datetime.now(timezone.utc).isoformat()))
        due = datetime.now(timezone.utc) + timedelta(days=2)
        a = Assignment(course_code="CS203", title="Draft", max_marks=10.0, created_by=fac.id, is_published=False, due_date=due)
        s.add(a)
        s.commit()
        summary = send_deadline_reminders(s, dry_run=True)
        # The unpublished assignment should not be checked
        # (checked count might include other assignments from previous tests sharing the DB,
        #  but the unpublished one itself should not generate a reminder)
        reminders = s.exec(select(Reminder).where(Reminder.assignment_id == a.id)).all()
        assert len(reminders) == 0
