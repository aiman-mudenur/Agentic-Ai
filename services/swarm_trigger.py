from __future__ import annotations

import os
import time
from pathlib import Path
from typing Any


def _enabled() -> bool:
    return os.getenv("AUTO_SWARM_ON_SUBMIT", "true").lower() in ("1", "true", "yes")


def maybe_run_swarm_for_submission(course_code: str, assignment_id: int, submission_id: int) -> dict[str, Any] | None:
    """Run Lab 8 swarm in backend when a submission arrives. No UI needed.

    Best-effort: never raises; returns summary dict or None when disabled/failing.
    """
    if not _enabled():
        return None
    try:
        from services.graph_service import run_parallel_swarm
    except Exception:
        return None
    run_id = f"auto-submit-a{assignment_id}-s{submission_id}-{int(time.time())}"
    try:
        return run_parallel_swarm(course_code=course_code, run_id=run_id, parallel=True)
    except Exception:
        return None
