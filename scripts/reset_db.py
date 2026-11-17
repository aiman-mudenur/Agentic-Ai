"""Reset the local demo database and reseed it.

Usage:
    python -m scripts.reset_db

Deletes course_organizer.db, recreates all tables and runs the explicit
idempotent demo seed (1 HoD, 2 faculty, 6 students, 2 courses).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "course_organizer.db"


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed {DB_PATH}")
    from db.init_db import init_db

    init_db()
    print("Tables recreated.")
    from scripts.seed_demo import seed_demo

    seed_demo()


if __name__ == "__main__":
    main()
