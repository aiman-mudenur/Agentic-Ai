#!/usr/bin/env python3
"""Complete end-to-end run of the Lab 8 parallel swarm.

Runs swarm (parallel) to gate, auto-approves, publishes. Also prints
swarm timing/quality and the published document path.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph import DECISION_APPROVE
from course_organizer.graph.swarm import resume_swarm, run_swarm
from course_organizer.graph.runner import GraphRun


def main() -> int:
    root = REPO / "out" / "lab08"
    root.mkdir(parents=True, exist_ok=True)
    run_id = "swarm-e2e"

    run: GraphRun = run_swarm(
        course_code="CS201",
        run_id=run_id,
        root=root,
        output_dir=root / run_id,
        parallel=True,
    )
    print("START:", run.summary())
    print("nodes:", " -> ".join(run.nodes_ran()))
    if run.state.get("swarm_timing"):
        t = run.state["swarm_timing"]
        print(f"  swarm: mode={t['mode']} tasks={t['tasks']} stage_ms={t['stage_ms']:.3f} span_ms={t['span_ms']:.3f} overlap={t['max_overlap']} quality={run.state.get('swarm_quality')}")

    if run.awaiting_gate:
        print("\n--- Gate awaiting (course_faculty) ---")
        resumed = resume_swarm(run_id=run_id, decision=DECISION_APPROVE, root=root)
        print("RESUMED:", resumed.summary())
        print("nodes:", " -> ".join(resumed.nodes_ran()))
        run = resumed

    if run.status.value == "published":
        pub = getattr(run, "published", None)
        print(f"\nPUBLISHED: {pub}")
        return 0

    print("\nDid not publish:", run.summary())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
