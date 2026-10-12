#!/usr/bin/env python3
"""Complete end-to-end run of the Lab 7 pipeline.

Runs ingest -> plan -> workers -> validate -> assemble -> gate, then
auto-approves via resume_run (cross-process safe) and publishes. Outputs
the published document path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src is on path when run from repo root
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph import DECISION_APPROVE, run_pipeline, resume_run
from course_organizer.graph.runner import GraphRun


def main() -> int:
    root = REPO / "out" / "lab07"
    root.mkdir(parents=True, exist_ok=True)
    run_id = "full-e2e"

    # Start pipeline
    run: GraphRun = run_pipeline(
        course_code="CS201",
        run_id=run_id,
        root=root,
        output_dir=root / run_id,
        fail_until_attempt=0,
    )
    print("START:", run.summary())
    print("nodes:", " -> ".join(run.nodes_ran()))

    if run.awaiting_gate:
        print("\n--- Gate awaiting (course_faculty) ---")
        resumed = resume_run(run_id=run_id, decision=DECISION_APPROVE, root=root)
        print("RESUMED:", resumed.summary())
        print("nodes:", " -> ".join(resumed.nodes_ran()))
        run = resumed

    if run.status.value == "published":
        pub = getattr(run, "published", None)
        print(f"\nPUBLISHED: {pub}")
        return 0

    print("\nRun did not publish:", run.summary())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
