"""The six Phase-A specialist agents that run inside the ``workers`` node.

Doc2.4's roster (lines 515-523) names nine agents. Lab 7 does not make nine
nodes out of them, and the reason is worth stating because it is the whole
point of the node-graph exercise:

============  ==========================================  ==================
Roster slot   Agent                                        Where it runs
============  ==========================================  ==================
0             Coordinator                                  the graph itself
1             Notes Repository Agent                       ``workers``
2             Assignment Distribution Agent                ``workers``
3             Submission Collection Agent                  ``workers``
4             Plagiarism/Similarity Agent                  ``workers``
5             Grading Assistance Agent                     ``workers``
6             Project Milestone Tracking Agent             ``workers``
7             Faculty Review Agent                         ``gate``
8             Grade Publishing & Archive Agent             ``publish``
============  ==========================================  ==================

Coordinator, Faculty Review and Publishing are the graph's own nodes. The other
six are *sequential steps inside one node*, for three reasons:

1. Doc 2.5's message map (line 528) walks them as a chain, not a fan-out. Lab 8
   is where parallel execution gets introduced; doing it here would mean
   Lab 7's back-edge is really testing fan-in, and the two failures would be
   indistinguishable.
2. Six nodes would put the ``validate -> workers`` back-edge five hops behind
   ``validate``, so the retry would re-run four specialists that had nothing to
   do with the failure. Here the back-edge is one hop, and the evidence for
   *what got redone* is a single appended audit record per pass.
3. Each specialist still gets its own audit entry, so "which agent produced the
   bad data" stays answerable. Folding them into one node without that would
   throw away the attribution that makes a diagnosis useful.

Every specialist takes the same inputs and returns the same
:class:`SpecialistResult`, which is what lets :func:`run_specialists` treat
them uniformly and lets a test invoke one in isolation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from course_organizer.graph.state import DraftGradeRow
from course_organizer.schemas import MilestoneCheckpoint, ProjectMilestones

#: Doc 2.4 line 519: the similarity agent *"flags high-similarity cases for
#: review"*. It never decides, so this threshold selects what a human looks at.
#: It is deliberately low: flagging too many is a queue problem, flagging too
#: few is an integrity problem, and only the second one is irreversible.
SIMILARITY_FLAG_THRESHOLD = 0.30

#: Deterministic weighting across rubric criteria when drafting a grade. Chosen
#: so the per-criterion split sums exactly to the total awarded, which matters:
#: a draft whose criteria add up to more than the total is a validation
#: failure nobody can explain, and the rubric check only compares the total.
CRITERION_WEIGHTS: tuple[float, ...] = (0.5, 0.35, 0.15)


@dataclass(frozen=True)
class SpecialistResult:
    """What one specialist did, and what it wants the shared state to become.

    Attributes:
        step_id: Stable id, ``w1``..``w6``, matching the roster order.
        agent: The roster name from doc 2.4, verbatim.
        ok: Did this specialist complete. A specialist that fails does not
            raise -- it reports, and validation decides what happens next.
            Raising here would abort the graph and the back-edge could never
            fire, which is the exact failure mode Lab 7 exists to rule out.
        summary: One human-readable line for the audit trail.
        update: The partial state update to merge. Empty on failure, so a failed
            specialist cannot half-apply.
        read: Source records this specialist consulted, for the
            ``records_read`` total Lab 6 budgets.
    """

    step_id: str
    agent: str
    ok: bool
    summary: str
    update: dict[str, Any] = field(default_factory=dict)
    read: int = 0

    def audit(self) -> dict[str, Any]:
        """This specialist's audit entry, appended to ``audit_trail``.

        The entry names the agent, not just the step, because "the run failed"
        is not an answerable question and ``workers/w3/SimilarityAgent failed``
        is.
        """
        return {
            "step_id": self.step_id,
            "agent": self.agent,
            "ok": self.ok,
            "summary": self.summary,
            "read": self.read,
        }


def _tool(result: Any) -> Any:
    """Unwrap a tool result, or raise ``ToolError``.

    The graph has a *bounded retry* for validation failures. It does not have a
    bounded retry for "the corpus is missing", so an infrastructure failure has
    to surface as an exception rather than becoming an empty result that then
    fails validation -- which would burn all three attempts on a problem no
    retry can fix and escalate with a misleading diagnosis.

    ``ToolResult.unwrap`` already raises ``ToolError`` with the underlying
    connector message, so there is nothing to add here beyond the intent.
    """
    return result.unwrap()


# ---------------------------------------------------------------------------
# 1. Notes Repository Agent  (roster 1)
# ---------------------------------------------------------------------------


def notes_repository(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Read the current notes for every unit, preserving version history.

    Doc 2.12 line 830 lists *"Keep clear version history for notes and
    materials"* as a guardrail, so this reads with ``current_only=False`` and
    reports the version count rather than only the current text. A workspace
    that silently keeps the newest version and loses the older ones satisfies
    every other check in the pipeline and still breaks that guardrail.
    """
    result = _tool(
        ctx.tools.invoke(
            "read_notes_store",
            ctx.notes_query(course_code=state["course_code"], units=[]),
            context=ctx.tool_context,
        )
    )
    by_unit: dict[str, int] = {}
    for note in result.notes:
        by_unit[note.unit] = by_unit.get(note.unit, 0) + 1
    return SpecialistResult(
        step_id="w1",
        agent="Notes Repository Agent",
        ok=True,
        summary=(
            f"read {result.note_count} note(s) across "
            f"{len(result.units_covered)} unit(s) with "
            f"{result.version_total} version(s) retained"
        ),
        update={
            "notes_by_unit": by_unit,
            "units_covered": list(result.units_covered),
            "version_total": result.version_total,
            "course_name": result.course_name,
        },
        read=result.note_count,
    )


# ---------------------------------------------------------------------------
# 2. Assignment Distribution Agent  (roster 2)
# ---------------------------------------------------------------------------


def assignment_distribution(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Read the faculty-approved assignment briefs and rubrics.

    Doc 2.4 line 517 is a constraint, not a description: *"Distribute only
    faculty-approved assignment briefs and rubrics."* So this refuses to
    distribute an unapproved brief. It reports ``ok=False`` with an empty update
    rather than distributing it anyway -- the run then fails validation on a
    check that names the problem, instead of publishing against a rubric no
    human signed.
    """
    result = _tool(
        ctx.tools.invoke(
            "read_assignment_rubrics",
            ctx.assignment_query(course_code=state["course_code"]),
            context=ctx.tool_context,
        )
    )
    rows = [
        {
            "assignment_id": a.assignment_id,
            "title": a.title,
            "unit": a.unit,
            "max_marks": a.max_marks,
            "approved_by": a.approved_by,
            # Carried as an ISO string so the row is JSON-clean for the
            # checkpoint. The milestone specialist parses it back rather than
            # holding a datetime, because a datetime in the state would need a
            # serializer on every resume.
            "due_date": a.due_date.isoformat(),
            "rubric": [
                {
                    "criterion_id": c.criterion_id,
                    "description": c.description,
                    "max_marks": c.max_marks,
                }
                for c in a.rubric
            ],
        }
        for a in result.assignments
        if a.approved_by
    ]
    withheld = len(result.assignments) - len(rows)
    return SpecialistResult(
        step_id="w2",
        agent="Assignment Distribution Agent",
        ok=not withheld,
        summary=(
            f"distributed {len(rows)} faculty-approved brief(s)"
            + (f"; withheld {withheld} unapproved" if withheld else "")
        ),
        update={"assignments": rows},
        read=len(result.assignments),
    )


# ---------------------------------------------------------------------------
# 3. Submission Collection Agent  (roster 3)
# ---------------------------------------------------------------------------


def submission_collection(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Collect submissions and derive lateness from the store's own seconds.

    Doc 2.4 line 518 and doc 2.12 line 833 both forbid altering a submission
    timestamp, so ``late`` is *derived* here from ``late_by_seconds`` and the
    timestamp is carried through untouched. The ``late_flags`` list is kept
    separately because validation re-derives its expectation from the store and
    compares, which is only a real check if the two were computed independently.
    """
    result = _tool(
        ctx.tools.invoke(
            "read_submission_records",
            ctx.submission_query(course_code=state["course_code"], statuses=[]),
            context=ctx.tool_context,
        )
    )
    rows = [
        {
            "submission_id": s.submission_id,
            "assignment_id": s.assignment_id,
            "student_id": s.student_id,
            "student_name": s.student_name,
            "submitted_at": s.submitted_at.isoformat(),
            "status": s.status.value,
            "late_by_seconds": s.late_by_seconds,
            "store_similarity_score": s.similarity_score,
        }
        for s in result.submissions
    ]
    late_flags = [
        {
            "submission_id": s.submission_id,
            "late_by_seconds": s.late_by_seconds,
            "late": s.late_by_seconds > 0,
        }
        for s in result.submissions
    ]
    return SpecialistResult(
        step_id="w3",
        agent="Submission Collection Agent",
        ok=True,
        summary=(
            f"collected {len(rows)} submission(s): "
            f"{result.on_time_count} on time, {result.late_count} late, "
            f"{result.previously_scored_count} previously scored"
        ),
        update={
            "submissions": rows,
            "late_flags": late_flags,
            "reported_late_count": result.late_count,
        },
        read=len(result.submissions),
    )


# ---------------------------------------------------------------------------
# 4. Plagiarism/Similarity Agent  (roster 4)
# ---------------------------------------------------------------------------


def similarity(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Attach a similarity score to every submission and flag the high ones.

    Doc 2.4 line 519 and doc 2.12 line 831 agree on the limit: the agent
    *flags*, it never reaches a final determination. So a score is attached to
    every submission, and ``similarity_flagged`` marks the ones above
    :data:`SIMILARITY_FLAG_THRESHOLD` for faculty. Nothing here sets a grade
    penalty.

    The score is a **real measurement of the real text**: the maximum Jaccard
    overlap of word-trigram shingles against any other submission *in the same
    assignment*. Comparing across assignments would be meaningless (a shared
    word about ``O(n)`` is not evidence of anything), and comparing within one
    assignment is the question a marker would actually ask.

    Each flag names the submission it overlaps most, via ``similarity_against``.
    A flag that says only "this one scored high" is not actionable; one that
    says "overlaps DS-A1-S003 by 0.41" can be opened and read. The store's own
    ``similarity_score`` is preferred wherever it is populated -- this measures
    only where the store has no score.

    **This is where the back-edge is demonstrated.** When ``fail_until_attempt``
    is set, the first ``fail_until_attempt - 1`` passes skip scoring entirely,
    reproducing the real failure this lab cares about: a pass that completes
    without ever attaching a score, which doc 2.7's third check then catches.
    That is an *injected* defect, and the documentation says so -- the pipeline
    does not fail at random.
    """
    attempt = int(state.get("attempts", 0))
    fail_until = int(state.get("fail_until_attempt", 0) or 0)
    if fail_until > 0 and attempt < fail_until:
        count = len(state.get("submissions", []))
        return SpecialistResult(
            step_id="w4",
            agent="Plagiarism/Similarity Agent",
            ok=False,
            summary=(
                f"skipped scoring on attempt {attempt + 1}: "
                f"{count} submission(s) left without a score"
            ),
            update={"similarity_scores": {}, "similarity_flags": []},
            read=count,
        )

    # Read the submissions again for their text. The state deliberately does not
    # carry `content`: it is the largest field in the store and would bloat
    # every checkpoint. Re-reading is one tool call, and the tool ledger
    # records it like any other read.
    records = _tool(
        ctx.tools.invoke(
            "read_submission_records",
            ctx.submission_query(course_code=state["course_code"], statuses=[]),
            context=ctx.tool_context,
        )
    ).submissions

    by_assignment: dict[str, list[Any]] = {}
    for record in records:
        by_assignment.setdefault(record.assignment_id, []).append(record)

    shingles = {r.submission_id: _shingles(r.content) for r in records}
    scores: dict[str, float] = {}
    against: dict[str, str] = {}
    flags: list[dict[str, Any]] = []
    for record in records:
        peers = [
            p for p in by_assignment[record.assignment_id]
            if p.submission_id != record.submission_id
        ]
        stored = record.similarity_score
        best_peer = ""
        best = 0.0
        for peer in peers:
            overlap = _jaccard(
                shingles[record.submission_id], shingles[peer.submission_id]
            )
            if overlap > best:
                best = overlap
                best_peer = peer.submission_id
        # The store wins when it has a score: it is the authoritative record.
        if stored is not None:
            score, peer = float(stored), (record.similarity_against or [""])[0]
        else:
            score, peer = round(best, 4), best_peer
        scores[record.submission_id] = score
        if peer:
            against[record.submission_id] = peer
        if score >= SIMILARITY_FLAG_THRESHOLD:
            flags.append(
                {
                    "submission_id": record.submission_id,
                    "score": score,
                    "threshold": SIMILARITY_FLAG_THRESHOLD,
                    "against": peer,
                    "source": "store" if stored is not None else "measured",
                    "reason": (
                        f"similarity {score:g} against {peer} in "
                        f"{record.assignment_id} is at or above the faculty "
                        f"review threshold; flagged for human review, not "
                        f"adjudicated"
                    ),
                }
            )

    return SpecialistResult(
        step_id="w4",
        agent="Plagiarism/Similarity Agent",
        ok=True,
        summary=(
            f"attached {len(scores)} similarity score(s) "
            f"({sum(1 for s in scores.values() if s >= SIMILARITY_FLAG_THRESHOLD)} "
            f"at or above threshold); flagged {len(flags)} for faculty review"
        ),
        update={
            "similarity_scores": scores,
            "similarity_against": against,
            "similarity_flags": flags,
        },
        read=len(records),
    )


#: Trigrams of normalised words. Three is the smallest width that distinguishes
#: "the same phrasing" from "the same word", which is the distinction a marker
#: cares about; bigrams over-fire on shared technical vocabulary.
SHINGLE_WIDTH = 3

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Extremely common words carry no signal about whether two students wrote the
#: same sentence, and including them pushes every pair's overlap upwards by a
#: similar amount. Excluding them makes the score mean something. A production
#: system would use a corpus-derived stop list; this is the small, documented
#: subset of it.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been but by can did do does for from had has have he
    her his i if in into is it its me my no not of on or our she so than that
    the their them then there these they this to was we were what when which
    who will with would you your
    """.split()
)


def _shingles(text: str, width: int = SHINGLE_WIDTH) -> frozenset[tuple[str, ...]]:
    """Normalise ``text`` and return its word-``width`` shingles.

    Returns a set of tuples rather than a joined string so two shingles can
    never collide by containing the join character. An empty or very short
    input yields the empty set, which makes every Jaccard against it ``0.0``
    rather than a division by zero.
    """
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS]
    if len(words) < width:
        return frozenset({tuple(words)})
    return frozenset(
        tuple(words[i : i + width]) for i in range(len(words) - width + 1)
    )


def _jaccard(left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]) -> float:
    """Jaccard overlap: ``|A and B| / |A or B|``. Empty on either side gives 0.0.

    Range is 0.0 to 1.0 and 1.0 means *identical*, so the score is directly
    interpretable as "this fraction of the phrasing is shared" rather than being
    an opaque number needing a legend.
    """
    if not left or not right:
        return 0.0
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


# ---------------------------------------------------------------------------
# 5. Grading Assistance Agent  (roster 5)
# ---------------------------------------------------------------------------


def grading(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Draft a grade per submission, every one still a ``draft``.

    Doc 2.4 line 520 frames this as *assistance*: the agent drafts and the
    human approves. So every row leaves here with ``status="draft"`` and no
    ``approved_by``. The ``gate`` node is the only thing that can change that,
    which makes doc 2.7's fourth check mechanically true rather than a promise.

    The awarded marks are capped at the rubric maximum by construction. That
    check then still runs and still passes -- the point is that the cap holds
    for the *right* reason, and the ``fail_until_attempt`` injection in the
    grading step is what proves the check has teeth.
    """
    attempt = int(state.get("attempts", 0))
    fail_until = int(state.get("fail_until_attempt", 0) or 0)
    injecting = fail_until > 0 and attempt < fail_until

    assignments = {a["assignment_id"]: a for a in state.get("assignments", [])}
    late_by = {
        str(f["submission_id"]): int(f["late_by_seconds"])
        for f in state.get("late_flags", [])
    }
    scores = state.get("similarity_scores", {}) or {}
    against = state.get("similarity_against", {}) or {}
    flagged = {
        str(f["submission_id"]) for f in state.get("similarity_flags", [])
    }
    grades: list[DraftGradeRow] = []
    for row in state.get("submissions", []):
        submission_id = str(row["submission_id"])
        assignment = assignments.get(str(row["assignment_id"]), {})
        ceiling = float(assignment.get("max_marks") or 0.0)
        rubric = assignment.get("rubric", []) or []
        if injecting:
            # The injected defect: award above the ceiling so doc 2.7's first
            # check fails and the back-edge has something to catch.
            awarded = ceiling + max(1.0, ceiling * 0.1)
        else:
            awarded = _draft_marks(submission_id, ceiling)
        criterion_marks = _split_criteria(rubric, awarded)
        grades.append(
            DraftGradeRow(
                submission_id=submission_id,
                student_name=str(row.get("student_name", "")),
                assignment_id=str(row.get("assignment_id", "")),
                awarded_marks=round(awarded, 2),
                max_marks=ceiling,
                criterion_marks=criterion_marks,
                feedback=_feedback(submission_id, awarded, ceiling),
                similarity_score=scores.get(submission_id),
                similarity_against=against.get(submission_id, ""),
                similarity_flagged=submission_id in flagged,
                late=int(late_by.get(submission_id, 0)) > 0,
                status="draft",
            )
        )
    overs = sum(1 for g in grades if g.awarded_marks > g.max_marks)
    return SpecialistResult(
        step_id="w5",
        agent="Grading Assistance Agent",
        ok=not injecting,
        summary=(
            f"drafted {len(grades)} grade(s)"
            + (f", {overs} above the rubric maximum" if overs else "")
            + "; all drafts pending faculty approval"
        ),
        update={"draft_grades": grades},
        read=len(grades),
    )


def _draft_marks(seed: str, ceiling: float) -> float:
    """A deterministic draft mark in [40%, 95%] of the ceiling.

    The band is narrow on purpose: a drafter that awards near-perfect marks
    makes the reviewer's job much harder than one that is visibly provisional,
    and doc 2.4's whole premise is that the draft is a suggestion for a human.
    """
    if ceiling <= 0:
        return 0.0
    digest = hashlib.sha256(("marks:" + seed).encode("utf-8")).digest()
    fraction = 0.40 + (int.from_bytes(digest[:2], "big") / 0xFFFF) * 0.55
    return round(ceiling * fraction, 2)


def _split_criteria(rubric: Sequence[Mapping[str, Any]], awarded: float) -> dict[str, float]:
    """Split the awarded marks across the rubric criteria.

    The split is by :data:`CRITERION_WEIGHTS` when the criterion count matches,
    and the last criterion absorbs the rounding remainder. Without that, the
    criteria would sum to a number that differs from the total by a fraction of
    a mark, and a reviewer comparing the two would be right to ask which one is
    correct.
    """
    if not rubric:
        return {}
    if len(rubric) == len(CRITERION_WEIGHTS):
        parts = [round(awarded * w, 2) for w in CRITERION_WEIGHTS[:-1]]
        parts.append(round(awarded - sum(parts), 2))
    else:
        share = round(awarded / len(rubric), 2)
        parts = [share] * len(rubric)
        parts[-1] = round(awarded - sum(parts[:-1]), 2)
    return {str(c["criterion_id"]): part for c, part in zip(rubric, parts)}


def _feedback(submission_id: str, awarded: float, ceiling: float) -> str:
    """Provisional feedback text. Names the ratio, asserts no judgement.

    Doc 2.12 line 831's over-automation guardrail is about determinations, and
    a feedback string that reads as a verdict invites a reviewer to rubber-stamp
    it. So this states the number and stops.
    """
    pct = (awarded / ceiling * 100) if ceiling else 0.0
    return (
        f"Draft only, pending faculty review. Provisional mark "
        f"{awarded:g} of {ceiling:g} ({pct:.0f}%) for {submission_id}."
    )


# ---------------------------------------------------------------------------
# 6. Project Milestone Tracking Agent  (roster 6)
# ---------------------------------------------------------------------------


def milestones(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Derive one milestone checkpoint per assignment that has submissions.

    Doc 2.6 names project milestones as an entity, but the seed store carries
    no independent milestone table, so they are derived from the assignments
    themselves -- one checkpoint per assignment that actually has submissions,
    with the assignment's own due date. Deriving beats inventing: the dates are
    grounded in faculty-approved records instead of being made up, and a
    milestone with a due date nobody approved would contradict doc 2.4 line 517.
    """
    submissions = state.get("submissions", [])
    assignments = {a["assignment_id"]: a for a in state.get("assignments", [])}
    by_assignment: dict[str, list[Mapping[str, Any]]] = {}
    for row in submissions:
        by_assignment.setdefault(str(row["assignment_id"]), []).append(row)

    rows: list[dict[str, Any]] = []
    for assignment_id, group in sorted(by_assignment.items()):
        assignment = assignments.get(assignment_id)
        if assignment is None:
            continue
        project = ProjectMilestones(
            project_id=f"PRJ-{assignment_id}",
            course_code=state["course_code"],
            group_id=f"GRP-{assignment_id}",
            members=sorted({str(r["student_id"]) for r in group}),
            checkpoints=[
                MilestoneCheckpoint(
                    checkpoint_id=f"{assignment_id}-M1",
                    name=str(assignment["title"]),
                    due_date=_parse_due(assignment["due_date"]),
                    expected_deliverable=(
                        f"{len(group)} submission(s) received for "
                        f"{assignment_id}"
                    ),
                )
            ],
        )
        rows.append(
            {
                "project_id": project.project_id,
                "group_id": project.group_id,
                "members": project.members,
                "checkpoints": [
                    {
                        "checkpoint_id": c.checkpoint_id,
                        "name": c.name,
                        "due_date": c.due_date.isoformat(),
                        "expected_deliverable": c.expected_deliverable,
                        "status": c.status.value,
                    }
                    for c in project.checkpoints
                ],
            }
        )
    return SpecialistResult(
        step_id="w6",
        agent="Project Milestone Tracking Agent",
        ok=True,
        summary=f"tracked {len(rows)} project milestone group(s)",
        update={"milestones": rows},
        read=len(rows),
    )


def _parse_due(value: Any) -> datetime:
    """Turn a checkpointed ISO string back into a datetime for the model.

    ``assignment_distribution`` stores due dates as ISO strings so the state
    stays JSON-clean across a SQLite round-trip. This is the inverse. A
    ``KeyError`` here would mean the assignment rows came from somewhere that
    did not carry a due date, so the failure names that rather than returning a
    sentinel date that would quietly make every milestone look on time.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


#: The six, in doc 2.4 roster order. Named so a test can assert the graph runs
#: all of them and in this order, rather than trusting this mapping.
SPECIALISTS: tuple[tuple[str, str, Callable[..., SpecialistResult]], ...] = (
    ("w1", "Notes Repository Agent", notes_repository),
    ("w2", "Assignment Distribution Agent", assignment_distribution),
    ("w3", "Submission Collection Agent", submission_collection),
    ("w4", "Plagiarism/Similarity Agent", similarity),
    ("w5", "Grading Assistance Agent", grading),
    ("w6", "Project Milestone Tracking Agent", milestones),
)

#: Just the names, for the run's audit summary and the tests.
SPECIALIST_NAMES: tuple[str, ...] = tuple(a for _, a, _ in SPECIALISTS)


def run_specialists(
    state: Mapping[str, Any], ctx: Any, *, only: Optional[Sequence[str]] = None
) -> tuple[list[SpecialistResult], dict[str, Any]]:
    """Run the specialists in roster order and merge their updates.

    Args:
        state: The shared graph state.
        ctx: Per-run dependencies -- the tool registry and query factories.
        only: Restrict to these ``step_id``s. Used by tests to run one
            specialist; the graph itself always runs all six so the roster
            coverage test is meaningful.

    Returns:
        ``(results, merged)``. The merged dict is built by applying updates in
        order, later wins. ``draft_grades`` is the one key a single specialist
        owns end to end, so LastValue is correct for all of them.

    Note:
        Each specialist sees the updates of the ones before it. This is
        essential and easy to get wrong: ``similarity`` computes the scores and
        ``grading`` attaches them to the rows, so grading cannot work if both
        read the *input* state. Handing every specialist the same input dict
        makes the pipeline silently lose data between two specialists that each
        report ``ok=True`` -- which is exactly the kind of failure that looks
        healthy in a log.
    """
    wanted = set(only) if only is not None else None
    results: list[SpecialistResult] = []
    merged: dict[str, Any] = {}
    for step_id, agent, fn in SPECIALISTS:
        if wanted is not None and step_id not in wanted:
            continue
        current: dict[str, Any] = {**state, **merged}
        result = fn(current, ctx)
        results.append(result)
        merged.update(result.update)
    return results, merged


__all__ = [
    "CRITERION_WEIGHTS",
    "SIMILARITY_FLAG_THRESHOLD",
    "SPECIALISTS",
    "SPECIALIST_NAMES",
    "SpecialistResult",
    "assignment_distribution",
    "grading",
    "milestones",
    "notes_repository",
    "run_specialists",
    "similarity",
    "submission_collection",
]