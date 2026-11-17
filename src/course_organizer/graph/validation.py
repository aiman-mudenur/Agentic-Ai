"""Doc 2.7's ValidationAgent: the hard gate, and the explanation of a failure.

Line 558 gives the whole function, and it is worth quoting in full because two
details in it are load-bearing:

    function ValidationAgent.validate(ctx, plan):
        checks = [
          ('a draft grade never exceeds the rubric maximum', test_0(ctx, plan)),
          ('late submissions are flagged accurately',            test_1(ctx, plan)),
          ('a plagiarism/similarity score is attached',           test_2(ctx, plan)),
          ('no grade is published without faculty approval',      test_3(ctx, plan)),
        ]
        failing = [n for n, ok in checks if not ok]
        return Report(passed = not failing, failing = failing,
                      diagnosis = explain(failing))

**It returns. It does not raise, and it does not loop.** The regenerate loop lives
in the *graph*, on the ``validate --fail--> workers`` edge (line 696). That
separation is the lab: if validation retried internally, the back-edge would have
nothing to be a back-edge *to*, and the doc's *"Bound the retry edge"* requirement
would be satisfied by a ``while`` loop nobody can see in the topology.

``explain`` is the part that is easy to leave as a placeholder. It is not one
here, because Lab 7 reaches escalation through this string: doc 2.12 line 825
asks for *"escalate to course faculty with a diagnosis"*, so the diagnosis has to
identify **which** check failed, **on what value**, and **what would fix it**. A
diagnosis of *"validation failed"* sends the human back to the start.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from course_organizer.graph.state import DOC_CHECKS, DraftGradeRow, ValidationOutcome
from course_organizer.evidence import CandidateEvidence

#: The rubric tolerance for doc 2.7's first check. Zero: the doc says a draft
#: grade *never* exceeds the maximum (line 496, line 558), so any headroom would
#: be a loophole written into the acceptance criterion.
RUBRIC_TOLERANCE = 0.0

#: Doc 2.4 line 519: the similarity agent *"flags high-similarity cases for
#: review"*. It does not decide, so this threshold is about flagging, not judging.
SIMILARITY_FLAG_THRESHOLD = 0.30


def check_draft_grade_within_rubric_maximum(
    grades: list[DraftGradeRow], max_marks_by_assignment: Mapping[str, float]
) -> tuple[bool, str]:
    """Doc check 0: *\"a draft grade never exceeds the rubric maximum\"*.

    Doc 2.2 (line 496) names this as the worked example of the KPI *\"100% of
    validation checks pass before course faculty review\"*, so it is the check the
    whole pipeline is built to protect.

    Compared against the *rubric maximum* rather than the assignment's headline
    ``max_marks`` where they differ, because the rubric is what faculty approved
    (doc 2.4 line 517: *"Distribute only faculty-approved assignment briefs and
    rubrics"*).

    Args:
        grades: The draft grades under test.
        max_marks_by_assignment: The ceiling for each assignment id.

    Returns:
        ``(passed, detail)``. The detail names the offending row and both numbers,
        because *"grade too high"* with no ids is not a diagnosis.
    """
    if not grades:
        return False, "no draft grades to check; the workers node produced nothing"
    breaches: list[str] = []
    for row in grades:
        ceiling = max_marks_by_assignment.get(row.assignment_id, row.max_marks)
        if ceiling <= 0:
            breaches.append(
                f"{row.submission_id} has no rubric maximum for "
                f"{row.assignment_id or 'its assignment'}, so its marks cannot "
                f"be checked"
            )
            continue
        if row.awarded_marks > ceiling + RUBRIC_TOLERANCE:
            breaches.append(
                f"{row.submission_id} was awarded {row.awarded_marks:g} against "
                f"a rubric maximum of {ceiling:g} for {row.assignment_id}"
            )
    if breaches:
        return False, "; ".join(breaches)
    return True, (
        f"all {len(grades)} draft grade(s) are within their rubric maximum"
    )


def check_late_submissions_flagged_accurately(
    grades: list[DraftGradeRow],
    late_by_submission: Mapping[str, int],
) -> tuple[bool, str]:
    """Doc check 1: *\"late submissions are flagged accurately\"*.

    Accuracy is two-sided and the second half is the one that gets skipped: a
    late submission that is *not* flagged is a false negative, and a submission
    marked late when ``late_by_seconds`` is 0 is a false positive. Doc 2.4 line
    518 guards the timestamp itself -- *"never alter a submission timestamp"* --
    so this check reads the store's own value rather than a stored flag, and the
    store is the single source of truth.

    Args:
        grades: The draft grades under test.
        late_by_submission: Seconds late per submission id, from the store.

    Returns:
        ``(passed, detail)``.
    """
    if not late_by_submission:
        return True, "no submission lateness to compare against"
    mismatches: list[str] = []
    by_id = {row.submission_id: row for row in grades}
    for submission_id, seconds in sorted(late_by_submission.items()):
        expected = int(seconds) > 0
        row = by_id.get(submission_id)
        if row is None:
            mismatches.append(
                f"{submission_id} is {'late' if expected else 'on time'} in the "
                f"store but has no draft grade to flag"
            )
            continue
        if row.late != expected:
            mismatches.append(
                f"{submission_id} is {'late' if expected else 'on time'} by the "
                f"store ({seconds}s) but the grade says "
                f"{'late' if row.late else 'on time'}"
            )
    if mismatches:
        return False, "; ".join(mismatches)
    flagged = sum(1 for row in grades if row.late)
    return True, (
        f"all {len(late_by_submission)} submission(s) agree with the store; "
        f"{flagged} flagged late"
    )


def check_similarity_score_attached(
    grades: list[DraftGradeRow],
) -> tuple[bool, str]:
    """Doc check 2: *\"a plagiarism/similarity score is attached\"*.

    Every graded submission needs a score. Doc 2.4 line 519 is emphatic that the
    similarity agent *"flags"* and *"never makes a final plagiarism/similarity
    determination automatically"* (doc 2.12 line 831) -- so this check demands
    that a score *exists*, never that it is high or low.

    Args:
        grades: The draft grades under test.

    Returns:
        ``(passed, detail)``.
    """
    if not grades:
        return False, "no draft grades to check for a similarity score"
    missing = [row.submission_id for row in grades if row.similarity_score is None]
    if missing:
        return False, (
            f"{len(missing)} of {len(grades)} submission(s) have no similarity "
            f"score: {', '.join(sorted(missing))}"
        )
    flagged = [row.submission_id for row in grades if row.similarity_flagged]
    return True, (
        f"all {len(grades)} submission(s) carry a similarity score; "
        f"{len(flagged)} flagged for faculty review"
    )


def check_no_grade_published_without_faculty_approval(
    grades: list[DraftGradeRow],
) -> tuple[bool, str]:
    """Doc check 3: *\"no grade is published without faculty approval\"*.

    Doc 2.12 line 830 states this as the first guardrail, and it is the check
    that can only be written *after* the gate exists -- which is why it belongs
    in Lab 7 and not in Lab 3. Before the gate, every grade is ``draft`` and this
    passes trivially. After a faculty decision, a row that says ``published`` with
    no ``approved_by`` means the gate was bypassed, and this says so by name.

    Args:
        grades: The draft grades under test.

    Returns:
        ``(passed, detail)``.
    """
    unapproved = [
        f"{row.submission_id} (status={row.status})"
        for row in grades
        if row.status != "draft" and not row.approved_by
    ]
    if unapproved:
        return False, (
            f"{len(unapproved)} grade(s) are past draft with nobody's name on "
            f"them: {', '.join(sorted(unapproved))}"
        )
    published = [row for row in grades if row.status == "published"]
    if not published:
        return True, "every grade is still a draft, as it must be before approval"
    named = sum(1 for row in published if row.approved_by)
    return True, (
        f"{len(published)} published grade(s), all {named} carrying an approver"
    )


def check_evidence_grounded(
    grades: list[DraftGradeRow],
    evidence_tables: Mapping[str, Any],
) -> tuple[bool, str]:
    """Lab 9 check: *every draft grade is grounded in faculty-approved sources*.

    Each grade needs an evidence table whose every item resolves to a real,
    authorized, in-scope, current source with a quoted excerpt. A missing
    table, an ungrounded item, or a table with no items fails the grade by
    name — fail-closed, because an ungrounded candidate must never advance to
    faculty review looking validated.

    Args:
        grades: The draft grades under test.
        evidence_tables: Submission id to :class:`CandidateEvidence` (or its
            checkpoint-JSON dump — resuming from sqlite yields dicts, and a
            check that only accepted the model would fail every resumed run).

    Returns:
        ``(passed, detail)``.
    """
    if not grades:
        return False, "no draft grades to ground; the workers node produced nothing"
    problems: list[str] = []
    for row in grades:
        raw = (evidence_tables or {}).get(row.submission_id)
        if raw is None:
            problems.append(
                f"{row.submission_id} has no evidence table; ground it before review"
            )
            continue
        try:
            table = raw if isinstance(raw, CandidateEvidence) else CandidateEvidence.model_validate(raw)
        except Exception:
            problems.append(
                f"{row.submission_id} has an unreadable evidence table; ground it before review"
            )
            continue
        if not table.items:
            problems.append(
                f"{row.submission_id} has an empty evidence table; ground it before review"
            )
            continue
        if not table.supported:
            problems.append(
                f"{row.submission_id} ungrounded: {'; '.join(table.reasons())}"
            )
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"all {len(grades)} draft grade(s) grounded in faculty-approved sources"
    )


#: The doc's pairs plus Lab 9's grounding check, in run order. Kept as a
#: mapping so a caller cannot reorder them by accident and so a missing check
#: is a KeyError rather than a silently shorter list.
CHECKS: dict[str, Callable[[Any], tuple[bool, str]]] = {
    "draft_grade_within_rubric_maximum": (
        lambda ctx: check_draft_grade_within_rubric_maximum(
            ctx.grades, ctx.max_marks_by_assignment
        )
    ),
    "late_submissions_flagged_accurately": (
        lambda ctx: check_late_submissions_flagged_accurately(
            ctx.grades, ctx.late_by_submission
        )
    ),
    "similarity_score_attached": (
        lambda ctx: check_similarity_score_attached(ctx.grades)
    ),
    "no_grade_published_without_faculty_approval": (
        lambda ctx: check_no_grade_published_without_faculty_approval(ctx.grades)
    ),
    "evidence_grounded": (
        lambda ctx: check_evidence_grounded(ctx.grades, ctx.evidence_tables)
    ),
}


class CheckContext:
    """Everything the five checks read. A named bundle, so a sixth check is a
    deliberate signature change rather than a silent widening."""

    __slots__ = ("grades", "max_marks_by_assignment", "late_by_submission", "evidence_tables")

    def __init__(
        self,
        grades: list[DraftGradeRow],
        max_marks_by_assignment: Mapping[str, float],
        late_by_submission: Mapping[str, int],
        evidence_tables: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.grades = grades
        self.max_marks_by_assignment = max_marks_by_assignment
        self.late_by_submission = late_by_submission
        #: Submission id to evidence table (model or checkpoint dump). ``None``
        #: means "no tables were built" — the grounding check then fails closed
        #: rather than passing vacuously.
        self.evidence_tables = evidence_tables or {}


def validate(
    ctx: CheckContext, *, attempt: int = 0
) -> ValidationOutcome:
    """Run doc 2.7's checks plus Lab 9 grounding and return the doc's ``Report``.

    Args:
        ctx: The grades and reference values under test.
        attempt: Which attempt this is, carried into the report so a diagnosis
            can say *third* try rather than just *failed*.

    Returns:
        The report. ``passed`` is ``not failing``, as the doc computes it.
    """
    failing: list[str] = []
    details: list[str] = []
    for name in DOC_CHECKS:
        passed, detail = CHECKS[name](ctx)
        if not passed:
            failing.append(name)
            details.append(f"{name}: {detail}")
    return ValidationOutcome(
        passed=not failing,
        failing=failing,
        diagnosis=explain_failure(failing, details, attempt=attempt),
        checks_run=list(DOC_CHECKS),
        attempt=attempt,
    )


def explain_failure(
    failing: list[str], details: list[str], *, attempt: int = 0
) -> str:
    """Doc 2.7's ``diagnosis = explain(failing)``, actually implemented.

    Doc 2.12 line 825 requires escalation *\"with a diagnosis\"*, and line 826's
    over-automation row says the human's options must be *\"effortless and
    logged\"*. A diagnosis that only names the failing check therefore fails the
    requirement: it hands back work rather than a decision. Each clause here
    answers a different question -- what failed, on what value, what fixes it.

    Args:
        failing: The names of the checks that failed.
        details: Their details, positionally matching ``failing``.
        attempt: The attempt number, when known.

    Returns:
        One paragraph. Empty when nothing failed, so a passing run has no
        diagnosis rather than a cheerful one.
    """
    if not failing:
        return ""
    which = ", ".join(failing)
    count = len(failing)
    # The spaces live in the f-string below, never inside ``ordinal``. Baking
    # them into the fragment means the diagnosis opens with a space, and any
    # caller that interpolates it ("... forever. {diagnosis}") then renders a
    # double space -- which is exactly what the escalation did until this was
    # fixed, and the kind of artefact that only a rendered transcript shows.
    ordinal = "" if attempt <= 1 else f"Attempt {attempt} "
    body = ". ".join(d.rstrip(".") for d in details) if details else ""
    fixes = {
        "draft_grade_within_rubric_maximum": (
            "re-score the affected submission(s) against the faculty-approved "
            "rubric, or raise the rubric maximum itself -- the second needs a "
            "faculty decision recorded first"
        ),
        "late_submissions_flagged_accurately": (
            "re-read the submission timestamps from the store; doc 2.4 forbids "
            "altering them, so the flags move, not the timestamps"
        ),
        "similarity_score_attached": (
            "run the similarity step for the submission(s) with no score; an "
            "absent score is not a zero score"
        ),
        "no_grade_published_without_faculty_approval": (
            "do not publish; the gate was bypassed. Route the run back to the "
            "gate so a decision is recorded against a named human"
        ),
        "evidence_grounded": (
            "ground the affected submission(s): cite a real syllabus unit, "
            "current note version, rubric criterion, or submission record with "
            "a quoted excerpt — then revalidate"
        ),
    }
    remedies = "; ".join(
        fixes.get(name, "inspect the failing unit") for name in failing
    )
    return (
        f"{ordinal}validation failed {count} of {len(DOC_CHECKS)} check(s): "
        f"{which}. {body}. To clear this: {remedies}."
    )


def next_route(report: ValidationOutcome, *, attempts: int, max_attempts: int,
               unrepairable: bool = False) -> str:
    """Which way the ``validate`` node routes, per doc line 696.

    The doc's two edges are ``--fail--> workers`` and ``--pass--> assemble``, and
    doc 2.12's ``MAX_ATTEMPTS`` bound adds a third destination the pseudocode does
    not spell out: exhaustion. Deciding it *here* rather than inside the node
    keeps one function responsible for "what does a failing validation mean", so
    the answer is testable without running a graph.

    Args:
        report: This attempt's validation report.
        attempts: Attempts made so far, including this one.
        max_attempts: The bound from doc 2.12.

    Returns:
        :data:`~course_organizer.graph.state.ROUTE_RETRY`,
        ``ROUTE_PASS``, or ``ROUTE_ESCALATE``.

    Note:
        The order matters. ``passed`` is tested *before* everything else,
        because a run that passed on its last permitted attempt must advance.
        ``unrepairable`` comes next: when no finding has a safe automatic
        repair, retrying would burn budget proving nothing, so the run
        escalates immediately (Lab 10). It defaults to False, so every
        existing caller keeps the historical routing.
    """
    from course_organizer.graph.state import ROUTE_ESCALATE, ROUTE_PASS, ROUTE_RETRY

    if report.passed:
        return ROUTE_PASS
    if unrepairable:
        return ROUTE_ESCALATE
    if attempts >= max_attempts:
        return ROUTE_ESCALATE
    return ROUTE_RETRY


def missing_checks() -> tuple[str, ...]:
    """Which of the doc's checks are not implemented. Empty, and a test says so.

    A check that exists in the doc but not in this mapping would be a silent gap:
    the report would say *4 of 4* while running three.
    """
    implemented = set(CHECKS)
    return tuple(name for name in DOC_CHECKS if name not in implemented)


def summarise(report: ValidationOutcome) -> Optional[str]:
    """One line for a log, or ``None`` when the run passed."""
    if report.passed:
        return None
    return (
        f"attempt {report.attempt}: {len(report.failing)} of "
        f"{len(report.checks_run)} check(s) failed ({', '.join(report.failing)})"
    )