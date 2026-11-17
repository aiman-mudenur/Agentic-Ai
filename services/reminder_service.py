from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from models.assignment import Assignment
from models.auth import Enrollment, User
from models.reminder import Reminder
from services.mailer import smtp_configured, send_login_code  # reuse mailer
import logging

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS = (7, 3, 1)  # days before deadline


def _now() -> datetime:
    return datetime.now(timezone.utc)


def send_deadline_reminders(
    session: Session,
    intervals: tuple[int, ...] = DEFAULT_INTERVALS,
    dry_run: bool = False,
) -> dict:
    """Send deadline reminders for published assignments.

    - Only targets published assignments with future deadlines.
    - Only targets enrolled students.
    - Prevents duplicate reminders for the same assignment/student/interval.
    - Records delivery attempts, outcomes, and timestamps.
    - Returns a summary dict.

    Does NOT start any background scheduler. Safe to call from tests or cron.
    """
    now = _now()
    summary = {"checked": 0, "sent": 0, "skipped": 0, "failed": 0, "errors": []}

    # Find published assignments with future deadlines
    assignments = session.exec(
        select(Assignment).where(Assignment.is_published == True)
    ).all()

    for assignment in assignments:
        if not assignment.due_date:
            continue
        due = assignment.due_date
        if isinstance(due, datetime):
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        else:
            # date object — convert to datetime
            due = datetime(due.year, due.month, due.day, tzinfo=timezone.utc)
        if due <= now:
            continue  # expired deadline, skip

        days_until = (due - now).days
        summary["checked"] += 1

        # Find enrolled students
        enrollments = session.exec(
            select(Enrollment).where(Enrollment.course_code == assignment.course_code)
        ).all()
        student_ids = [e.student_id for e in enrollments]

        for sid in student_ids:
            user = session.get(User, sid)
            if not user or not user.email:
                continue

            for interval in intervals:
                if days_until > interval:
                    continue  # not yet time for this interval
                if days_until < interval - 1:
                    continue  # past this interval window

                # Check for existing reminder
                existing = session.exec(
                    select(Reminder).where(
                        Reminder.assignment_id == assignment.id,
                        Reminder.student_id == sid,
                        Reminder.interval_days == interval,
                    )
                ).first()

                if existing:
                    if existing.status in ("sent", "skipped"):
                        summary["skipped"] += 1
                        continue
                    # Retry failed reminders
                    reminder = existing
                else:
                    reminder = Reminder(
                        assignment_id=assignment.id,
                        student_id=sid,
                        interval_days=interval,
                        status="pending",
                    )
                    session.add(reminder)
                    session.commit()
                    session.refresh(reminder)

                if dry_run:
                    reminder.status = "skipped"
                    session.add(reminder)
                    session.commit()
                    summary["skipped"] += 1
                    continue

                # Send email
                try:
                    if smtp_configured():
                        # Reuse mailer pattern — but mailer only sends login codes.
                        # For reminders, we'll use smtplib directly.
                        import smtplib
                        from email.message import EmailMessage
                        host = __import__("os").getenv("SMTP_HOST", "smtp.gmail.com")
                        port = int(__import__("os").getenv("SMTP_PORT", "587"))
                        smtp_user = __import__("os").getenv("SMTP_USER", "")
                        smtp_pwd = __import__("os").getenv("SMTP_APP_PASSWORD", "")
                        sender = __import__("os").getenv("SMTP_FROM", smtp_user)
                        if smtp_user and smtp_pwd:
                            msg = EmailMessage()
                            msg["Subject"] = f"Reminder: {assignment.title} due in {interval} day(s)"
                            msg["From"] = sender
                            msg["To"] = user.email
                            msg.set_content(
                                f"Hi {user.full_name or user.email},\n\n"
                                f"This is a reminder that the assignment '{assignment.title}' "
                                f"for {assignment.course_code} is due in {interval} day(s) "
                                f"(due: {assignment.due_date}).\n\n"
                                f"Please submit before the deadline.\n\n"
                                f"— Course Organizer"
                            )
                            with smtplib.SMTP(host, port) as server:
                                server.starttls()
                                server.login(smtp_user, smtp_pwd)
                                server.send_message(msg)
                            reminder.status = "sent"
                            reminder.sent_at = _now()
                            summary["sent"] += 1
                        else:
                            # SMTP not configured — record as skipped
                            reminder.status = "skipped"
                            summary["skipped"] += 1
                    else:
                        reminder.status = "skipped"
                        summary["skipped"] += 1
                except Exception as e:
                    reminder.status = "failed"
                    reminder.error = str(e)[:500]
                    summary["failed"] += 1
                    summary["errors"].append(f"assignment={assignment.id} student={sid} interval={interval}: {str(e)[:200]}")
                finally:
                    session.add(reminder)
                    session.commit()

    return summary
