"""Lab 8 -- the parallel swarm: fan out per work item, merge by plan alignment.

Doc 2.10's Lab 8 (lines 699-711) is one paragraph of pseudocode::

    results = parallel_map(process_assignment, plan.units)
    output  = merge(results, key=alignment_to(plan))

and three sentences of constraint:

* line 700 -- *"Parallelise the per-unit work and select the best."*
* line 701 -- *"Several worker agents process assignment submissions in
  parallel; a merge/selector picks the best per the plan."*
* line 704 -- deliverable *"A fan-out/fan-in that processes all units and merges
  by alignment to the plan."*, accepted when *"Parallel run matches or beats
  sequential quality at lower wall-clock time."*
* line 707 -- *"Merge strictly by plan alignment; picking the 'nicest' output
  quietly breaks coverage."*

This module is that fan-out and that merge. It is deliberately **additive**:
Lab 7's graph, nodes, runner and tests are not touched. Instead the swarm is a
second topology that reuses Lab 7's nodes and inserts one stage between
``workers`` and ``validate``::

    workers -> swarm_dispatch --Send(swarm_worker)--> (fan-in) swarm_merge -> validate

The work item is not only ``plan.units``. Line 701 names *assignment
submissions*, line 704 says *all units*, and the worklet's chapter 2.7 (line 570)
calls the locked unit of work *"one assignment submission"*. So the swarm fans
out over **both** kinds of work item, with submission work the higher priority:

============  =====  ==========================================================
kind          count  candidate strategies
============  =====  ==========================================================
``submission``  8    ``strict`` / ``generous`` / ``sparse``
``unit``        5    ``complete`` / ``outline`` / ``overclaimed``
============  =====  ==========================================================

Thirteen work items times three strategies is thirty-nine candidates per pass.
Every item gets a selected candidate, so *all* work is processed; every
candidate is written to disk, so *the rest are logged* (line 573).

Three design points that are the actual lesson:

**The merge recomputes alignment from the plan.** A candidate carries a
``self_reported_alignment`` for the log, but :func:`merge_results` never reads
it. It calls :func:`alignment_to_plan` again. A selector that trusted the
candidate's own score would be selecting on the thing it is supposed to verify.

**The fan-out is a real :class:`~langgraph.types.Send` fan-out.** The conditional
edge returns one ``Send`` per ``(work item, strategy)`` pair; LangGraph runs them
on its thread pool and fans them back in at ``swarm_merge``. The per-candidate
work is genuine I/O -- each worker writes its candidate's evidence record -- which
is why the parallel pass beats the sequential pass at wall-clock time despite the
GIL. That is measured, not asserted by construction.

**The reducer is load-bearing.** ``swarm_candidates`` is an ``append`` channel.
With LangGraph's default ``LastValue`` reducer the thirty-nine concurrent writes
would clobber each other and the merge would see one candidate. Candidates carry
their ``attempt`` so a retried pass does not merge two attempts' candidates
together.

Run the demonstration with::

    python -m course_organizer.labs.lab08_parallel_swarm
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Mapping, Optional, Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from course_organizer.graph.builder import (
    GATE_ROUTES,
    VALIDATE_ROUTES,
    route_after_gate,
    route_after_validate,
)
from course_organizer.graph.context import (
    RunContext,
    build_run_context,
    repo_root,
)
from course_organizer.graph.nodes import build_nodes
from course_organizer.graph.runner import (
    GraphRun,
    checkpoint_path,
    open_checkpointer,
)
from course_organizer.graph.state import (
    MAX_ATTEMPTS,
    CourseState,
    DraftGradeRow,
    append,
)
from course_organizer.graph.validation import RUBRIC_TOLERANCE

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The vocabulary of the swarm, quoted from the doc above.
# ---------------------------------------------------------------------------

KIND_SUBMISSION = "submission"
KIND_UNIT = "unit"

#: Submission work is the priority kind. ``plan.units`` (line 710) is the generic
#: spelling of the work list, but line 701 names submissions first and the
#: chapter's own work item (line 570) is *"one assignment submission"*. The
#: priority is a plain weight so the merge's quality number says which kind it is
#: measuring.
PRIORITY_SUBMISSION = 2
PRIORITY_UNIT = 1

#: The three deterministic calibrations of one submission draft. Deterministic
#: because a scripted run must be reproducible; three because best-of-N needs a
#: loser to log.
SUBMISSION_STRATEGIES: tuple[str, ...] = ("strict", "generous", "sparse")

#: The three calibrations of one unit's coverage record.
UNIT_STRATEGIES: tuple[str, ...] = ("complete", "outline", "overclaimed")

#: The obligations a submission candidate is screened against. Each is a
#: sentence from the chapter restated as something checkable.
SUBMISSION_OBLIGATIONS: tuple[str, ...] = (
    # doc 2.7 line 558 "a draft grade never exceeds the rubric maximum", and the
    # definition of done at doc 2.2 line 496, which restates it as the worked
    # example of what must pass before faculty review.
    "within_rubric_maximum",
    # doc 2.7 line 558 "a plagiarism/similarity score is attached"; surfaced per
    # student on the dashboard, doc 2.9 line 594.
    "similarity_attached",
    # doc 2.7 line 558 "late submissions are flagged accurately".
    "late_accurate",
    # doc 2.11 line 801 "rubric-aligned grading drafts" -- every rubric
    # criterion of the assignment carries a mark.
    "criteria_complete",
    # doc 2.10 line 704, "merges by alignment to the plan".
    "assignment_in_plan",
)

#: The obligations a unit coverage candidate is screened against. Line 704 says
#: the fan-out "processes **all units**", so "did we cover this unit" is a screen
#: and not a nicety; doc 2.7 line 570 is the row that says what a unit is.
UNIT_OBLIGATIONS: tuple[str, ...] = (
    "notes_covered",
    "assignments_in_unit",
    "submissions_in_unit",
    # doc 2.11 line 823: ungrounded output is rejected.
    "no_overclaim",
    "unit_in_plan",
)

#: How much the ``generous`` calibration inflates a draft. Ten percent is enough
#: to push a well-marked submission over the rubric maximum, which is the point:
#: the "nicest" candidate is the one line 707 warns about.
GENEROUS_FACTOR = 1.10

#: The assignment id the ``overclaimed`` unit candidate invents when the unit has
#: no foreign assignment to borrow. Named so a reader can see it is a sentinel.
GHOST_ASSIGNMENT = "GHOST-999"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class SwarmState(CourseState, total=False):
    """Lab 7's state plus the swarm's channels.

    Subclassing rather than editing :class:`CourseState` is deliberate: the swarm
    is a second topology, and a key only Lab 8 writes has no business in the
    contract Lab 7's tests pin. Inheritance is supported -- verified against
    langgraph 1.2.12 -- and the reducers on the inherited keys come with it.
    """

    #: ``parallel`` or ``sequential``. Recorded so the acceptance transcript can
    #: name the mode it measured.
    swarm_mode: str
    #: The work items, each with the ``base`` record its candidates calibrate.
    swarm_items: list[dict[str, Any]]
    #: **Append, not LastValue.** Thirty-nine workers write this channel
    #: concurrently; the reducer is what stops them clobbering each other.
    swarm_candidates: Annotated[list[dict[str, Any]], append]
    #: One ``(start, end)`` per worker, used to prove real overlap in the trace.
    swarm_intervals: Annotated[list[dict[str, Any]], append]
    #: The per-item winning candidate.
    swarm_selected: list[dict[str, Any]]
    #: Every candidate with its recomputed alignment and whether it won.
    swarm_merge_log: list[dict[str, Any]]
    #: Priority-weighted mean alignment of the selected candidates, in [0, 1].
    swarm_quality: float
    #: The same mean per work-item kind, so the priority is visible.
    swarm_quality_by_kind: dict[str, float]
    #: Counts, for the transcript.
    swarm_stats: dict[str, Any]
    #: ``perf_counter`` at the start of the fan-out, so the merge can time it.
    swarm_t0: float
    #: Measured stage time (ms). Set directly by the sequential node; derived
    #: from ``swarm_t0`` by the merge for the parallel path.
    swarm_stage_ms: float
    #: The timing summary the tests and transcript read.
    swarm_timing: dict[str, Any]
    #: The selected unit coverage records.
    unit_coverage: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Pure layer: work items, candidates, alignment, merge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItem:
    """One unit of parallel work.

    Attributes:
        kind: :data:`KIND_SUBMISSION` or :data:`KIND_UNIT`.
        item_id: The submission id or the unit label.
        assignment_id: For a submission item, its assignment. Empty for a unit.
        unit: The syllabus unit, when known.
        priority: Higher runs first and weighs more in the quality metric.
    """

    kind: str
    item_id: str
    assignment_id: str = ""
    unit: str = ""
    priority: int = PRIORITY_SUBMISSION

    @property
    def key(self) -> str:
        """A filename-safe, kind-qualified identity. ``submission:DS-A1-S001``."""
        return f"{self.kind}:{self.item_id}"

    def to_dict(self) -> dict[str, Any]:
        """The plain-dict form that travels in the checkpointed state."""
        return {
            "kind": self.kind,
            "item_id": self.item_id,
            "assignment_id": self.assignment_id,
            "unit": self.unit,
            "priority": self.priority,
            "key": self.key,
        }


@dataclass(frozen=True)
class Alignment:
    """One candidate's alignment to the locked plan.

    Attributes:
        score: ``(satisfied - violated) / total`` clamped to ``[0, 1]``. A single
            number so ``max(results, key=...)`` has an answer; the two tuples are
            what make it explainable rather than opaque.
        satisfied: Obligations the candidate meets.
        violated: Obligations it breaks.
    """

    score: float
    satisfied: tuple[str, ...]
    violated: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-clean record for the merge log."""
        return {
            "score": self.score,
            "satisfied": list(self.satisfied),
            "violated": list(self.violated),
        }


def plan_scope(plan_locked: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """The units and assignment ids the locked plan is over.

    The merge's key is *alignment to the plan* (line 710), so the plan has to be
    reduced to the two lists a candidate can be checked against. Reading them
    from the locked plan rather than from the store is what makes the key about
    the plan: a submission whose assignment the plan never covered scores lower
    even though the store would happily describe it.
    """
    plan_locked = plan_locked or {}
    return {
        "units": tuple(str(u) for u in plan_locked.get("units", ()) or ()),
        "assignment_ids": tuple(
            str(a) for a in plan_locked.get("assignment_ids", ()) or ()
        ),
    }


def _unit_sort(unit: str) -> tuple[int, str]:
    """Order units numerically when they look numeric, lexically otherwise."""
    text = str(unit)
    return (int(text), "") if text.isdigit() else (1 << 30, text)


def build_work_items(state: Mapping[str, Any]) -> list[WorkItem]:
    """Every piece of work, submissions first.

    The order is the priority made concrete: the fan-out sends submission work
    before unit work, and the sequential baseline processes it in the same order,
    so the two modes are comparable. Ordering the work is also the only honest
    way to express priority when every item is processed anyway -- a priority
    that changed *which* items ran would contradict line 704's *all units*.

    Args:
        state: The shared state, after ``workers`` has populated
            ``submissions``, ``assignments`` and ``units_covered``.

    Returns:
        The work items, submission kind before unit kind.
    """
    assignments = {
        str(a["assignment_id"]): a for a in state.get("assignments", []) or []
    }
    items: list[WorkItem] = []
    for row in sorted(
        state.get("submissions", []) or [],
        key=lambda r: str(r["submission_id"]),
    ):
        assignment_id = str(row["assignment_id"])
        items.append(
            WorkItem(
                kind=KIND_SUBMISSION,
                item_id=str(row["submission_id"]),
                assignment_id=assignment_id,
                unit=str(assignments.get(assignment_id, {}).get("unit", "")),
                priority=PRIORITY_SUBMISSION,
            )
        )
    units = sorted(
        (str(u) for u in state.get("units_covered", []) or []),
        key=_unit_sort,
    )
    for unit in units:
        items.append(
            WorkItem(
                kind=KIND_UNIT,
                item_id=unit,
                unit=unit,
                priority=PRIORITY_UNIT,
            )
        )
    return items


def _foreign_assignment(
    unit: str, assignments: Mapping[str, Mapping[str, Any]]
) -> str:
    """An assignment id that is *not* in ``unit``, for the overclaim candidate.

    Borrowed from a real other unit when one exists, so the overclaim looks like
    a plausible mistake rather than an obvious sentinel.
    """
    owned = {
        aid for aid, a in assignments.items() if str(a.get("unit", "")) == unit
    }
    for aid, a in sorted(assignments.items()):
        if aid not in owned:
            return aid
    return GHOST_ASSIGNMENT


def build_base(state: Mapping[str, Any], item: WorkItem) -> dict[str, Any]:
    """The baseline record a candidate is a calibration of.

    For a submission the base is the draft ``workers`` already produced. Using
    the upstream draft rather than re-deriving marks means the swarm is a
    *selector over drafts*, which is what line 573 describes, and it means the
    ``fail_until_attempt`` injection in the grading specialist flows through the
    swarm instead of being masked by it. For a unit the base is read from the
    store's own per-unit facts.

    Args:
        state: The shared state.
        item: The work item to build a base for.

    Returns:
        A JSON-clean dict. ``expected_*`` keys are *not* candidate content; they
        are the ground truth the alignment function checks the candidate against,
        and are stripped before the candidate is logged or selected.
    """
    if item.kind == KIND_SUBMISSION:
        base_row = None
        for row in state.get("draft_grades", []) or []:
            if str(row.submission_id) == item.item_id:
                base_row = row
                break
        if base_row is None:
            raise KeyError(
                f"no draft grade for {item.item_id}; the swarm selects over the "
                f"drafts 'workers' produced, so it must run after 'workers'"
            )
        assignment = next(
            (
                a
                for a in state.get("assignments", []) or []
                if str(a["assignment_id"]) == str(base_row.assignment_id)
            ),
            {},
        )
        late_by = {
            str(f["submission_id"]): int(f["late_by_seconds"])
            for f in state.get("late_flags", []) or []
        }
        row = base_row.model_dump()
        return {
            "submission_id": row["submission_id"],
            "student_name": row.get("student_name", ""),
            "assignment_id": row.get("assignment_id", ""),
            "awarded_marks": float(row.get("awarded_marks", 0.0)),
            "max_marks": float(row.get("max_marks", 0.0)),
            "criterion_marks": dict(row.get("criterion_marks", {}) or {}),
            "feedback": row.get("feedback", ""),
            "similarity_score": row.get("similarity_score"),
            "similarity_against": row.get("similarity_against", ""),
            "similarity_flagged": bool(row.get("similarity_flagged", False)),
            "late": bool(row.get("late", False)),
            # Ground truth, not content:
            "criterion_ids": [
                str(c["criterion_id"]) for c in assignment.get("rubric", []) or []
            ],
            "expected_late": bool(late_by.get(item.item_id, 0) > 0),
        }

    # -- unit ------------------------------------------------------------
    assignments = {
        str(a["assignment_id"]): a for a in state.get("assignments", []) or []
    }
    submissions = list(state.get("submissions", []) or [])
    own_assignments = sorted(
        aid
        for aid, a in assignments.items()
        if str(a.get("unit", "")) == item.unit
    )
    own_submissions = sorted(
        str(s["submission_id"])
        for s in submissions
        if str(s["assignment_id"]) in set(own_assignments)
    )
    notes_by_unit = state.get("notes_by_unit", {}) or {}
    return {
        "unit": item.unit,
        "note_count": int(notes_by_unit.get(item.unit, 0) or 0),
        "assignment_ids": own_assignments,
        "submission_ids": own_submissions,
        # Ground truth, not content:
        "expected_note_count": int(notes_by_unit.get(item.unit, 0) or 0),
        "expected_assignment_ids": list(own_assignments),
        "expected_submission_ids": list(own_submissions),
        "foreign_assignment": _foreign_assignment(item.unit, assignments),
    }


def _scaled_criteria(criteria: Mapping[str, float], factor: float) -> dict[str, float]:
    """Scale every criterion by ``factor``, rounding to two places."""
    return {str(k): round(float(v) * factor, 2) for k, v in criteria.items()}


def make_candidate(
    item: WorkItem, base: Mapping[str, Any], strategy: str
) -> dict[str, Any]:
    """Calibrate one candidate from a base record.

    The three strategies exist to give the merge something to reject. They are
    deterministic and cheap because a scripted run has to be reproducible, and
    because the lab is about the *selection*, not about generation.

    Args:
        item: The work item.
        base: :func:`build_base`'s output.
        strategy: One of :data:`SUBMISSION_STRATEGIES` or
            :data:`UNIT_STRATEGIES` for the item's kind.

    Returns:
        The candidate's *content*: the fields alignment checks and selection
        need, with the ``expected_*`` ground truth stripped.

    Raises:
        ValueError: If ``strategy`` is not one this kind understands. A silently
            unknown strategy would produce a candidate nobody chose.
    """
    if item.kind == KIND_SUBMISSION:
        allowed = SUBMISSION_STRATEGIES
        content: dict[str, Any] = {
            key: base.get(key)
            for key in (
                "submission_id",
                "student_name",
                "assignment_id",
                "awarded_marks",
                "max_marks",
                "criterion_marks",
                "feedback",
                "similarity_score",
                "similarity_against",
                "similarity_flagged",
                "late",
            )
        }
        content["criterion_marks"] = dict(content.get("criterion_marks") or {})
        content["criterion_ids"] = list(base.get("criterion_ids", []))
        content["expected_late"] = bool(base.get("expected_late", False))
        if strategy == "generous":
            awarded = round(float(content["awarded_marks"]) * GENEROUS_FACTOR, 2)
            if content["awarded_marks"]:
                factor = awarded / float(content["awarded_marks"])
                content["criterion_marks"] = _scaled_criteria(
                    content["criterion_marks"], factor
                )
            content["awarded_marks"] = awarded
        elif strategy == "sparse":
            # A sparse draft is one that skipped the similarity attachment and
            # one rubric criterion. Both are screens doc 2.7 requires.
            content["similarity_score"] = None
            content["similarity_against"] = ""
            marks = dict(content["criterion_marks"])
            if len(marks) > 1:
                drop = sorted(marks)[-1]
                marks.pop(drop)
            content["criterion_marks"] = marks
        elif strategy != "strict":
            raise ValueError(
                f"unknown submission strategy {strategy!r}; known: {allowed}"
            )
        return content

    if item.kind == KIND_UNIT:
        allowed = UNIT_STRATEGIES
        content = {
            "unit": base.get("unit"),
            "note_count": int(base.get("note_count", 0) or 0),
            "assignment_ids": list(base.get("assignment_ids", []) or []),
            "submission_ids": list(base.get("submission_ids", []) or []),
            "expected_note_count": int(base.get("expected_note_count", 0) or 0),
            "expected_assignment_ids": list(
                base.get("expected_assignment_ids", []) or []
            ),
            "expected_submission_ids": list(
                base.get("expected_submission_ids", []) or []
            ),
        }
        if strategy == "outline":
            # Notes covered, but the assignment/submission linkage omitted.
            content["assignment_ids"] = []
            content["submission_ids"] = []
        elif strategy == "overclaimed":
            extra = str(base.get("foreign_assignment") or GHOST_ASSIGNMENT)
            content["assignment_ids"] = sorted(
                set(content["assignment_ids"]) | {extra}
            )
        elif strategy != "complete":
            raise ValueError(
                f"unknown unit strategy {strategy!r}; known: {allowed}"
            )
        return content

    raise ValueError(f"unknown work-item kind {item.kind!r}")


def _score(satisfied: Sequence[str], violated: Sequence[str], total: int) -> float:
    """``(satisfied - violated) / total`` clamped to ``[0, 1]``.

    Counting *violations* as well as satisfactions matters: the ``overclaimed``
    unit candidate satisfies four obligations and breaks one, and a metric that
    only counted satisfactions would call it perfect.
    """
    if total <= 0:
        return 1.0
    raw = (len(satisfied) - len(violated)) / total
    return round(max(0.0, min(1.0, raw)), 4)


def align_submission(
    content: Mapping[str, Any], scope: Mapping[str, Sequence[str]]
) -> Alignment:
    """Screen one submission candidate against :data:`SUBMISSION_OBLIGATIONS`."""
    plan_assignments = set(scope.get("assignment_ids", ()))
    criteria_present = set(content.get("criterion_marks") or {})
    criteria_needed = set(content.get("criterion_ids") or ())
    checks: list[tuple[str, bool]] = [
        (
            "within_rubric_maximum",
            float(content.get("awarded_marks", 0.0))
            <= float(content.get("max_marks", 0.0)) + RUBRIC_TOLERANCE,
        ),
        ("similarity_attached", content.get("similarity_score") is not None),
        (
            "late_accurate",
            bool(content.get("late")) == bool(content.get("expected_late")),
        ),
        ("criteria_complete", criteria_present == criteria_needed),
        (
            "assignment_in_plan",
            (not plan_assignments)
            or str(content.get("assignment_id", "")) in plan_assignments,
        ),
    ]
    satisfied = tuple(name for name, ok in checks if ok)
    violated = tuple(name for name, ok in checks if not ok)
    return Alignment(
        score=_score(satisfied, violated, len(SUBMISSION_OBLIGATIONS)),
        satisfied=satisfied,
        violated=violated,
    )


def align_unit(
    content: Mapping[str, Any], scope: Mapping[str, Sequence[str]]
) -> Alignment:
    """Screen one unit coverage candidate against :data:`UNIT_OBLIGATIONS`."""
    plan_units = set(scope.get("units", ()))
    claimed_a = set(str(a) for a in content.get("assignment_ids", []) or [])
    claimed_s = set(str(s) for s in content.get("submission_ids", []) or [])
    expected_a = set(str(a) for a in content.get("expected_assignment_ids", []) or [])
    expected_s = set(
        str(s) for s in content.get("expected_submission_ids", []) or []
    )
    checks: list[tuple[str, bool]] = [
        (
            "notes_covered",
            int(content.get("note_count", 0) or 0)
            >= int(content.get("expected_note_count", 0) or 0),
        ),
        ("assignments_in_unit", claimed_a == expected_a),
        ("submissions_in_unit", claimed_s == expected_s),
        (
            "no_overclaim",
            claimed_a <= expected_a and claimed_s <= expected_s,
        ),
        (
            "unit_in_plan",
            (not plan_units) or str(content.get("unit", "")) in plan_units,
        ),
    ]
    satisfied = tuple(name for name, ok in checks if ok)
    violated = tuple(name for name, ok in checks if not ok)
    return Alignment(
        score=_score(satisfied, violated, len(UNIT_OBLIGATIONS)),
        satisfied=satisfied,
        violated=violated,
    )


def alignment_to_plan(
    item: WorkItem, content: Mapping[str, Any], scope: Mapping[str, Sequence[str]]
) -> Alignment:
    """The merge key, line 710's ``alignment_to(plan)``.

    Args:
        item: The work item the candidate is for.
        content: The candidate's content.
        scope: :func:`plan_scope`'s output.

    Returns:
        The :class:`Alignment` for the item's kind.
    """
    if item.kind == KIND_SUBMISSION:
        return align_submission(content, scope)
    if item.kind == KIND_UNIT:
        return align_unit(content, scope)
    raise ValueError(f"unknown work-item kind {item.kind!r}")


def _strategy_rank(item: WorkItem, strategy: str) -> int:
    """Position of ``strategy`` in its kind's declared order, for tie-breaks."""
    order = (
        SUBMISSION_STRATEGIES
        if item.kind == KIND_SUBMISSION
        else UNIT_STRATEGIES
    )
    try:
        return order.index(strategy)
    except ValueError:
        return len(order)


def merge_results(
    items: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Select the best-aligned candidate per work item and log all of them.

    Line 707's warning is implemented literally: the key is
    :func:`alignment_to_plan`, recomputed here from the candidate's content. The
    candidate's own ``self_reported_alignment`` is recorded and ignored, so a
    candidate cannot nominate itself.

    Ties are broken by the declared strategy order, then by strategy name, so the
    result is deterministic regardless of the order the thread pool finished in.
    The fan-in deliberately does **not** depend on completion order.

    Args:
        items: The work items, as dicts (from :meth:`WorkItem.to_dict`, plus
            ``base``).
        candidates: The candidate records the workers produced.
        scope: :func:`plan_scope`'s output.

    Returns:
        ``selected``, ``log``, ``quality``, ``quality_by_kind`` and ``stats``.
    """
    by_key: dict[str, WorkItem] = {}
    base_of: dict[str, Mapping[str, Any]] = {}
    for raw in items:
        item = WorkItem(
            kind=str(raw["kind"]),
            item_id=str(raw["item_id"]),
            assignment_id=str(raw.get("assignment_id", "")),
            unit=str(raw.get("unit", "")),
            priority=int(raw.get("priority", PRIORITY_UNIT)),
        )
        by_key[item.key] = item
        base_of[item.key] = raw.get("base", {})

    grouped: dict[str, list[Mapping[str, Any]]] = {key: [] for key in by_key}
    for cand in candidates:
        key = str(cand.get("key", ""))
        if key in grouped:
            grouped[key].append(cand)

    selected: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    weight_total = 0
    weight_score = 0.0
    per_kind: dict[str, list[float]] = {KIND_SUBMISSION: [], KIND_UNIT: []}

    for key in sorted(by_key):
        item = by_key[key]
        scored: list[tuple[float, int, str, Mapping[str, Any], Alignment]] = []
        for cand in grouped[key]:
            content = cand.get("content", {}) or {}
            alignment = alignment_to_plan(item, content, scope)
            scored.append(
                (
                    alignment.score,
                    _strategy_rank(item, str(cand.get("strategy", ""))),
                    str(cand.get("strategy", "")),
                    cand,
                    alignment,
                )
            )
        if not scored:
            # No candidate is logged as a decision with a null winner rather
            # than skipped, so "all units processed" is checkable.
            log.append(
                {
                    "key": key,
                    "kind": item.kind,
                    "item_id": item.item_id,
                    "priority": item.priority,
                    "strategy": "",
                    "alignment": None,
                    "selected": False,
                    "reason": "no candidate produced for this work item",
                }
            )
            continue
        # Highest score wins; among equals the earliest declared strategy wins.
        best = max(scored, key=lambda t: (t[0], -t[1]))
        for score, _rank, strategy, cand, alignment in scored:
            is_winner = cand is best[3]
            record: dict[str, Any] = {
                "key": key,
                "kind": item.kind,
                "item_id": item.item_id,
                "priority": item.priority,
                "strategy": strategy,
                "alignment": alignment.score,
                "satisfied": list(alignment.satisfied),
                "violated": list(alignment.violated),
                "self_reported_alignment": cand.get("self_reported_alignment"),
                "selected": is_winner,
            }
            if item.kind == KIND_SUBMISSION:
                cand_content = cand.get("content", {}) or {}
                record["awarded_marks"] = cand_content.get("awarded_marks")
                record["max_marks"] = cand_content.get("max_marks")
            log.append(record)
        winner_cand = best[3]
        winner_alignment = best[4]
        selected.append(
            {
                "key": key,
                "kind": item.kind,
                "item_id": item.item_id,
                "priority": item.priority,
                "strategy": best[2],
                "alignment": winner_alignment.score,
                "satisfied": list(winner_alignment.satisfied),
                "violated": list(winner_alignment.violated),
                "content": dict(winner_cand.get("content", {}) or {}),
                "base": dict(base_of.get(key, {}) or {}),
            }
        )
        weight_total += item.priority
        weight_score += item.priority * winner_alignment.score
        per_kind.setdefault(item.kind, []).append(winner_alignment.score)

    quality = round(weight_score / weight_total, 6) if weight_total else 0.0
    quality_by_kind = {
        kind: (round(sum(scores) / len(scores), 6) if scores else 0.0)
        for kind, scores in per_kind.items()
    }
    stats = {
        "items": len(by_key),
        "submission_items": sum(
            1 for i in by_key.values() if i.kind == KIND_SUBMISSION
        ),
        "unit_items": sum(1 for i in by_key.values() if i.kind == KIND_UNIT),
        "candidates": sum(len(v) for v in grouped.values()),
        "selected": len(selected),
        "rejected": sum(len(v) for v in grouped.values()) - len(selected),
        "priority_weight": weight_total,
    }
    return {
        "selected": selected,
        "log": log,
        "quality": quality,
        "quality_by_kind": quality_by_kind,
        "stats": stats,
    }


def timing_summary(
    intervals: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    stage_ms: float,
) -> dict[str, Any]:
    """Summarise a pass's fan-out timing, including peak concurrency.

    Peak overlap is computed by sweeping start/end events. ``>= 2`` is the
    structural proof that the fan-out really ran concurrently; ``stage_ms`` is
    the wall-clock number the acceptance compares. Both come from the same
    ``perf_counter`` interval list, so a fabricated speedup would have to fake
    the intervals too.
    """
    tasks = len(intervals)
    sum_ms = sum(
        (float(i["end"]) - float(i["start"])) * 1000.0 for i in intervals
    )
    events: list[tuple[float, int]] = []
    for i in intervals:
        events.append((float(i["start"]), 1))
        events.append((float(i["end"]), -1))
    events.sort()
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    span_ms = 0.0
    if intervals:
        span_ms = (
            max(float(i["end"]) for i in intervals)
            - min(float(i["start"]) for i in intervals)
        ) * 1000.0
    return {
        "mode": mode,
        "tasks": tasks,
        "stage_ms": round(float(stage_ms), 3),
        "span_ms": round(span_ms, 3),
        "sum_task_ms": round(sum_ms, 3),
        "max_overlap": peak,
    }


# ---------------------------------------------------------------------------
# The nodes
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(node: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"node": node, "at": _now(), "summary": summary, **extra}


def slug(text: str) -> str:
    """A filename-safe token from a work-item key or strategy name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_") or "item"


def _candidate_path(
    output_dir: Path, attempt: int, item_key: str, strategy: str
) -> Path:
    """``<output_dir>/swarm/a<NN>__<key>__<strategy>.json``."""
    return (
        Path(output_dir)
        / "swarm"
        / f"a{attempt:02d}__{slug(item_key)}__{slug(strategy)}.json"
    )


def write_candidate_artifact(
    output_dir: Path, attempt: int, candidate: Mapping[str, Any]
) -> str:
    """Write one candidate's evidence record to disk and return its path.

    This is the swarm's genuine per-candidate I/O, and it is not padding: line
    573 requires the rejected candidates to be *logged*, and line 497 asks for an
    evidence record per unit. Writing it from the worker, as the candidate is
    produced, means a fan-out that dies half way still leaves the candidates it
    had already produced. It is also *why* the parallel pass wins: file I/O
    releases the GIL, so the thread pool overlaps it.

    Args:
        output_dir: The run's output directory.
        attempt: The validation pass, so attempts do not overwrite each other.
        candidate: The candidate record, including its provenance.

    Returns:
        The path written, as a string, for the log.
    """
    path = _candidate_path(
        output_dir,
        attempt,
        str(candidate.get("key", "item")),
        str(candidate.get("strategy", "strategy")),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, indent=2, sort_keys=True) + "\n")
        handle.flush()
        # Durable, not just flushed: a fan-out that is killed midway must leave
        # the candidate records it had already produced. The fsync is *why* the
        # parallel pass wins -- it is real I/O that releases the GIL, so the
        # thread pool overlaps it while the sequential baseline pays it serially.
        os.fsync(handle.fileno())
    return str(path)


def make_dispatch(ctx: RunContext):
    """``swarm_dispatch`` -- build the work items and start the clock.

    The node itself returns a partial update; the *fan-out* is the conditional
    edge below, because a ``Send`` list is only legal there (verified: returning
    one from a node raises ``InvalidUpdateError``).
    """

    def swarm_dispatch(state: SwarmState) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for item in build_work_items(state):
            raw = item.to_dict()
            raw["base"] = build_base(state, item)
            items.append(raw)
        summary = (
            f"dispatched {len(items)} work item(s) "
            f"({sum(1 for i in items if i['kind'] == KIND_SUBMISSION)} "
            f"submission(s) first, "
            f"{sum(1 for i in items if i['kind'] == KIND_UNIT)} unit(s))"
        )
        LOGGER.info("swarm_dispatch %s", summary)
        return {
            "swarm_mode": "parallel",
            "swarm_items": items,
            "swarm_t0": time.perf_counter(),
            "audit_trail": [_audit("swarm_dispatch", summary, items=len(items))],
            "nodes_ran": ["swarm_dispatch"],
        }

    return swarm_dispatch


def _strategies_for(kind: str) -> tuple[str, ...]:
    """The declared strategy tuple for a kind, or a loud failure."""
    if kind == KIND_SUBMISSION:
        return SUBMISSION_STRATEGIES
    if kind == KIND_UNIT:
        return UNIT_STRATEGIES
    raise ValueError(f"unknown work-item kind {kind!r}")


def fan_out(state: SwarmState) -> list[Send]:
    """The conditional edge: one ``Send`` per ``(work item, strategy)``.

    Read by LangGraph after ``swarm_dispatch`` returns, so ``swarm_items`` is
    already populated. Each ``Send`` carries everything its worker needs -- the
    item, its base record, the strategy and the plan scope -- because a ``Send``
    target receives the payload, not the accumulated state.
    """
    attempt = int(state.get("attempts", 0) or 0)
    sends: list[Send] = []
    for raw in state.get("swarm_items", []) or []:
        for strategy in _strategies_for(str(raw["kind"])):
            sends.append(
                Send(
                    "swarm_worker",
                    {
                        "item": {
                            k: v for k, v in raw.items() if k != "base"
                        },
                        "base": raw.get("base", {}),
                        "strategy": strategy,
                        "attempt": attempt,
                        "plan_scope": {
                            "units": list(
                                state.get("plan_locked", {})
                                .get("units", [])
                                or []
                            ),
                            "assignment_ids": list(
                                state.get("plan_locked", {})
                                .get("assignment_ids", [])
                                or []
                            ),
                        },
                    },
                )
            )
    return sends


def make_swarm_worker(ctx: RunContext):
    """``swarm_worker`` -- one candidate, produced and logged.

    The worker is a pure function of its ``Send`` payload plus the run's output
    directory. It deliberately does not read the shared state: with a fan-out,
    the shared state is not what the node receives.
    """

    def swarm_worker(payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_item = payload["item"]
        item = WorkItem(
            kind=str(raw_item["kind"]),
            item_id=str(raw_item["item_id"]),
            assignment_id=str(raw_item.get("assignment_id", "")),
            unit=str(raw_item.get("unit", "")),
            priority=int(raw_item.get("priority", PRIORITY_UNIT)),
        )
        strategy = str(payload["strategy"])
        attempt = int(payload.get("attempt", 0) or 0)
        scope = payload.get("plan_scope", {}) or {}
        start = time.perf_counter()
        content = make_candidate(item, payload.get("base", {}) or {}, strategy)
        # Self-reported score, for the log only. `merge_results` recomputes.
        self_score = alignment_to_plan(item, content, scope).score
        candidate = {
            "key": item.key,
            "kind": item.kind,
            "item_id": item.item_id,
            "assignment_id": item.assignment_id,
            "unit": item.unit,
            "priority": item.priority,
            "strategy": strategy,
            "attempt": attempt,
            "content": content,
            "self_reported_alignment": self_score,
            "provenance": {
                "plan_scope": scope,
                "base": dict(payload.get("base", {}) or {}),
            },
        }
        candidate["artifact"] = write_candidate_artifact(
            ctx.output_dir, attempt, candidate
        )
        end = time.perf_counter()
        return {
            "swarm_candidates": [candidate],
            "swarm_intervals": [
                {
                    "key": item.key,
                    "strategy": strategy,
                    "attempt": attempt,
                    "start": start,
                    "end": end,
                }
            ],
        }

    return swarm_worker


def make_sequential(ctx: RunContext):
    """``swarm_sequential`` -- the same candidates, one at a time.

    This is the baseline the acceptance compares against. It runs the *same*
    :func:`make_candidate` for the *same* items in the *same* order and writes
    the *same* artefacts; the only difference is that there is no thread pool, so
    the per-candidate I/O does not overlap.
    """

    def swarm_sequential(state: SwarmState) -> dict[str, Any]:
        attempt = int(state.get("attempts", 0) or 0)
        scope = plan_scope(state.get("plan_locked"))
        items = build_work_items(state)
        candidates: list[dict[str, Any]] = []
        intervals: list[dict[str, Any]] = []
        item_dicts: list[dict[str, Any]] = []
        start_all = time.perf_counter()
        for item in items:
            base = build_base(state, item)
            item_dicts.append({**item.to_dict(), "base": base})
            for strategy in _strategies_for(item.kind):
                start = time.perf_counter()
                content = make_candidate(item, base, strategy)
                self_score = alignment_to_plan(item, content, scope).score
                candidate = {
                    "key": item.key,
                    "kind": item.kind,
                    "item_id": item.item_id,
                    "assignment_id": item.assignment_id,
                    "unit": item.unit,
                    "priority": item.priority,
                    "strategy": strategy,
                    "attempt": attempt,
                    "content": content,
                    "self_reported_alignment": self_score,
                    "provenance": {
                        "plan_scope": {
                            "units": list(scope["units"]),
                            "assignment_ids": list(scope["assignment_ids"]),
                        },
                        "base": dict(base),
                    },
                }
                candidate["artifact"] = write_candidate_artifact(
                    ctx.output_dir, attempt, candidate
                )
                end = time.perf_counter()
                candidates.append(candidate)
                intervals.append(
                    {
                        "key": item.key,
                        "strategy": strategy,
                        "attempt": attempt,
                        "start": start,
                        "end": end,
                    }
                )
        stage_ms = (time.perf_counter() - start_all) * 1000.0
        summary = (
            f"produced {len(candidates)} candidate(s) sequentially for "
            f"{len(items)} work item(s)"
        )
        LOGGER.info("swarm_sequential %s", summary)
        return {
            "swarm_mode": "sequential",
            "swarm_items": item_dicts,
            "swarm_candidates": candidates,
            "swarm_intervals": intervals,
            "swarm_stage_ms": stage_ms,
            "audit_trail": [
                _audit("swarm_sequential", summary, candidates=len(candidates))
            ],
            "nodes_ran": ["swarm_sequential"],
        }

    return swarm_sequential


def _row_from_content(content: Mapping[str, Any]) -> DraftGradeRow:
    """Rebuild a draft grade row from a selected submission candidate."""
    return DraftGradeRow(
        submission_id=str(content["submission_id"]),
        student_name=str(content.get("student_name", "")),
        assignment_id=str(content.get("assignment_id", "")),
        awarded_marks=float(content.get("awarded_marks", 0.0)),
        max_marks=float(content.get("max_marks", 0.0)),
        criterion_marks={
            str(k): float(v)
            for k, v in (content.get("criterion_marks") or {}).items()
        },
        feedback=str(content.get("feedback", "")),
        similarity_score=content.get("similarity_score"),
        similarity_against=str(content.get("similarity_against", "")),
        similarity_flagged=bool(content.get("similarity_flagged", False)),
        late=bool(content.get("late", False)),
        status="draft",
    )


def make_merge(ctx: RunContext):
    """``swarm_merge`` -- the fan-in and the selector.

    Runs once, after every ``swarm_worker`` for the pass has finished. It filters
    the accumulated candidates to the current attempt (a retried pass appends to
    the same channel), recomputes every alignment from the plan, selects one
    candidate per item and logs the rest.
    """

    def swarm_merge(state: SwarmState) -> dict[str, Any]:
        attempt = int(state.get("attempts", 0) or 0)
        scope = plan_scope(state.get("plan_locked"))
        all_candidates = [
            c
            for c in state.get("swarm_candidates", []) or []
            if int(c.get("attempt", 0) or 0) == attempt
        ]
        intervals = [
            i
            for i in state.get("swarm_intervals", []) or []
            if int(i.get("attempt", 0) or 0) == attempt
        ]
        items = state.get("swarm_items", []) or []
        merged = merge_results(items, all_candidates, scope)

        mode = str(state.get("swarm_mode", "parallel"))
        if mode == "parallel":
            t0 = float(state.get("swarm_t0") or 0.0)
            stage_ms = (time.perf_counter() - t0) * 1000.0 if t0 else 0.0
        else:
            stage_ms = float(state.get("swarm_stage_ms") or 0.0)
        timing = timing_summary(intervals, mode=mode, stage_ms=stage_ms)

        grades = [
            _row_from_content(sel["content"])
            for sel in merged["selected"]
            if sel["kind"] == KIND_SUBMISSION
        ]
        coverage = [
            {
                "unit": sel["content"].get("unit"),
                "note_count": sel["content"].get("note_count"),
                "assignment_ids": list(sel["content"].get("assignment_ids", [])),
                "submission_ids": list(sel["content"].get("submission_ids", [])),
                "strategy": sel["strategy"],
                "alignment": sel["alignment"],
            }
            for sel in merged["selected"]
            if sel["kind"] == KIND_UNIT
        ]
        stats = dict(merged["stats"])
        stats["mode"] = mode
        stats["attempt"] = attempt
        summary = (
            f"{mode}: selected {len(merged['selected'])} of "
            f"{merged['stats']['candidates']} candidate(s), rejected "
            f"{merged['stats']['rejected']}; quality {merged['quality']:.3f}; "
            f"stage {timing['stage_ms']:.1f}ms, peak overlap "
            f"{timing['max_overlap']}"
        )
        LOGGER.info("swarm_merge %s", summary)
        return {
            "swarm_selected": merged["selected"],
            "swarm_merge_log": merged["log"],
            "swarm_quality": merged["quality"],
            "swarm_quality_by_kind": merged["quality_by_kind"],
            "swarm_stats": stats,
            "swarm_timing": timing,
            "draft_grades": grades,
            "unit_coverage": coverage,
            "audit_trail": [
                _audit(
                    "swarm_merge",
                    summary,
                    quality=merged["quality"],
                    selected=len(merged["selected"]),
                    rejected=merged["stats"]["rejected"],
                    stage_ms=round(timing["stage_ms"], 3),
                    max_overlap=timing["max_overlap"],
                )
            ],
            "nodes_ran": ["swarm_merge"],
        }

    return swarm_merge


# ---------------------------------------------------------------------------
# The topology
# ---------------------------------------------------------------------------


def build_swarm_graph(
    ctx: RunContext, *, parallel: bool, checkpointer: Optional[Any] = None
):
    """Wire Lab 7's nodes plus the swarm stage, and compile.

    Args:
        ctx: Per-run dependencies, captured by the node closures.
        parallel: ``True`` for the Send fan-out, ``False`` for the sequential
            baseline. Both compile to graphs whose only difference is which
            swarm entry node is wired after ``workers``.
        checkpointer: Passed to ``compile()``, exactly as Lab 7 does.

    Returns:
        The compiled graph.
    """
    builder: StateGraph = StateGraph(SwarmState)
    for name, fn in build_nodes(ctx).items():
        builder.add_node(name, fn)
    builder.add_node("swarm_merge", make_merge(ctx))

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "workers")

    if parallel:
        builder.add_node("swarm_dispatch", make_dispatch(ctx))
        builder.add_node("swarm_worker", make_swarm_worker(ctx))
        builder.add_edge("workers", "swarm_dispatch")
        # The explicit path list is what makes the fan-out visible to
        # `get_graph()`; without it LangGraph renders the branch as
        # `swarm_dispatch -> __end__` (verified by probe).
        builder.add_conditional_edges(
            "swarm_dispatch", fan_out, ["swarm_worker"]
        )
        builder.add_edge("swarm_worker", "swarm_merge")
    else:
        builder.add_node("swarm_sequential", make_sequential(ctx))
        builder.add_edge("workers", "swarm_sequential")
        builder.add_edge("swarm_sequential", "swarm_merge")

    # The back-edge target is still `workers`, from Lab 7 unchanged. Because
    # `workers -> swarm stage`, a retry re-runs the swarm as well, which is what
    # makes the bound test meaningful for Lab 8.
    builder.add_edge("swarm_merge", "validate")
    builder.add_edge("assemble", "gate")
    builder.add_edge("publish", END)
    builder.add_edge("escalate", END)
    builder.add_conditional_edges("validate", route_after_validate, VALIDATE_ROUTES)
    builder.add_conditional_edges("gate", route_after_gate, GATE_ROUTES)

    compiled = builder.compile(checkpointer=checkpointer)
    if checkpointer is None:
        LOGGER.warning(
            "compiled the swarm graph without a checkpointer: the gate will "
            "suspend but the run cannot be resumed in another process"
        )
    return compiled


def swarm_edges(graph: Any) -> list[tuple[str, str, bool]]:
    """The compiled swarm graph's edges, as ``(source, target, conditional)``."""
    from course_organizer.graph.builder import graph_edges

    return graph_edges(graph)


def swarm_nodes(graph: Any) -> list[str]:
    """The compiled swarm graph's node ids, sorted (read off the graph)."""
    return sorted(str(node) for node in graph.get_graph().nodes)


def inspect_swarm(ctx: RunContext, *, parallel: bool) -> dict[str, Any]:
    """Compile a swarm graph for inspection only, with an in-memory saver.

    Args:
        ctx: Per-run dependencies.
        parallel: Which topology to build.

    Returns:
        ``{"nodes": [...], "edges": [...], "lines": [...]}``, all read off the
        compiled graph rather than off a declared constant.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    compiled = build_swarm_graph(ctx, parallel=parallel, checkpointer=InMemorySaver())
    lines = [f"swarm topology ({'parallel' if parallel else 'sequential'}):"]
    for source, target, conditional in swarm_edges(compiled):
        marker = "[cond]" if conditional else "[edge]"
        lines.append(f"  {marker} {source} -> {target}")
    return {
        "nodes": swarm_nodes(compiled),
        "edges": swarm_edges(compiled),
        "lines": lines,
    }


def swarm_topology_lines(ctx: RunContext, *, parallel: bool) -> list[str]:
    """The swarm topology as sorted, ASCII-only lines, for the transcript."""
    from langgraph.checkpoint.memory import InMemorySaver

    compiled = build_swarm_graph(ctx, parallel=parallel, checkpointer=InMemorySaver())
    lines = [f"swarm topology ({'parallel' if parallel else 'sequential'}):"]
    for source, target, conditional in swarm_edges(compiled):
        marker = "[cond]" if conditional else "[edge]"
        lines.append(f"  {marker} {source} -> {target}")
    return lines


#: Where the swarm's checkpoints live, beside Lab 7's rather than on top of them.
SWARM_OUTPUT_ROOT = "lab08"


def default_swarm_root() -> Path:
    """``<repo>/out/lab08`` -- one parent for every swarm run."""
    return repo_root() / "out" / SWARM_OUTPUT_ROOT


#: How wide the fan-out may be. Chosen by measurement, not by taste -- see
#: :class:`_BoundedFanOut` and ``docs/lab08.md`` section 9.
SWARM_MAX_CONCURRENCY: int = 8


class _BoundedFanOut:
    """A compiled graph whose invoke pins ``max_concurrency``.

    LangGraph runs a ``Send`` fan-out on
    ``ThreadPoolExecutor(max_workers=config["max_concurrency"])``. That was
    confirmed by reading the installed library rather than assumed: see
    ``langgraph.pregel._executor.BackgroundExecutor.__init__``, which delegates
    to ``langchain_core.runnables.config.get_executor_for_config``, which builds
    ``ContextThreadPoolExecutor(max_workers=config.get("max_concurrency"))``.

    Left unset, the pool is as wide as the fan-out -- 39 threads for this
    corpus -- and the stage gets *slower*, because each task's LangGraph
    bookkeeping is GIL-serialised while the only GIL-releasing work is one
    small durable write. The width below was measured, not guessed. Stage speedup
    against the sequential topology, median of three runs per width, in a plain
    process and inside a Jupyter kernel on the same machine::

        width   plain    jupyter
           2   1.04x     1.00x
           3   1.26x     1.25x
           4   1.22x     1.46x
           6   1.35x     1.65x
           8   1.38x     1.68x   <- chosen
          12   1.14x     1.52x
          39   1.13x     1.29x
        unbounded 1.27-1.58x     0.71x   <- not an acceptance criterion

    Two things fall out of that table. A narrow fan-out is not better than none,
    so the bound has to be wide enough to overlap real I/O. And an unbounded one
    is *unstable*: in a plain process it won, inside the Jupyter kernel it lost,
    reproducibly. Pinning the width is what makes doc 2.10 line 704's acceptance
    criterion hold everywhere rather than sometimes.

    A useful side effect is that the bound is checkable without a stopwatch: the
    peak overlap of the recorded intervals cannot exceed the pool width, so
    ``2 <= max_overlap <= SWARM_MAX_CONCURRENCY`` proves the fan-out both
    overlapped and stayed bounded.

    This is an adapter rather than an edit to ``GraphRun.start`` because Lab 7's
    runner builds its own config and Lab 8 does not touch it.
    """

    def __init__(self, graph: Any, width: int = SWARM_MAX_CONCURRENCY) -> None:
        self._graph = graph
        self._width = max(1, int(width))

    @property
    def width(self) -> int:
        """The pinned fan-out width."""
        return self._width

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """Forward to the wrapped graph with ``max_concurrency`` merged in."""
        if len(args) > 1:
            config = args[1]
            args = (
                args[0],
                {**(config or {}), "max_concurrency": self._width},
            ) + args[2:]
        else:
            config = kwargs.pop("config", None)
            kwargs["config"] = {
                **(config or {}),
                "max_concurrency": self._width,
            }
        return self._graph.invoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Everything else (``get_graph``, ``get_state``, ...) is the graph's.
        return getattr(self._graph, name)


def run_swarm(
    *,
    course_code: str = "CS201",
    run_id: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    human_authority: str = "course_faculty",
    output_dir: Optional[Path] = None,
    root: Optional[Path] = None,
    fail_until_attempt: int = 0,
    parallel: bool = True,
    max_concurrency: int = SWARM_MAX_CONCURRENCY,
) -> GraphRun:
    """Run the swarm graph to the human gate and return the handle.

    Args mirror :func:`~course_organizer.graph.runner.run_pipeline`, plus
    ``parallel`` and ``max_concurrency`` (the fan-out width, see
    :class:`_BoundedFanOut`).
    """
    run_id = run_id or f"lab08-{uuid.uuid4().hex[:8]}"
    base = Path(root) if root is not None else default_swarm_root()
    target = Path(output_dir) if output_dir is not None else base / run_id
    ctx = build_run_context(
        run_id,
        course_code=course_code,
        max_attempts=max_attempts,
        human_authority=human_authority,
        output_dir=target,
    )
    run = GraphRun(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        context=ctx,
    )
    with open_checkpointer(checkpoint_path(base, run_id)) as saver:
        graph = build_swarm_graph(ctx, parallel=parallel, checkpointer=saver)
        run.start(
            _BoundedFanOut(graph, max_concurrency),
            fail_until_attempt=fail_until_attempt,
        )
    return run


def resume_swarm(
    run_id: str,
    decision: Any,
    *,
    parallel: bool = True,
    root: Optional[Path] = None,
    max_concurrency: int = SWARM_MAX_CONCURRENCY,
) -> GraphRun:
    """Reopen a suspended swarm run from disk and resume it (cross-process)."""
    from langgraph.types import Command

    from course_organizer.graph.builder import RECURSION_BACKSTOP, graph_config

    base = Path(root) if root is not None else default_swarm_root()
    ctx = build_run_context(run_id, output_dir=base / run_id)
    run = GraphRun(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        context=ctx,
    )
    with open_checkpointer(checkpoint_path(base, run_id)) as saver:
        graph = build_swarm_graph(ctx, parallel=parallel, checkpointer=saver)
        cfg = {
            **graph_config(run.thread_id, run_id=run_id),
            "max_concurrency": max(1, int(max_concurrency)),
        }
        result = graph.invoke(
            Command(resume=decision),
            cfg,
            recursion_limit=RECURSION_BACKSTOP,
        )
        run._absorb(result)
    return run


__all__ = [
    "GHOST_ASSIGNMENT",
    "GENEROUS_FACTOR",
    "KIND_SUBMISSION",
    "KIND_UNIT",
    "PRIORITY_SUBMISSION",
    "PRIORITY_UNIT",
    "SUBMISSION_OBLIGATIONS",
    "SUBMISSION_STRATEGIES",
    "SWARM_MAX_CONCURRENCY",
    "SWARM_OUTPUT_ROOT",
    "SwarmState",
    "UNIT_OBLIGATIONS",
    "UNIT_STRATEGIES",
    "Alignment",
    "WorkItem",
    "align_submission",
    "align_unit",
    "alignment_to_plan",
    "build_base",
    "build_swarm_graph",
    "build_work_items",
    "default_swarm_root",
    "fan_out",
    "inspect_swarm",
    "make_candidate",
    "make_dispatch",
    "make_merge",
    "make_sequential",
    "make_swarm_worker",
    "merge_results",
    "plan_scope",
    "resume_swarm",
    "run_swarm",
    "slug",
    "swarm_edges",
    "swarm_nodes",
    "swarm_topology_lines",
    "timing_summary",
    "write_candidate_artifact",
]
