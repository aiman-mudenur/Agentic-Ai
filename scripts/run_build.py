#!/usr/bin/env python3
"""Tested build entry point (Lab 11).

Takes a locked-plan JSON file plus the course, runs the *existing* graph,
and judges acceptance. Exit 0 means ready_for_review; anything else prints
the blocking reason. Never resumes, approves, or publishes — a green build
only names the gate a faculty member may answer separately.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.build import BuildRequest, run_build


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", required=True,
                        help="Path to a locked-plan JSON file (LockedPlan dump).")
    parser.add_argument("--course", required=True, help="Course code, e.g. CS201.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--root", default=None,
                        help="Build run root (default: <repo>/out/lab11).")
    parser.add_argument("--fail-until-attempt", type=int, default=0,
                        help="Test-only fault injection; never for real builds.")
    args = parser.parse_args(argv)

    plan_path = Path(args.plan_json)
    if not plan_path.is_file():
        print(f"no such plan file: {plan_path}")
        return 1
    try:
        locked = json.loads(plan_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"plan file is not valid JSON: {exc}")
        return 1

    request = BuildRequest(
        locked_plan=locked,
        course_code=args.course,
        run_id=args.run_id,
        max_attempts=args.max_attempts,
        output_dir=args.output_dir,
        fail_until_attempt=args.fail_until_attempt,
    )
    root = Path(args.root) if args.root else REPO / "out" / "lab11"
    result = run_build(request, root=root)

    print(f"status: {result.status}")
    print(f"run_id: {result.run_id}")
    print(f"reason: {result.reason}")
    print(f"checks: {result.acceptance_report.checks_run if result.acceptance_report else []}")
    print(f"failed: {result.failed_checks}")
    print(f"gate_awaiting: {result.gate_awaiting}")
    print(f"can_advance: {result.can_advance}")
    print(f"published: {result.published}")
    if result.status == "ready_for_review":
        print("A faculty member may now answer the gate. This command never approves or publishes.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
