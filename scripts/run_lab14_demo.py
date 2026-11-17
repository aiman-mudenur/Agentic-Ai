#!/usr/bin/env python3
"""Lab 14 Phase 4 -- repeatable end-to-end capstone demo.

Runs the real pipeline (ingest -> plan -> parallel workers -> domain
specialist -> validation/repair -> faculty approval gate -> publication)
against the local seed corpus, using the same ``run_pipeline`` /
``resume_run`` entry points the API uses. Nothing here is simulated: the
graph suspends on the genuine LangGraph interrupt, and publication happens
only after an explicit decision is fed back through the gate.

The default action is to *hold* at the gate: the script prints the pending
run id and the reviewer's checklist, then exits without approving. Resume
in a second invocation (or the same one) with an explicit decision:

    python -m scripts.run_lab14_demo --domain lab
    python -m scripts.run_lab14_demo --resume demo-lab-a1b2c3d4 --decision approve
    python -m scripts.run_lab14_demo --resume demo-lab-a1b2c3d4 --decision refuse

Every run gets its own checkpoint directory under ``out/demo/lab14`` by
default (configurable with ``--root``), so demos never inherit another
run's checkpoint state and never touch the API's ``out/lab07`` runs.

Exit codes: 0 success (published, or cleanly refused); 1 the run did not
publish (escalated, specialist failed); 2 governance blocked publication
(approve was attempted on a run whose specialist failed); 3 usage error.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph import (  # noqa: E402
    DECISION_APPROVE,
    DECISION_REFUSE,
    load_gate_request,
    resume_run,
    run_pipeline,
)
from course_organizer.graph.runner import GraphRun  # noqa: E402

VALID_DOMAINS = ("theory", "lab", "project")
VALID_DECISIONS = ("approve", "refuse")


def _print_run_banner(run: GraphRun, root: Path) -> None:
    print("=" * 72)
    print(f"  run id:        {run.run_id}")
    print(f"  status:        {run.status.value}")
    print(f"  nodes ran:     {' -> '.join(run.nodes_ran()) or 'none'}")
    print(f"  attempts:      {run.attempts_made()}")
    print(f"  checkpoint:    {root / run.run_id / 'graph.sqlite'}")
    print(f"  artifact dir:  {root / run.run_id}")
    print("=" * 72)


def _print_gate_request(run: GraphRun) -> None:
    req = run.gate_request
    if req is None:
        print("[!] No gate request on this run -- it did not reach the gate.")
        return
    print("\n--- PENDING FACULTY REVIEW (the graph is suspended here) ---")
    print(f"  course:            {req.course_code} {req.course_name}")
    print(f"  domain:            {req.domain}")
    print(f"  specialist ok:     {req.specialist_ok}"
          + (f"  (domain error: {req.domain_error})" if req.domain_error else ""))
    print(f"  validation:        passed={req.validation.passed} "
          f"attempt={req.validation.attempt}")
    if req.validation.failing:
        print(f"  failing checks:    {', '.join(req.validation.failing)}")
    print(f"  draft grades:      {len(req.draft_grades)}")
    print(f"  similarity flags:  {len(req.similarity_flags)}")
    print(f"  units covered:     {', '.join(req.units_covered) or 'none'}")
    print(f"  assignments:       {', '.join(req.assignments_included) or 'none'}")
    print(f"  summary:           {req.summary}")
    audit = run.state.get("audit_trail", []) or []
    if audit:
        print("  audit trail:")
        for entry in audit[-5:]:
            print(f"    - {entry}")


def _print_artifacts(run: GraphRun, root: Path) -> None:
    out_dir = root / run.run_id
    print(f"\nArtifacts in {out_dir}:")
    if not out_dir.is_dir():
        print("  (no directory -- nothing was written)")
        return
    files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    if not files:
        print("  (empty)")
        return
    for f in files:
        print(f"  {f.relative_to(out_dir)}  ({f.stat().st_size} bytes)")
    published = run.state.get("published_path", "")
    if published:
        print(f"\nPublished workspace: {published}")


def _start(args: argparse.Namespace) -> int:
    if args.domain not in VALID_DOMAINS:
        print(f"[ERROR] Unknown domain {args.domain!r}; expected one of {VALID_DOMAINS}.")
        return 3
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"demo-{args.domain}-{uuid.uuid4().hex[:8]}"

    # A run id that already has a checkpoint would resume the *old* thread
    # instead of executing fresh; refuse rather than silently inherit state.
    if (root / run_id / "graph.sqlite").exists():
        print(f"[ERROR] run id {run_id!r} already has a checkpoint under {root}.")
        print("        Pick a new --run-id (the default generates one per run).")
        return 3

    print(f"Starting pipeline: course={args.course_code} domain={args.domain} "
          f"run_id={run_id}\n")
    run: GraphRun = run_pipeline(
        course_code=args.course_code,
        run_id=run_id,
        root=root,
        output_dir=root / run_id,
        domain=args.domain,
    )
    _print_run_banner(run, root)

    if run.status.value == "escalated":
        esc = run.state.get("escalation")
        print(f"\n[FAILED] Validation escalated: {getattr(esc, 'reason', '')}")
        print("Publication is blocked for an escalated run.")
        return 1

    _print_gate_request(run)

    if args.decision == "hold":
        print("\nHeld at the gate (no decision taken). To resume:")
        print(f"  python -m scripts.run_lab14_demo --resume {run.run_id} "
              f"--decision approve   # publish")
        print(f"  python -m scripts.run_lab14_demo --resume {run.run_id} "
              f"--decision refuse    # do not publish")
        return 0

    return _resume(run_id, args.decision, root)


def _resume(run_id: str, decision: str, root: Path) -> int:
    if decision not in VALID_DECISIONS:
        print(f"[ERROR] Unknown decision {decision!r}; expected one of {VALID_DECISIONS}.")
        return 3
    graph_decision = DECISION_APPROVE if decision == "approve" else DECISION_REFUSE
    print(f"\nResuming {run_id} with explicit decision: {decision!r}")
    try:
        run: GraphRun = resume_run(run_id=run_id, decision=graph_decision, root=root)
    except PermissionError as exc:
        print(f"\n[BLOCKED] Publication refused by governance: {exc}")
        print("The specialist (or validation) failed; approval cannot launder it.")
        return 2
    _print_run_banner(run, root)
    _print_artifacts(run, root)

    if decision == "refuse":
        if run.published:
            print("[ERROR] Refused run unexpectedly published.")
            return 1
        print("\nOK: refusal recorded; nothing was published.")
        return 0

    if run.published:
        print("\nOK: approved run published.")
        return 0
    print(f"\n[FAILED] Run did not publish: {run.summary()}")
    return 1


def _resume_existing(args: argparse.Namespace) -> int:
    root = Path(args.root)
    req = load_gate_request(args.resume, root=root)
    if req is None:
        print(f"[ERROR] No suspended run {args.resume!r} under {root}.")
        print("        Start it first (without --resume) or check --root.")
        return 3
    if args.decision == "hold":
        # Show the pending review without deciding, mirroring the start path.
        print(f"Run {args.resume} is suspended at the gate. Pending review:")
        print(f"  course:            {req.course_code} {req.course_name}")
        print(f"  domain:            {req.domain}")
        print(f"  specialist ok:     {req.specialist_ok}"
              + (f"  (domain error: {req.domain_error})" if req.domain_error else ""))
        print(f"  validation:        passed={req.validation.passed}")
        print(f"  draft grades:      {len(req.draft_grades)}")
        print(f"  summary:           {req.summary}")
        print("\nNo decision taken. Resume with --decision approve|refuse.")
        return 0
    return _resume(args.resume, args.decision, root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lab 14 Phase 4 end-to-end capstone demo (offline, local corpus)."
    )
    parser.add_argument("--course", dest="course_code", default="CS201",
                        help="Course code from the local seed corpus (default: CS201).")
    parser.add_argument("--domain", default="theory",
                        help=f"Domain specialist to route to: {', '.join(VALID_DOMAINS)}.")
    parser.add_argument("--run-id", default=None,
                        help="Explicit run id (default: demo-<domain>-<unique>).")
    parser.add_argument("--root", default=str(REPO / "out" / "demo" / "lab14"),
                        help="Checkpoint/artifact root (default: out/demo/lab14).")
    parser.add_argument("--decision", default="hold",
                        help="hold (default) | approve | refuse -- the faculty decision.")
    parser.add_argument("--resume", default=None, metavar="RUN_ID",
                        help="Resume an existing suspended run instead of starting one.")
    args = parser.parse_args()

    if args.resume:
        return _resume_existing(args)
    return _start(args)


if __name__ == "__main__":
    raise SystemExit(main())
