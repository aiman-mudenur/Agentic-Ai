"""CLI entry point for running deadline reminders.

Usage (production cron / Windows Task Scheduler):
    python -m scripts.run_reminders [--dry-run] [--intervals 7,3,1]

This script does NOT start a background scheduler. It runs once and exits.
Schedule it via cron or Task Scheduler at the desired cadence.

Example cron (daily at 8 AM):
    0 8 * * * cd /path/to/project && python -m scripts.run_reminders

Example Windows Task Scheduler:
    Program: python
    Arguments: -m scripts.run_reminders
    Start in: C:\\path\\to\\agentic-course-organizer
    Trigger: Daily at 8:00 AM
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.base import SQLModel
from db.engine import engine
from services.reminder_service import send_deadline_reminders


def main():
    parser = argparse.ArgumentParser(description="Run deadline reminders")
    parser.add_argument("--dry-run", action="store_true", help="Check only, no emails sent")
    parser.add_argument("--intervals", default="7,3,1", help="Comma-separated reminder intervals in days")
    args = parser.parse_args()

    intervals = tuple(int(x.strip()) for x in args.intervals.split(",") if x.strip())

    # Ensure tables exist
    SQLModel.metadata.create_all(engine)

    from sqlmodel import Session as SqlSession
    with SqlSession(engine) as session:
        summary = send_deadline_reminders(session, intervals=intervals, dry_run=args.dry_run)
        print(f"Reminder run complete: checked={summary['checked']} sent={summary['sent']} skipped={summary['skipped']} failed={summary['failed']}")
        if summary["errors"]:
            for err in summary["errors"]:
                print(f"  ERROR: {err}")


if __name__ == "__main__":
    main()
