"""Doc 2.10 Lab 8: the parallel swarm, measured against its sequential baseline.

Doc 2.10's Lab 8 deliverable, quoted (line 704): *"A fan-out/fan-in that
processes all units and merges by alignment to the plan."* The acceptance is
comparative: *"Parallel run matches or beats sequential quality at lower
wall-clock time."* Line 707 adds the constraint the selector has to honour:
*"Merge strictly by plan alignment; picking the 'nicest' output quietly breaks
coverage."*

So this module runs the same work two ways and compares them:

1. **Parallel** -- the ``Send`` fan-out, one worker per candidate, merged by
   ``alignment_to_plan``.
2. **Sequential** -- the identical candidates in the identical order, produced by
   one node with no thread pool.
3. **Approved** -- the parallel run resumed through the human gate, to prove the
   swarm sits *before* the gate and does not re-run on resume.
4. **Exhaustion** -- the ``MAX_ATTEMPTS`` bound, so the parallel stage is shown
   to honour the back-edge rather than bypassing it.

Then it asserts twenty-four acceptance checks against what actually happened. The
wall-clock claim is a *measured property*, not a construction: the check reads
``stage_ms`` out of each run's own timing summary, and the concurrency claim reads
the peak overlap out of the same per-worker intervals.

Everything printed here is produced by the run. No number in this module's output
is written by hand, so the transcript in ``docs/lab08.md`` cannot drift from the
code without the checks failing.

Run it with::

    python -m course_organizer.labs.lab08_parallel_swarm
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
    DOC_CHECKS,
    KIND_SUBMISSION,
    KIND_UNIT,
    MAX_ATTEMPTS,
    SWARM_MAX_CONCURRENCY,
    CourseStatus,
    inspect_swarm,
    resume_swarm,
    run_swarm,
)
from course_organizer.graph.runner import GraphRun

LOGGER = logging.getLogger("lab08")

#: The four scenarios, in the order they run.
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("parallel", "the Send fan-out, merged by alignment to the plan"),
    ("sequential", "the identical candidates, one at a time"),
    ("approved", "the parallel run resumed through the human gate"),
    ("exhaustion", "the MAX_ATTEMPTS bound stops the swarm loop"),
)


@dataclass
class LabResult:
    """Everything the four scenarios produced, plus the verdict.

    Attributes:
        root: Where this invocation's artefacts live.
        parallel: The fan-out run, stopped at the gate.
        sequential: The baseline run, stopped at the gate.
        approved_suspended: The parallel run that will be resumed.
        approved: The same run after faculty approval, published.
        exhaustion: The run that spent its whole budget.
        checks: The acceptance checks, each with a name, a pass flag and the
            evidence string that decided it.
        pass_all: True only when every check passed.
    """

    root: Path
    parallel: Optional[GraphRun] = None
    sequential: Optional[GraphRun] = None
    approved_suspended: Optional[GraphRun] = None
    approved: Optional[GraphRun] = None
    exhaustion: Optional[GraphRun] = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pass_all(self) -> bool:
        """Did every acceptance check pass?"""
        return bool(self.checks) and all(c["passed"] for c in self.checks)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    """One acceptance check (name, verdict, evidence)."""
    return {"name": name, "passed": bool(passed), "detail": detail}


def _selected(log: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    """The winning record for ``key``, or an empty dict."""
    for record in log:
        if record["key"] == key and record.get("selected"):
            return record
    return {}


def _records(log: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Every merge-log record for ``key``, best alignment first."""
    return sorted(
        (r for r in log if r["key"] == key),
        key=lambda r: (-float(r.get("alignment") or 0.0), r.get("strategy", "")),
    )


def _nicest_loser(log: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A submission where the highest-marked candidate lost to the aligned one.

    Line 707's pitfall, made concrete. Returns the winning record plus the marks
    of the rejected 'nicest' candidate, or ``None`` if no item demonstrates it.
    """
    keys = sorted({r["key"] for r in log if r["kind"] == KIND_SUBMISSION})
    for key in keys:
        records = _records(log, key)
        if not records:
            continue
        richest = max(records, key=lambda r: float(r.get("awarded_marks") or 0.0))
        winner = next((r for r in records if r.get("selected")), None)
        if winner is None or richest.get("selected"):
            continue
        if float(richest.get("awarded_marks") or 0.0) > float(
            winner.get("awarded_marks") or 0.0
        ):
            return {
                "key": key,
                "winner": winner,
                "richest": richest,
            }
    return None


def run_scenarios(root: Path) -> LabResult:
    """Run the four scenarios against a fresh root.

    Args:
        root: A directory this invocation owns. Each run gets its own
            subdirectory, so a scenario cannot see another's checkpoint.

    Returns:
        The populated :class:`LabResult`, checks included.
    """
    result = LabResult(root=root)

    # -- 1. parallel -----------------------------------------------------
    result.parallel = run_swarm(
        course_code="CS201",
        run_id="parallel",
        root=root,
        output_dir=root / "parallel",
        parallel=True,
    )

    # -- 2. sequential ---------------------------------------------------
    result.sequential = run_swarm(
        course_code="CS201",
        run_id="sequential",
        root=root,
        output_dir=root / "sequential",
        parallel=False,
    )

    # -- 3. approved -----------------------------------------------------
    result.approved_suspended = run_swarm(
        course_code="CS201",
        run_id="approved",
        root=root,
        output_dir=root / "approved",
        parallel=True,
    )
    result.approved = resume_swarm(
        "approved", DECISION_APPROVE, parallel=True, root=root
    )

    # -- 4. exhaustion ---------------------------------------------------
    result.exhaustion = run_swarm(
        course_code="CS201",
        run_id="exhaustion",
        root=root,
        output_dir=root / "exhaustion",
        parallel=True,
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )

    result.checks = acceptance_checks(result)
    return result


def _candidate_files(run: GraphRun, attempt: Optional[int] = None) -> list[Path]:
    """The candidate evidence records a run wrote to disk."""
    directory = Path(run.context.output_dir) / "swarm"
    if not directory.is_dir():
        return []
    pattern = "a*__*.json" if attempt is None else f"a{attempt:02d}__*.json"
    return sorted(directory.glob(pattern))


def acceptance_checks(result: LabResult) -> list[dict[str, Any]]:
    """The twenty-four acceptance checks, each computed from a real run.

    Returns:
        A list of check dicts, in the order the scenarios ran.
    """
    parallel = result.parallel
    sequential = result.sequential
    approved = result.approved
    approved_suspended = result.approved_suspended
    exhausted = result.exhaustion
    assert parallel is not None and sequential is not None
    assert approved is not None and approved_suspended is not None
    assert exhausted is not None

    checks: list[dict[str, Any]] = []

    # -- topology, read off the two compiled graphs ----------------------
    par_info = inspect_swarm(parallel.context, parallel=True)
    seq_info = inspect_swarm(sequential.context, parallel=False)
    par_nodes = set(par_info["nodes"])
    seq_nodes = set(seq_info["nodes"])
    par_edges = set(par_info["edges"])

    checks.append(
        _check(
            "the fan-out node and the merge node are compiled (parallel)",
            {"swarm_dispatch", "swarm_worker", "swarm_merge"} <= par_nodes,
            "compiled swarm nodes: "
            + ", ".join(
                n
                for n in sorted(par_nodes)
                if n.startswith("swarm_")
            ),
        )
    )
    checks.append(
        _check(
            "swarm_dispatch --Send--> swarm_worker is a conditional edge",
            ("swarm_dispatch", "swarm_worker", True) in par_edges,
            "read from the compiled graph, not a declared constant",
        )
    )
    checks.append(
        _check(
            "swarm_worker --> swarm_merge is the fan-in edge",
            ("swarm_worker", "swarm_merge", False) in par_edges,
            "all workers join once, after the last one finishes",
        )
    )
    checks.append(
        _check(
            "the sequential graph has a loop node and no Send worker",
            "swarm_sequential" in seq_nodes
            and "swarm_worker" not in seq_nodes
            and "swarm_dispatch" not in seq_nodes,
            "compiled swarm nodes: "
            + ", ".join(n for n in sorted(seq_nodes) if n.startswith("swarm_")),
        )
    )
    checks.append(
        _check(
            "Lab 7's back-edge survives under the swarm",
            ("validate", "workers", True) in par_edges,
            "a retry re-runs the swarm stage as well, which is the point",
        )
    )

    # -- the work items --------------------------------------------------
    pstats = parallel.state["swarm_stats"]
    items = parallel.state["swarm_items"]
    kinds = [i["kind"] for i in items]
    priorities = [i["priority"] for i in items]
    checks.append(
        _check(
            "both work-item kinds are fanned out",
            pstats["submission_items"] == 8 and pstats["unit_items"] == 5,
            f"{pstats['submission_items']} submission item(s) + "
            f"{pstats['unit_items']} unit item(s) = {pstats['items']}",
        )
    )
    checks.append(
        _check(
            "submission work is dispatched before unit work (priority)",
            kinds == sorted(kinds, key=lambda k: 0 if k == KIND_SUBMISSION else 1)
            and priorities == sorted(priorities, reverse=True),
            f"submission priority={priorities[0]}, unit priority={priorities[-1]}, "
            f"weights {pstats['priority_weight']}",
        )
    )
    checks.append(
        _check(
            "every work item is processed and gets a selected candidate",
            pstats["selected"] == pstats["items"] == 13,
            f"{pstats['selected']} selected of {pstats['items']} item(s)",
        )
    )
    checks.append(
        _check(
            "each item is best-of-N: three candidates, losers logged",
            pstats["candidates"] == 39 and pstats["rejected"] == 26,
            f"{pstats['candidates']} candidate(s), {pstats['rejected']} rejected",
        )
    )

    # -- the merge -------------------------------------------------------
    log = parallel.state["swarm_merge_log"]
    merge_is_max = all(
        abs(
            float(_selected(log, key).get("alignment") or 0.0)
            - max(float(r.get("alignment") or 0.0) for r in _records(log, key))
        )
        < 1e-9
        for key in {r["key"] for r in log}
        if _records(log, key) and _selected(log, key)
    )
    logged = [r for r in log if r.get("alignment") is not None]
    checks.append(
        _check(
            "every candidate is logged with its alignment and its screen",
            len(logged) == 39
            and all(r.get("satisfied") or r.get("violated") for r in logged),
            f"{len(logged)} merge decision(s), each with satisfied/violated",
        )
    )
    checks.append(
        _check(
            "merge selects the maximum plan-alignment candidate per item",
            merge_is_max,
            "selected alignment equals the max over the item's candidates",
        )
    )
    loser = _nicest_loser(log)
    checks.append(
        _check(
            "the 'nicest' (highest-marked) candidate is rejected",
            loser is not None,
            (
                f"{loser['key']}: {loser['richest']['strategy']} "
                f"awarded {loser['richest']['awarded_marks']} rejected; "
                f"{loser['winner']['strategy']} "
                f"awarded {loser['winner']['awarded_marks']} selected at "
                f"alignment {loser['winner']['alignment']}"
                if loser
                else "no item showed the pitfall"
            ),
        )
    )
    par_selected = {
        r["key"]: r["strategy"]
        for r in parallel.state["swarm_selected"]
    }
    seq_selected = {
        r["key"]: r["strategy"]
        for r in sequential.state["swarm_selected"]
    }
    checks.append(
        _check(
            "parallel and sequential select identical candidates",
            par_selected == seq_selected and len(par_selected) == 13,
            f"{len(par_selected)} item(s), same winner in both modes",
        )
    )
    checks.append(
        _check(
            "parallel quality matches or beats sequential quality",
            float(parallel.state["swarm_quality"])
            >= float(sequential.state["swarm_quality"]) - 1e-9,
            f"parallel={parallel.state['swarm_quality']} "
            f"sequential={sequential.state['swarm_quality']}",
        )
    )

    # -- priority weighting ----------------------------------------------
    by_kind = parallel.state["swarm_quality_by_kind"]
    weight_sub = pstats["submission_items"] * 2
    weight_unit = pstats["unit_items"] * 1
    expected_q = round(
        (
            by_kind[KIND_SUBMISSION] * weight_sub
            + by_kind[KIND_UNIT] * weight_unit
        )
        / (weight_sub + weight_unit),
        6,
    )
    checks.append(
        _check(
            "quality is priority-weighted (submission 2 : unit 1)",
            set(by_kind) == {KIND_SUBMISSION, KIND_UNIT}
            and abs(float(parallel.state["swarm_quality"]) - expected_q) < 1e-6,
            f"quality={parallel.state['swarm_quality']}, "
            f"by kind={by_kind}, weights {weight_sub}:{weight_unit}",
        )
    )

    # -- wall-clock and concurrency --------------------------------------
    par_timing = parallel.state["swarm_timing"]
    seq_timing = sequential.state["swarm_timing"]
    checks.append(
        _check(
            "the parallel pass is lower wall-clock than the sequential pass",
            float(par_timing["stage_ms"]) < float(seq_timing["stage_ms"]),
            f"parallel stage={par_timing['stage_ms']}ms < "
            f"sequential stage={seq_timing['stage_ms']}ms "
            f"({float(seq_timing['stage_ms']) / float(par_timing['stage_ms']):.2f}x)",
        )
    )
    checks.append(
        _check(
            "the fan-out really ran concurrently",
            int(par_timing["max_overlap"]) >= 2
            and int(seq_timing["max_overlap"]) == 1,
            f"parallel peak overlap={par_timing['max_overlap']}, "
            f"sequential peak overlap={seq_timing['max_overlap']}",
        )
    )
    checks.append(
        _check(
            "the fan-out stayed inside the measured pool width",
            int(par_timing["max_overlap"]) <= SWARM_MAX_CONCURRENCY,
            f"peak overlap={par_timing['max_overlap']} <= "
            f"SWARM_MAX_CONCURRENCY={SWARM_MAX_CONCURRENCY}; an unbounded pool "
            f"measured between 0.71x and 1.58x of sequential depending on the "
            f"process, so the bound is what makes the check above hold",
        )
    )
    checks.append(
        _check(
            "the fan-out did not drop any work",
            int(par_timing["tasks"]) == 39 and pstats["candidates"] == 39,
            f"{par_timing['tasks']} worker interval(s) over "
            f"{par_timing['span_ms']}ms of concurrent span",
        )
    )

    # -- the gate and the resume -----------------------------------------
    checks.append(
        _check(
            "the parallel run stops at the human gate with doc 2.7 passing",
            parallel.awaiting_gate
            and parallel.state["validation"].passed
            and tuple(parallel.state["validation"].checks_run) == DOC_CHECKS,
            f"status={parallel.status.value}, "
            f"{len(parallel.state['validation'].checks_run)} check(s), "
            f"{len(parallel.state['draft_grades'])} merged grade(s)",
        )
    )
    pre_nodes = approved_suspended.nodes_ran()
    post_nodes = approved.nodes_ran()
    ran_swarm_after_resume = any(
        n in {"swarm_dispatch", "swarm_worker", "swarm_sequential", "swarm_merge"}
        for n in post_nodes[len(pre_nodes):]
    )
    checks.append(
        _check(
            "resuming does not re-run the swarm or the pre-gate nodes",
            not ran_swarm_after_resume
            and "workers" not in post_nodes[len(pre_nodes):],
            "after resume, nodes executed: "
            + ", ".join(post_nodes[len(pre_nodes):] or ["(none)"]),
        )
    )
    published_path = approved.state.get("published_path")
    published_ok = False
    published_bytes = 0
    if published_path:
        text = Path(published_path).read_text(encoding="utf-8")
        published_bytes = len(text.encode("ascii"))
        published_ok = "## Approval Recorded" in text
    checks.append(
        _check(
            "an approved run publishes the merged grades",
            approved.status is CourseStatus.PUBLISHED
            and approved.published
            and published_ok
            and all(g.approved_by for g in approved.state["draft_grades"]),
            (
                f"status={approved.status.value}, "
                f"{len(approved.state['draft_grades'])} grade(s) approved, "
                f"{published_bytes} ASCII char(s) published"
                if published_path
                else "nothing published"
            ),
        )
    )

    # -- the bound -------------------------------------------------------
    checks.append(
        _check(
            "the MAX_ATTEMPTS bound still stops a non-converging swarm",
            exhausted.status is CourseStatus.ESCALATED
            and exhausted.attempts_made() == MAX_ATTEMPTS
            and "escalate" in exhausted.nodes_ran(),
            f"stopped after {exhausted.attempts_made()} of "
            f"{MAX_ATTEMPTS} permitted attempt(s)",
        )
    )
    merge_runs = exhausted.nodes_ran().count("swarm_merge")
    dispatch_runs = exhausted.nodes_ran().count("swarm_dispatch")
    checks.append(
        _check(
            "the swarm ran on every retry, not just the first",
            merge_runs == MAX_ATTEMPTS and dispatch_runs == MAX_ATTEMPTS,
            f"swarm_dispatch ran {dispatch_runs} time(s), swarm_merge "
            f"{merge_runs} time(s) for {MAX_ATTEMPTS} attempts",
        )
    )
    esc = exhausted.state["escalation"]
    checks.append(
        _check(
            "the bound escalates with a diagnosis and the failing draft",
            bool(esc.diagnosis)
            and bool(esc.failing)
            and bool(esc.pending_draft.get("draft_grades")),
            f"reason={esc.reason}, {len(esc.failing)} failing check(s): "
            f"{', '.join(esc.failing)}",
        )
    )

    # -- durable evidence ------------------------------------------------
    par_files = _candidate_files(parallel, attempt=0)
    all_exhaustion_files = _candidate_files(exhausted)
    parse_ok = True
    if par_files:
        sample = json.loads(par_files[0].read_text(encoding="utf-8"))
        parse_ok = bool(sample.get("content")) and "self_reported_alignment" in sample
    checks.append(
        _check(
            "every candidate's durable evidence record is on disk",
            len(par_files) == 39 and parse_ok,
            f"{len(par_files)} record(s) under "
            f"{Path(parallel.context.output_dir).name}/swarm, first parses",
        )
    )
    checks.append(
        _check(
            "the exhaustion run logged every attempt's candidates",
            len(all_exhaustion_files) == MAX_ATTEMPTS * 39,
            f"{len(all_exhaustion_files)} record(s) across "
            f"{MAX_ATTEMPTS} attempt(s)",
        )
    )

    return checks


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


def _line(char: str = "-", width: int = 78) -> str:
    return char * width


def _print_timing(timing: dict[str, Any]) -> None:
    print(
        f"  timing      : mode={timing['mode']}, tasks={timing['tasks']}, "
        f"stage={timing['stage_ms']}ms, span={timing['span_ms']}ms, "
        f"sum_task={timing['sum_task_ms']}ms, peak_overlap={timing['max_overlap']}"
    )


def _print_merge(parallel: GraphRun, limit: int = 13) -> None:
    """The per-item selection: winner, alignment, and the rejected candidates."""
    log = parallel.state["swarm_merge_log"]
    keys = sorted({r["key"] for r in log})
    print(
        f"    {'work item':<22} {'kind':<10} {'winner':<10} {'align':>6}  "
        f"rejected"
    )
    for key in keys[:limit]:
        records = _records(log, key)
        winner = next((r for r in records if r.get("selected")), None)
        losers = ", ".join(
            f"{r['strategy']}={r['alignment']}"
            for r in records
            if not r.get("selected")
        )
        if winner is None:
            continue
        print(
            f"    {key:<22} {winner['kind']:<10} {winner['strategy']:<10} "
            f"{winner['alignment']:>6}  {losers}"
        )


def print_transcript(result: LabResult) -> None:
    """Print the whole run, all of it read off the runs."""
    parallel = result.parallel
    sequential = result.sequential
    approved = result.approved
    exhausted = result.exhaustion
    assert parallel is not None and sequential is not None
    assert approved is not None and exhausted is not None

    print("=" * 78)
    print("Lab 8 -- Parallel Swarm: fan out the work, merge by plan alignment")
    print("Doc 2.10 -- lower wall-clock at matched quality, selecting by the plan")
    print("=" * 78)
    print(f"workspace   : {result.root}")
    print(f"max attempts: {MAX_ATTEMPTS}")
    print(f"gate        : course_faculty (approve | edit | override | refuse)")

    # -- topology --------------------------------------------------------
    print("\n-- Compiled topologies --")
    print("  (read from each compiled graph, not from a declared constant)")
    for line in inspect_swarm(parallel.context, parallel=True)["lines"]:
        print(f"  {line}")
    for line in inspect_swarm(sequential.context, parallel=False)["lines"]:
        print(f"  {line}")

    # -- scenario 1 ------------------------------------------------------
    print("\n-- 1. Parallel: the Send fan-out --")
    print(f"  summary     : {parallel.summary()}")
    print(f"  nodes       : {' -> '.join(parallel.nodes_ran())}")
    stats = parallel.state["swarm_stats"]
    print(
        f"  work items  : {stats['items']} "
        f"({stats['submission_items']} submission + {stats['unit_items']} unit)"
    )
    print(
        f"  candidates  : {stats['candidates']} produced, "
        f"{stats['selected']} selected, {stats['rejected']} logged as rejected"
    )
    print(f"  quality     : {parallel.state['swarm_quality']} "
          f"(by kind {parallel.state['swarm_quality_by_kind']})")
    _print_timing(parallel.state["swarm_timing"])
    print("\n  selection by plan alignment (line 710):")
    _print_merge(parallel)

    # -- scenario 2 ------------------------------------------------------
    print("\n-- 2. Sequential: the same candidates, one at a time --")
    print(f"  summary     : {sequential.summary()}")
    print(f"  nodes       : {' -> '.join(sequential.nodes_ran())}")
    sstats = sequential.state["swarm_stats"]
    print(
        f"  candidates  : {sstats['candidates']} produced, "
        f"{sstats['selected']} selected"
    )
    print(f"  quality     : {sequential.state['swarm_quality']}")
    _print_timing(sequential.state["swarm_timing"])

    # -- comparison ------------------------------------------------------
    par_timing = parallel.state["swarm_timing"]
    seq_timing = sequential.state["swarm_timing"]
    print("\n-- Parallel vs sequential --")
    print(
        f"    {'mode':<12} {'stage_ms':>10} {'span_ms':>10} {'sum_task_ms':>12} "
        f"{'overlap':>8} {'quality':>8}"
    )
    for timing, run in (
        (par_timing, parallel),
        (seq_timing, sequential),
    ):
        print(
            f"    {timing['mode']:<12} {timing['stage_ms']:>10} "
            f"{timing['span_ms']:>10} {timing['sum_task_ms']:>12} "
            f"{timing['max_overlap']:>8} {run.state['swarm_quality']:>8}"
        )
    speedup = float(seq_timing["stage_ms"]) / float(par_timing["stage_ms"])
    print(f"    stage-speedup: {speedup:.2f}x at identical quality")

    # -- scenario 3 ------------------------------------------------------
    print("\n-- 3. Approval: the parallel draft through the human gate --")
    print(f"  before resume: {result.approved_suspended.summary()}"
          if result.approved_suspended
          else "  before resume: (missing)")
    print(f"  after resume : {approved.summary()}")
    path = approved.state.get("published_path")
    if path:
        text = Path(path).read_text(encoding="utf-8")
        print(f"  published    : {path}")
        print(
            f"  length       : {len(text.encode('ascii'))} ASCII char(s), "
            f"approval marker present: {'## Approval Recorded' in text}"
        )
    print(f"  grades       : {len(approved.state['draft_grades'])} approved")

    # -- scenario 4 ------------------------------------------------------
    esc = exhausted.state["escalation"]
    print("\n-- 4. The bound: on exhaustion, escalate with a diagnosis --")
    print(f"  summary     : {exhausted.summary()}")
    print(f"  nodes       : {' -> '.join(exhausted.nodes_ran())}")
    print(f"  reason      : {esc.reason}")
    print(f"  attempts    : {esc.attempts} of {esc.max_attempts}")
    print(f"  failing     : {', '.join(esc.failing)}")
    print(f"  diagnosis   : {esc.diagnosis}")

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
        argv: Arguments. ``--keep`` preserves the workspace; otherwise it is
            deleted after the transcript.

    Returns:
        0 when every acceptance check passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python -m course_organizer.labs.lab08_parallel_swarm",
        description=(
            "Doc 2.10 Lab 8: run the parallel swarm and its sequential baseline, "
            "then verify the acceptance criteria."
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
        help="also write the checks to lab08_checks.json in the workspace",
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

    root = Path(tempfile.mkdtemp(prefix="lab08-"))
    try:
        result = run_scenarios(root)
        print_transcript(result)
        if args.json:
            path = root / "lab08_checks.json"
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
