"""Doc 2.10 Lab 7: one command yields a validated organised course workspace.

Doc 2.10's acceptance criterion, quoted (line 690): *"One command yields a
validated organised course workspace (notes, assignments, grades) draft end to
end."* The deliverable is *"A graph runner that produces a full organised
course workspace (notes, assignments, grades) draft end to end."*

So this module is the command. It runs four scenarios in order, because the
interesting claims about a node graph are all comparative:

1. **Clean** -- the doc's happy path, reaching the gate with a validated draft.
2. **Retry** -- the back-edge actually re-enters ``workers`` and converges.
3. **Exhaustion** -- the bound stops the loop and escalates with a diagnosis.
4. **Refusal** -- a human says no, and nothing is published.

Then it asserts eighteen acceptance checks against what actually happened.

Everything printed here is produced by the run. No number in this module's
output is written by hand, so the transcript in ``docs/lab07.md`` cannot drift
from the code without the checks failing.

Run it with::

    python -m course_organizer.labs.lab07_node_graph
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from course_organizer.graph import (
    DECISION_APPROVE,
    DECISION_REFUSE,
    DOC_CHECKS,
    DOC_NODE_CHAIN,
    MAX_ATTEMPTS,
    CourseStatus,
    build_run_context,
    compiled_nodes,
    load_gate_request,
    resume_run,
    run_pipeline,
    topology_lines,
)
from course_organizer.graph.runner import GraphRun

LOGGER = logging.getLogger("lab07")

#: The doc's own words for the gate's refusal, reused as a run id so the
#: artefacts on disk say what they are.
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("clean", "the doc's happy path, stopping at the gate"),
    ("retry", "the back-edge re-enters workers and converges"),
    ("exhaustion", "the MAX_ATTEMPTS bound stops the loop and escalates"),
    ("refusal", "a human refuses; nothing is published"),
)


@dataclass
class LabResult:
    """Everything the four scenarios produced, plus the verdict.

    Attributes:
        root: Where this invocation's artefacts live.
        clean: The happy-path run, stopped at the gate.
        retry: The run whose first passes fail validation.
        exhaustion: The run that spent its whole budget.
        refused: The run a human said no to.
        checks: The acceptance checks, each with a name, a pass flag and the
            evidence string that decided it.
        pass_all: True only when every check passed.
    """

    root: Path
    clean: Optional[GraphRun] = None
    retry: Optional[GraphRun] = None
    exhaustion: Optional[GraphRun] = None
    refused: Optional[GraphRun] = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pass_all(self) -> bool:
        """Did every acceptance check pass?"""
        return bool(self.checks) and all(c["passed"] for c in self.checks)


def _check(
    name: str, passed: bool, detail: str
) -> dict[str, Any]:
    """One acceptance check.

    Args:
        name: What is being asserted, in the doc's vocabulary.
        passed: The verdict.
        detail: The evidence. Every detail here is computed from the run, so
            the transcript and the check can never disagree.
    """
    return {"name": name, "passed": bool(passed), "detail": detail}


def run_scenarios(root: Path) -> LabResult:
    """Run the four scenarios against a fresh root.

    Args:
        root: A directory this invocation owns. Each run gets its own
            subdirectory beneath it, so a scenario cannot see another's
            checkpoint.

    Returns:
        The populated :class:`LabResult`, checks included.
    """
    result = LabResult(root=root)

    # -- 1. clean --------------------------------------------------------
    result.clean = run_pipeline(
        course_code="CS201",
        run_id="clean",
        root=root,
        output_dir=root / "clean",
    )

    # -- 2. retry --------------------------------------------------------
    # fail_until_attempt=2 means the first two passes are defective and the
    # third is clean, so the back-edge has to fire twice and still converge
    # inside the bound.
    result.retry = run_pipeline(
        course_code="CS201",
        run_id="retry",
        root=root,
        output_dir=root / "retry",
        fail_until_attempt=2,
    )

    # -- 3. exhaustion ---------------------------------------------------
    result.exhaustion = run_pipeline(
        course_code="CS201",
        run_id="exhaustion",
        root=root,
        output_dir=root / "exhaustion",
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )

    # -- 4. refusal ------------------------------------------------------
    refused_run = run_pipeline(
        course_code="CS201",
        run_id="refusal",
        root=root,
        output_dir=root / "refusal",
    )
    result.refused = resume_run("refusal", DECISION_REFUSE, root=root)

    result.checks = acceptance_checks(result)
    return result


def acceptance_checks(result: LabResult) -> list[dict[str, Any]]:
    """The eighteen acceptance checks, each computed from a real run.

    Every one of these reads a run that already happened. There are no
    assertions here that could pass because the module was edited rather than
    because the graph behaved -- which is the difference between a checklist
    and a test.

    Returns:
        A list of check dicts, in the order the sections above ran.
    """
    clean = result.clean
    retry = result.retry
    exhausted = result.exhaustion
    refused = result.refused
    assert clean is not None and retry is not None
    assert exhausted is not None and refused is not None

    checks: list[dict[str, Any]] = []

    # -- topology, read off the compiled graph ---------------------------
    topology = "\n".join(topology_lines(clean.context))
    edge_pairs = {
        line.split("->")[1].strip()
        for line in topology.splitlines()
        if "->" in line
    }
    node_ids = set(compiled_nodes(clean.context))

    checks.append(
        _check(
            "every doc node is compiled into the graph",
            set(DOC_NODE_CHAIN) <= node_ids,
            f"doc chain {', '.join(DOC_NODE_CHAIN)} plus escalate",
        )
    )
    checks.append(
        _check(
            "the back-edge exists: validate --fail--> workers",
            "workers" in edge_pairs,
            "compiled edges include validate -> workers (conditional)",
        )
    )
    checks.append(
        _check(
            "workers --forward--> validate, without which the loop cannot cycle",
            "validate" in edge_pairs,
            "a conditional edge alone does not create a cycle",
        )
    )

    # -- the clean run ---------------------------------------------------
    ran = clean.nodes_ran()
    checks.append(
        _check(
            "the clean run executes the doc path and stops at the gate",
            ran == ["ingest", "plan", "workers", "validate", "assemble"],
            f"nodes executed: {' -> '.join(ran)}",
        )
    )
    checks.append(
        _check(
            "the clean run stops at a real human gate",
            clean.awaiting_gate,
            clean.summary(),
        )
    )
    checks.append(
        _check(
            "all four of doc 2.7's checks ran and passed",
            tuple(clean.state["validation"].checks_run) == DOC_CHECKS
            and clean.state["validation"].passed,
            f"{len(clean.state['validation'].checks_run)} check(s): "
            f"{', '.join(clean.state['validation'].checks_run)}",
        )
    )
    checks.append(
        _check(
            "nothing published before a human approved",
            not clean.published
            and clean.state.get("published_path") is None,
            f"status={clean.status.value}, no published file",
        )
    )
    checks.append(
        _check(
            "every grade offered to the reviewer is still a draft",
            {g.status for g in clean.state["draft_grades"]} == {"draft"}
            and all(not g.approved_by for g in clean.state["draft_grades"]),
            f"{len(clean.state['draft_grades'])} grade(s), all status=draft",
        )
    )
    checks.append(
        _check(
            "the assembled document has no drift against the locked plan",
            clean.state["drift_findings"] == [],
            f"drift findings: {len(clean.state['drift_findings'])}",
        )
    )
    request = clean.gate_request
    checks.append(
        _check(
            "the gate request carries grades, flags and the validation report",
            request is not None
            and bool(request.draft_grades)
            and bool(request.assignments_included)
            and request.validation.passed,
            (
                f"{len(request.draft_grades)} grade(s), "
                f"{len(request.similarity_flags)} similarity flag(s)"
                if request
                else "no gate request"
            ),
        )
    )
    checks.append(
        _check(
            "the plan was locked before any work began",
            ran.index("plan") < ran.index("workers")
            and bool(clean.state["plan_sha256"]),
            f"{clean.state['obligation_count']} obligation(s), "
            f"plan_sha256={clean.state['plan_sha256'][:12]}",
        )
    )

    # -- the back-edge ---------------------------------------------------
    passes = [e["pass"] for e in retry.state["attempt_log"]]
    checks.append(
        _check(
            "the back-edge retried and converged inside the bound",
            retry.state["validation"].passed
            and retry.attempts_made() == 3
            and retry.awaiting_gate,
            f"attempt log records passes {passes}; "
            f"converged on pass {retry.attempts_made()}",
        )
    )
    checks.append(
        _check(
            "each retry re-entered workers",
            retry.nodes_ran().count("workers") == len(passes)
            and retry.nodes_ran().count("validate") == len(passes),
            f"workers and validate each executed {len(passes)} time(s), once "
            f"per pass, for passes {passes}",
        )
    )

    # -- the bound -------------------------------------------------------
    esc = exhausted.state["escalation"]
    checks.append(
        _check(
            "the bound stops a loop that never converges",
            exhausted.status is CourseStatus.ESCALATED
            and exhausted.attempts_made() == MAX_ATTEMPTS
            and "escalate" in exhausted.nodes_ran(),
            f"stopped after {exhausted.attempts_made()} of "
            f"{MAX_ATTEMPTS} permitted attempt(s); routed to escalate",
        )
    )
    checks.append(
        _check(
            "exhaustion escalates with a diagnosis and the failing draft",
            bool(esc.diagnosis)
            and bool(esc.failing)
            and bool(esc.pending_draft.get("draft_grades")),
            f"raised_by={esc.raised_by}, to={esc.escalated_to}, "
            f"{len(esc.failing)} failing check(s), draft attached",
        )
    )

    # -- the refusal -----------------------------------------------------
    checks.append(
        _check(
            "a faculty refusal publishes nothing and escalates",
            refused.status is CourseStatus.REFUSED
            and not refused.published
            and refused.state["escalation"].reason == "refuse",
            refused.summary(),
        )
    )
    checks.append(
        _check(
            "a refused run leaves every grade unapproved",
            {g.status for g in refused.state["draft_grades"]} == {"draft"}
            and all(not g.approved_by for g in refused.state["draft_grades"]),
            f"{len(refused.state['draft_grades'])} grade(s) retained as drafts",
        )
    )

    # -- resumption ------------------------------------------------------
    persisted = load_gate_request("clean", root=result.root)
    checks.append(
        _check(
            "a suspended run's gate request survives the process",
            persisted is not None and bool(persisted.draft_grades),
            (
                f"recovered {len(persisted.draft_grades)} grade(s) from the "
                f"SQLite checkpointer"
                if persisted
                else "nothing recovered"
            ),
        )
    )

    return checks


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


def _line(char: str = "-", width: int = 78) -> str:
    return char * width


def _print_gate(request: Any) -> None:
    """Render the gate request the way doc 2.11 describes the dashboard."""
    print(f"  course       : {request.course_code} {request.course_name}")
    print(f"  attempt      : {request.attempt}")
    print(f"  units        : {', '.join(request.units_covered)}")
    print(f"  assignments  : {', '.join(request.assignments_included)}")
    print(
        f"  validation   : "
        f"{'passed' if request.validation.passed else 'FAILED'} "
        f"({len(request.validation.checks_run)} check(s))"
    )
    print(f"  draft grades : {len(request.draft_grades)}")
    print()
    print(
        f"    {'submission':<14} {'student':<16} {'awarded':>8} "
        f"{'max':>6}  {'late':<5} {'sim':>6}  flag"
    )
    for grade in request.draft_grades:
        sim = (
            "n/a"
            if grade.similarity_score is None
            else f"{grade.similarity_score:.2f}"
        )
        mark = "*" if grade.similarity_flagged else ""
        print(
            f"    {grade.submission_id:<14} {grade.student_name:<16} "
            f"{grade.awarded_marks:>8.2f} {grade.max_marks:>6.1f}  "
            f"{'yes' if grade.late else 'no':<5} {sim:>6}  {mark}"
        )
    if request.similarity_flags:
        print()
        print("    flagged for faculty review (not adjudicated):")
        for flag in request.similarity_flags:
            print(
                f"      {flag['submission_id']} vs {flag['against']} "
                f"= {flag['score']} (threshold {flag['threshold']})"
            )


def print_transcript(result: LabResult) -> None:
    """Print the whole run.

    Everything here is read off the runs. The one exception is the topology
    block, which is rendered from the compiled graph -- also real output, just
    read from a different object.
    """
    print("=" * 78)
    print("Lab 7 -- Agentic Node Graph: the full pipeline")
    print("Doc 2.10 -- one command yields a validated organised course workspace")
    print("=" * 78)
    print(f"workspace   : {result.root}")
    print(f"max attempts: {MAX_ATTEMPTS}")
    print(f"gate        : course_faculty (approve | edit | override | refuse)")

    # -- topology --------------------------------------------------------
    clean = result.clean
    assert clean is not None
    print("\n-- Compiled topology --")
    print("  (read from the compiled graph, not from a declared constant)")
    for line in topology_lines(clean.context):
        print(f"  {line}")

    # -- scenario 1 ------------------------------------------------------
    print("\n-- 1. Clean run: the doc's happy path --")
    print(f"  summary     : {clean.summary()}")
    print(f"  nodes       : {' -> '.join(clean.nodes_ran())}")
    print()
    print("  audit trail:")
    for entry in clean.state["audit_trail"]:
        print(f"    {entry['node']:<10} {entry['summary']}")
    print()
    print("  awaiting the human gate:")
    request = clean.gate_request
    if request is not None:
        _print_gate(request)

    # -- scenario 2 ------------------------------------------------------
    retry = result.retry
    assert retry is not None
    print("\n-- 2. The back-edge: validate --fail--> workers --")
    print(f"  summary     : {retry.summary()}")
    print(f"  nodes       : {' -> '.join(retry.nodes_ran())}")
    print()
    print("  attempt log (the proof the edge fired):")
    for entry in retry.state["attempt_log"]:
        failed = [
            s["step_id"] for s in entry["specialists"] if not s["ok"]
        ]
        detail = (
            f"faulty: {', '.join(failed)}" if failed else "all specialists ok"
        )
        print(f"    pass {entry['pass']}: {detail}")
    print()
    for entry in retry.state["audit_trail"]:
        if entry["node"] == "validate":
            print(f"    validate   {entry['summary']}")

    # -- scenario 3 ------------------------------------------------------
    exhausted = result.exhaustion
    assert exhausted is not None
    esc = exhausted.state["escalation"]
    print("\n-- 3. The bound: on exhaustion, escalate with a diagnosis --")
    print(f"  summary     : {exhausted.summary()}")
    print(f"  nodes       : {' -> '.join(exhausted.nodes_ran())}")
    print(f"  reason      : {esc.reason}")
    print(f"  raised by   : {esc.raised_by} -> {esc.escalated_to}")
    print(f"  attempts    : {esc.attempts} of {esc.max_attempts}")
    print(f"  failing     : {', '.join(esc.failing)}")
    print(f"  diagnosis   : {esc.diagnosis}")
    print(f"  draft kept  : {len(esc.pending_draft.get('draft_grades', []))} grade(s)")

    # -- scenario 4 ------------------------------------------------------
    refused = result.refused
    assert refused is not None
    print("\n-- 4. Faculty refusal: a hard stop --")
    print(f"  summary     : {refused.summary()}")
    print(f"  nodes       : {' -> '.join(refused.nodes_ran())}")
    print(f"  status      : {refused.status.value}")
    print(f"  published   : {refused.published}")
    print(f"  escalation  : {refused.state['escalation'].diagnosis}")

    # -- acceptance ------------------------------------------------------
    print("\n" + _line("="))
    print("-- Acceptance checks --")
    for i, check in enumerate(result.checks, 1):
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {i:>2}. {check['name']}")
        print(f"         {check['detail']}")

    print("\n" + _line("="))
    passed = sum(1 for c in result.checks if c["passed"])
    verdict = "OVERALL: PASS" if result.pass_all else "OVERALL: FAIL"
    print(f"{verdict} ({passed}/{len(result.checks)} checks)")
    print("=" * 78)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point.

    Args:
        argv: Arguments. ``--keep`` preserves the workspace for inspection;
            otherwise it is deleted after the transcript, since it holds a
            checkpoint and a published document per scenario.

    Returns:
        0 when every acceptance check passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m course_organizer.labs.lab07_node_graph",
        description=(
            "Doc 2.10 Lab 7: run the node graph end to end and verify the "
            "acceptance criteria."
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the workspace directory instead of deleting it",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="also write the checks to lab07_checks.json in the workspace",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log each node execution at INFO",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    root = Path(tempfile.mkdtemp(prefix="lab07-"))
    try:
        result = run_scenarios(root)
        print_transcript(result)
        if args.json:
            path = root / "lab07_checks.json"
            path.write_text(
                json.dumps(result.checks, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"\nchecks written to {path}")
        return 0 if result.pass_all else 1
    finally:
        if args.keep:
            print(f"\nworkspace kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(_main())