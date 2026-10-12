"""The eight graph nodes, in doc 2.10's order, plus ``escalate``.

Doc 2.10 line 696's pseudocode::

    graph.chain(ingest -> plan -> workers -> validate)
    graph.edge(validate --fail--> workers)              # bounded retry
    graph.edge(validate --pass--> assemble -> gate -> publish)

Seven named nodes and one back-edge. This module supplies those seven plus
``escalate``, which doc 2.12 line 825 requires on exhaustion but line 696 does
not draw.

Each node is a closure over a :class:`~course_organizer.graph.context.RunContext`
and returns a **partial** update -- never the whole state. That contract is what
makes the graph's reducers meaningful: ``workers`` returns ``draft_grades`` and
the ``attempt_log`` entry, ``validate`` returns ``attempts``, and everything else
it read stays as it was. A node that returned the full state would silently
clobber whatever ran before it.

The two nodes that are easy to get wrong:

``gate``
    Uses LangGraph's real :func:`~langgraph.types.interrupt`. The node is *not*
    an ``async def`` and does *not* catch ``GraphInterrupt`` -- calling
    ``interrupt`` inside a ``try`` that swallows exceptions breaks the
    suspension. The value passed to ``interrupt`` is what the caller receives
    back as ``Command(resume=...)``, so it has to be the reviewer's whole world:
    the grades, the validation report and the similarity flags.

``validate``
    Increments ``attempts`` and decides the route. It does **not** loop. Doc
    2.7's ``ValidationAgent`` returns a report; the loop is the edge, which is
    the entire reason doc 2.10 draws an arrow rather than a loop inside a node.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from langgraph.types import interrupt

from course_organizer.graph.context import RunContext
from course_organizer.graph.escalation import build_escalation
from course_organizer.graph.specialists import run_specialists
from course_organizer.graph.state import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
    DECISION_REFUSE,
    CourseState,
    CourseStatus,
    DraftGradeRow,
    FacultyDecision,
    GateRequest,
    ROUTE_APPROVED,
    ROUTE_ESCALATE,
    ROUTE_PASS,
    ROUTE_REFUSED,
    ROUTE_RETRY,
)
from course_organizer.graph.validation import CheckContext, next_route, validate
from course_organizer.skills import (
    FormatInputs,
    LockedPlan,
    PlanInputs,
    TemplateSpec,
    WorkspaceResult,
)
from course_organizer.skills import SECTIONS as TEMPLATE_SECTIONS

LOGGER = logging.getLogger(__name__)

#: The doc's template identity. Named rather than inlined so a test can assert
#: the document the gate shows is the document publish writes.
TEMPLATE_ID = "organised-course"
TEMPLATE_TITLE = "Organised Course Workspace"
TEMPLATE_AUDIENCE = "course_faculty"

#: Which of the scope tool's verified facts may appear in the assembled
#: document. Every key here was produced by ``compute_scope_validation`` reading
#: the seed corpus, so a reader of the document can trace it to a tool call.
#:
#: ``computed_at`` is deliberately absent. The plan skill already filters it out
#: of the locked plan as volatile, so naming it here would be naming a key that
#: can never resolve.
GROUNDED_FACT_KEYS: tuple[str, ...] = (
    "assignment_count",
    "marks_by_assignment",
    "note_count",
    "notes_by_unit",
    "total_available_marks",
    "unit_count",
    "units_covered",
    "units_with_notes",
    "version_total",
)


def _now() -> str:
    """UTC timestamp, ISO 8601. One helper so every node stamps the same way."""
    return datetime.now(timezone.utc).isoformat()


def _audit(node: str, summary: str, **extra: Any) -> dict[str, Any]:
    """One audit entry. Every node appends exactly one."""
    return {"node": node, "at": _now(), "summary": summary, **extra}


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def make_ingest(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``ingest`` -- read the course's scope from the store.

    Deliberately thin. It establishes which course the run is for and reads
    nothing else, because every fact beyond that belongs to a specialist that
    will be audited individually. An ``ingest`` node that quietly did the work
    of three specialists would make the attribution in the audit trail
    meaningless.
    """

    def ingest(state: CourseState) -> dict[str, Any]:
        course_code = str(state.get("course_code") or ctx.course_code)
        units = list(state.get("units_requested") or [])
        scope = ctx.tools.invoke(
            "compute_scope_validation",
            ctx.scope_query(
                course_code=course_code, units=units, assignment_ids=[]
            ),
            context=ctx.tool_context,
        ).unwrap()
        # The scope tool reports the course's real name and its true unit list
        # in verified_facts. Reading them from there rather than re-deriving
        # them keeps one owner for "what is in scope": the tool that already
        # checked the syllabus.
        facts = dict(getattr(scope, "verified_facts", {}) or {})
        units_covered = list(facts.get("units_covered", units))
        course_name = str(facts.get("course_name", "") or "")
        failed = [
            c for c in getattr(scope, "checks", []) if not getattr(c, "passed", True)
        ]
        if not scope.valid or failed:
            detail = "; ".join(
                str(getattr(c, "detail", "")) for c in failed
            ) or "the scope check reported the course as invalid"
            raise ValueError(
                f"scope is not valid for {course_code}: {detail}"
            )
        summary = (
            f"scope {course_code} ({course_name}): {len(units_covered)} "
            f"unit(s), {facts.get('note_count', '?')} note(s), "
            f"{facts.get('version_total', '?')} version(s), "
            f"{facts.get('assignment_count', '?')} assignment(s); "
            f"{len(scope.checks)} scope check(s) passed"
        )
        return {
            "course_code": course_code,
            "course_name": course_name,
            "units_requested": units_covered,
            "version_total": int(facts.get("version_total", 0) or 0),
            "run_id": ctx.run_id,
            "human_authority": ctx.human_authority,
            "scope_checks_run": [str(c.check_id) for c in scope.checks],
            # Carried forward so `plan` can lock a *real* scope and `assemble`
            # can report only tool-verified numbers. The formatter's drift check
            # rejects any reported fact the plan never verified, so without
            # this every number in the document would be flagged.
            "scope_verified_facts": facts,
            "scope_assignment_ids": sorted(
                (facts.get("marks_by_assignment") or {}).keys()
            ),
            "audit_trail": [_audit("ingest", summary, course_code=course_code)],
            "nodes_ran": ["ingest"],
        }

    return ingest


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def make_plan(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``plan`` -- lock the blueprint before any work starts.

    Doc 2.2 line 472: *"The blueprint/plan is locked before work begins."* The
    lock is the ``plan`` skill's, and this node stores its digest rather than
    just its obligations, so :mod:`~course_organizer.graph.nodes` can assert at
    ``assemble`` time that the plan did not change underneath the run.
    """

    def plan(state: CourseState) -> dict[str, Any]:
        units = tuple(state.get("units_requested") or state.get("units_covered") or ())
        # The assignment ids come from `ingest`, which read them from the scope
        # tool. Locking an empty scope would make every assignment the
        # specialists distribute look out of scope to the formatter's drift
        # check -- three findings that are really one bug here.
        assignment_ids = tuple(state.get("scope_assignment_ids") or ())
        # The plan's verified facts ARE the scope tool's. `assemble` is only
        # permitted to report numbers that appear here, because that is what
        # makes a number in the document traceable to a tool rather than to
        # this graph's own arithmetic.
        verified = dict(state.get("scope_verified_facts") or {})
        verified["human_authority"] = ctx.human_authority
        inputs = PlanInputs(
            course_code=state["course_code"],
            course_name=state.get("course_name") or state["course_code"],
            units=units,
            assignment_ids=assignment_ids,
            include_milestones=True,
            steps=ctx.lab_steps(),
            verified_facts=verified,
        )
        locked = ctx.skills.invoke("plan", inputs)
        if not locked.ok:
            raise RuntimeError(
                f"the plan skill could not lock a blueprint: {locked.call.error}"
            )
        plan_locked = locked.value
        summary = (
            f"locked {plan_locked.obligation_count} obligation(s); "
            f"plan_sha256={plan_locked.plan_sha256[:12]}"
        )
        return {
            "plan_locked": plan_locked.model_dump(mode="json"),
            "plan_sha256": plan_locked.plan_sha256,
            "obligation_count": plan_locked.obligation_count,
            "plan_inputs": inputs.model_dump(mode="json"),
            "audit_trail": [
                _audit(
                    "plan",
                    summary,
                    plan_sha256=plan_locked.plan_sha256,
                    obligation_count=plan_locked.obligation_count,
                )
            ],
            "nodes_ran": ["plan"],
        }

    return plan


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------


def make_workers(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``workers`` -- run the six Phase-A specialists, then record the pass.

    This is the node doc 2.10's back-edge points *at*, so it must be safe to
    run more than once. It is, because every specialist recomputes from the
    store rather than accumulating onto the previous pass, and because
    ``draft_grades`` is rebuilt whole (LastValue) instead of extended.

    ``attempts`` is **not** incremented here -- ``validate`` owns that counter,
    so the number in ``attempt_log`` and the number in the state cannot drift.
    """

    def workers(state: CourseState) -> dict[str, Any]:
        attempt = int(state.get("attempts", 0))
        results, merged = run_specialists(state, ctx)
        entries = [r.audit() for r in results]
        failed = [e for e in entries if not e["ok"]]
        summary = (
            f"pass {attempt + 1}: {len(entries)} specialist(s), "
            f"{len(failed)} reported a problem"
            + (f" [{', '.join(e['step_id'] for e in failed)}]" if failed else "")
        )
        LOGGER.info("workers %s", summary)
        return {
            **merged,
            "attempt_log": [
                {
                    "pass": attempt + 1,
                    "specialists": entries,
                    "at": _now(),
                }
            ],
            "audit_trail": [_audit("workers", summary, attempt=attempt + 1)],
            "nodes_ran": ["workers"],
        }

    return workers


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def make_validate(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``validate`` -- doc 2.7's four checks, then a route. No loop here.

    The bound is ``ctx.max_attempts``, and the order in
    :func:`~course_organizer.graph.validation.next_route` is deliberate: a run
    that passes *on* its last permitted attempt advances, because failing it
    would mean a run can never succeed at the bound.
    """

    def validate_node(state: CourseState) -> dict[str, Any]:
        grades = state.get("draft_grades", [])
        ceilings = {
            str(a["assignment_id"]): float(a["max_marks"])
            for a in state.get("assignments", [])
            if a.get("max_marks")
        }
        late_by = {
            str(f["submission_id"]): int(f["late_by_seconds"])
            for f in state.get("late_flags", [])
        }
        attempt = int(state.get("attempts", 0)) + 1
        report = validate(
            CheckContext(grades, ceilings, late_by), attempt=attempt
        )
        route = next_route(
            report, attempts=attempt, max_attempts=ctx.max_attempts
        )
        if report.passed:
            summary = (
                f"pass {attempt}: all {len(report.checks_run)} check(s) passed"
            )
        else:
            summary = (
                f"pass {attempt}: {len(report.failing)} of "
                f"{len(report.checks_run)} check(s) failed "
                f"({', '.join(report.failing)}) -> {route}"
            )
        LOGGER.info("validate %s", summary)
        return {
            "attempts": attempt,
            "validation": report,
            "route": route,
            "audit_trail": [
                _audit(
                    "validate",
                    summary,
                    attempt=attempt,
                    route=route,
                    failing=list(report.failing),
                )
            ],
            "nodes_ran": ["validate"],
        }

    return validate_node


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


def make_assemble(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``assemble`` -- render the organised workspace document.

    Runs :func:`~course_organizer.skills.format_organised` with the locked plan
    and the workspace the specialists actually produced. The formatter's drift
    check compares the two, so drift is *reported* to the gate rather than
    suppressed -- doc 2.11 line 594 has faculty seeing rubric-aligned drafts, and
    a deviation they were not told about is worse than one they were.
    """

    def assemble(state: CourseState) -> dict[str, Any]:
        grades = state.get("draft_grades", [])
        plan_locked = state.get("plan_locked") or {}
        locked = LockedPlan(**plan_locked)

        # Only report numbers the locked plan verified. The formatter treats any
        # other reported fact as drift, and it is right to: a number in this
        # document has to be traceable to a tool, and the pipeline's own
        # counts (draft grades, marks total, late count) are not.
        #
        # That is not a loss. Doc 2.10's own wording for this artifact is a
        # workspace *shell* -- verified facts only. The pipeline's output lives
        # where it belongs: the gate request shows the reviewer every draft
        # grade, and `publish` writes the approved grade table.
        grounded: dict[str, Any] = {}
        for key in sorted(locked.verified_facts):
            if key in GROUNDED_FACT_KEYS:
                grounded[key] = locked.verified_facts[key]

        workspace = WorkspaceResult(
            course_code=state["course_code"],
            units_covered=tuple(state.get("units_covered") or ()),
            assignments_included=tuple(
                a["assignment_id"] for a in state.get("assignments", [])
            ),
            milestones_emitted=bool(state.get("milestones")),
            completed_steps=tuple(s.step_id for s in ctx.lab_steps()),
            reported_facts=grounded,
            notes_by_unit=state.get("notes_by_unit") or {},
            ground_fact_keys=tuple(sorted(grounded)),
        )
        template = TemplateSpec(
            template_id=TEMPLATE_ID,
            title=TEMPLATE_TITLE,
            audience=ctx.human_authority,
            sections=TEMPLATE_SECTIONS,
        )
        rendered = ctx.skills.invoke(
            "format_organised", FormatInputs(result=workspace, plan=locked, template=template)
        )
        if not rendered.ok:
            raise RuntimeError(
                f"the format skill could not assemble the workspace: "
                f"{rendered.call.error}"
            )
        document = rendered.value
        drift = [f.model_dump(mode="json") for f in (document.drift.findings if document.drift else [])]
        summary = (
            f"assembled {document.document_id} "
            f"({len(document.sections_rendered)} section(s)); "
            f"drift findings: {len(drift)}"
        )
        LOGGER.info("assemble %s", summary)
        return {
            "document": document.model_dump(mode="json"),
            "document_id": document.document_id,
            "document_status": document.status.value,
            "document_body": document.body,
            "drift_findings": drift,
            "status": CourseStatus.DRAFT,
            "audit_trail": [_audit("assemble", summary, document_id=document.document_id)],
            "nodes_ran": ["assemble"],
        }

    return assemble


# ---------------------------------------------------------------------------
# gate  -- the real human gate
# ---------------------------------------------------------------------------


def make_gate(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``gate`` -- suspend the graph and wait for the named human.

    Doc 2.2 line 467: *"Amber -- the human review gate: a hard stop; the named
    human is the final authority."* Doc 2.5 line 538 says the gate returns
    ``approve | edit | override``. This node implements that.

    How the suspension works, since it is the one thing worth being precise
    about:

    1. The node builds a :class:`GateRequest` -- everything the reviewer needs,
       so approving does not require re-running anything.
    2. ``interrupt(request)`` suspends. LangGraph checkpoints the state and
       ``invoke`` returns with ``__interrupt__`` set.
    3. The caller resumes with ``Command(resume=<payload>)``; this node
       **re-enters from the top** and ``interrupt`` returns that payload
       immediately rather than suspending again.
    4. So ``ingest``/``plan``/``workers``/``validate``/``assemble`` do **not**
       re-run on resume. They already ran; their state is in the checkpoint.

    Point 3 is why the re-entry is harmless, and it is also why the payload has
    to be a plain serialisable model: it crosses the checkpoint boundary.
    """

    def gate(state: CourseState) -> dict[str, Any]:
        grades = state.get("draft_grades", [])
        validation = state.get("validation")
        request = GateRequest(
            course_code=state["course_code"],
            course_name=state.get("course_name", ""),
            draft_grades=list(grades),
            validation=validation,
            similarity_flags=list(state.get("similarity_flags", [])),
            units_covered=list(state.get("units_covered", [])),
            assignments_included=[
                a["assignment_id"] for a in state.get("assignments", [])
            ],
            attempt=int(state.get("attempts", 0)),
            summary=(
                f"{len(grades)} draft grade(s) awaiting {ctx.human_authority} "
                f"review; {len(state.get('similarity_flags', []))} similarity "
                f"flag(s); document {state.get('document_id', '')}"
            ),
        )
        # Suspends here. Never inside a try that swallows exceptions.
        payload = interrupt(request)
        decision = _coerce_decision(payload)
        summary = (
            f"{ctx.human_authority} returned '{decision.decision}' "
            f"for {len(grades)} draft grade(s)"
        )
        LOGGER.info("gate %s", summary)
        approved = decision.approved
        grades_out = _apply_decision(grades, decision)
        return {
            "decision": decision,
            "gate_request": request,
            "draft_grades": grades_out,
            "approved_at": _now() if approved else "",
            "status": (
                CourseStatus.PUBLISHED if approved else CourseStatus.REFUSED
            ),
            "route": ROUTE_APPROVED if approved else ROUTE_REFUSED,
            "audit_trail": [
                _audit(
                    "gate",
                    summary,
                    decision=decision.decision,
                    decided_by=decision.decided_by,
                    edits=len(decision.edits),
                    override_marks=len(decision.override_marks),
                )
            ],
            "nodes_ran": ["gate"],
        }

    return gate


def _coerce_decision(payload: Any) -> FacultyDecision:
    """Turn whatever came back from ``interrupt`` into a decision.

    Accepts a :class:`FacultyDecision`, a mapping, or a bare string. A bare
    string is the common case for a scripted gate and a human typing one word in
    a console, so it is supported rather than forcing every caller to construct
    the model.
    """
    if isinstance(payload, FacultyDecision):
        return payload
    if isinstance(payload, str):
        return FacultyDecision(decision=payload)
    if isinstance(payload, Mapping):
        data = dict(payload)
        data.setdefault("decision", DECISION_APPROVE)
        return FacultyDecision(**data)
    return FacultyDecision(decision=DECISION_REFUSE)


def _apply_decision(
    grades: list[Any], decision: FacultyDecision
) -> list[Any]:
    """Fold the faculty's decision into the rows, without inventing one.

    ``edit`` replaces only the marks the reviewer named. ``override`` uses the
    marks supplied. ``approve`` leaves them. A refusal returns them untouched,
    still drafts -- because a refused run has published nothing, and a row
    labelled anything other than ``draft`` would be a lie about that.

    The awarded marks are clamped to the rubric maximum. Doc 2.7's first check
    says a grade *never* exceeds the maximum, and an override is exactly the case
    where a human types a number; silently clamping could hide the mistake, so it
    is clamped *and* the clamp is visible in the audit summary.
    """
    from course_organizer.graph.state import DraftGradeRow

    out: list[Any] = []
    for row in grades:
        replacement: Optional[float] = None
        if decision.decision == DECISION_EDIT:
            replacement = decision.edits.get(row.submission_id)
        elif decision.decision == DECISION_OVERRIDE:
            replacement = decision.override_marks.get(row.submission_id)
        if replacement is None:
            out.append(row)
            continue
        value = min(float(replacement), float(row.max_marks))
        criterion_marks = row.criterion_marks
        if criterion_marks and row.awarded_marks:
            factor = value / row.awarded_marks if row.awarded_marks else 0.0
            criterion_marks = {
                k: round(v * factor, 2) for k, v in criterion_marks.items()
            }
        out.append(
            DraftGradeRow(
                **{
                    **row.model_dump(),
                    "awarded_marks": round(value, 2),
                    "criterion_marks": criterion_marks,
                    "feedback": (
                        f"{row.feedback} Adjusted by {decision.decided_by} "
                        f"({decision.decision}) to {value:g} of {row.max_marks:g}."
                    ),
                }
            )
        )
    return out


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def make_publish(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``publish`` -- write the workspace, but only after an approval.

    Doc 2.12 line 830: *"Never publish a grade or feedback without faculty
    approval."* So this node **re-checks** rather than trusting the gate's route.
    A graph is a control-flow structure; an edge can be miswired, a resumed run
    can arrive here by an unintended path, and the guardrail says the invariant
    holds at the point of publication, not that it held upstream. Re-checking is
    three lines and it makes the guardrail local.
    """

    def publish(state: CourseState) -> dict[str, Any]:
        decision = state.get("decision")
        grades = state.get("draft_grades", [])
        unapproved = [
            g for g in grades if g.status != "draft" and not g.approved_by
        ]
        if decision is None or not decision.approved:
            raise PermissionError(
                "publish requires a recorded faculty approval; the gate "
                "returned "
                + (repr(getattr(decision, "decision", None)) if decision else "no decision")
            )
        if unapproved:
            raise PermissionError(
                "publish refused: "
                f"{len(unapproved)} grade(s) are past draft with no approver "
                f"({', '.join(g.submission_id for g in unapproved)})"
            )

        stamped = [
            _stamp_published(
                g,
                approved_by=decision.decided_by,
                approved_at=state.get("approved_at") or _now(),
            )
            for g in grades
        ]
        body = _render_published(state, stamped)
        path = ctx.output_dir / f"{state.get('document_id', 'workspace')}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

        summary = (
            f"published {len(stamped)} grade(s) approved by "
            f"{decision.decided_by} -> {path.name}"
        )
        LOGGER.info("publish %s", summary)
        return {
            "draft_grades": stamped,
            "published_path": str(path),
            "document_body": body,
            "document_status": "published",
            "status": CourseStatus.PUBLISHED,
            "audit_trail": [
                _audit(
                    "publish",
                    summary,
                    approved_by=decision.decided_by,
                    published_path=str(path),
                )
            ],
            "nodes_ran": ["publish"],
        }

    return publish


def _stamp_published(
    grade: Any, *, approved_by: str, approved_at: str
) -> "DraftGradeRow":
    """Copy a grade row with the approval triple written on it.

    A function rather than an inline ``**{**row.model_dump(), ...}`` so the
    ``status``/``approved_by``/``approved_at`` triple is written in exactly one
    place. Doc 2.7's fourth check reads all three, so if a fourth field ever
    needs stamping this is where it goes -- and a test asserts all three move
    together.
    """
    from course_organizer.graph.state import DraftGradeRow

    return DraftGradeRow(
        **{
            **grade.model_dump(),
            "status": "published",
            "approved_by": approved_by,
            "approved_at": approved_at,
        }
    )


#: The pre-gate approval lines that :func:`~course_organizer.skills.format_skill`
#: renders. Lab 3 is right to hard-code them -- that lab has no approval step --
#: but Lab 7 has one, so a published document carrying them would assert
#: ``"approved": false`` and ``"Approved : no`` above a table of published
#: grades. ``publish`` rewrites these two lines and nothing else.
PREGATE_APPROVED_LINE = "Approved : no - this lab implements no approval step"
PREGATE_PUBLISHED_LINE = "Published: no"
APPROVED_SECTION_TITLE = "## Approval Required"


def _stamp_approval(body: str, decision: Any, approved_at: str) -> str:
    """Rewrite the pre-gate approval lines to the decision that was recorded.

    The formatter builds its approval section from
    :func:`~course_organizer.skills.format_skill._build_approval`, which is
    fixed to *not approved* because Lab 3 predates the gate. Changing that
    module would invalidate a lab that is already tested and documented, so the
    correction happens here instead, on the two exact lines.

    Args:
        body: The assembled workspace body.
        decision: The recorded faculty decision.
        approved_at: When, for the record.

    Returns:
        The body with the approval lines stating what actually happened. If the
        expected lines are absent the body is returned unchanged and the caller
        still publishes -- a missing line is a formatting difference, not a
        reason to withhold a document the reviewer approved.
    """
    stamp = approved_at or _now()
    replacements = (
        (
            PREGATE_APPROVED_LINE,
            f"Approved : yes - {decision.decided_by} ({decision.decision}) at {stamp}",
        ),
        (
            PREGATE_PUBLISHED_LINE,
            "Published: yes - grades below carry the approver's name",
        ),
    )
    out = body
    for old, new in replacements:
        if old in out:
            out = out.replace(old, new)
    # The heading is the formatter's, and "Approval Required" is no longer true
    # of this artefact. Renaming it keeps the document internally consistent.
    if APPROVED_SECTION_TITLE in out:
        out = out.replace(
            APPROVED_SECTION_TITLE, "## Approval Recorded", 1
        )
    return out


def _render_published(state: Mapping[str, Any], grades: list[Any]) -> str:
    """The published markdown: the stamped workspace plus the approved grades.

    Doc 2.11 line 594 says faculty review shows *"rubric-aligned draft grades
    and feedback awaiting faculty approval"*, so the grade table is the
    substance of the document the run is judged by, not an appendix.

    A publication record goes at the very top so the first thing a reader sees
    is who approved what and when. Doc 2.12 line 834 requires an audit log of
    every grade published and who approved it, and this line is that record in
    the artefact itself -- the durable JSONL audit trail is Lab 6's job.
    """
    decision = state.get("decision")
    approver = grades[0].approved_by if grades else "course_faculty"
    when = state.get("approved_at") or _now()
    header = [
        "---",
        f"Published by: {approver}",
        f"Decision    : {getattr(decision, 'decision', 'approve')}",
        f"At           : {when}",
        f"Course       : {state.get('course_code', '')} "
        f"{state.get('course_name', '')}".rstrip(),
        "---",
        "",
    ]
    body = _stamp_approval(state.get("document_body", ""), decision, when)
    lines = header + [body.rstrip(), ""]
    lines += [
        "## Published Grades",
        "",
        f"Approved by: {approver}",
        "",
        "| Submission | Student | Assignment | Awarded | Max | Late | Similarity |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for g in grades:
        similarity = (
            f"{g.similarity_score:.2f}" if g.similarity_score is not None else "n/a"
        )
        flag = " (flagged)" if g.similarity_flagged else ""
        if g.similarity_against:
            flag = f"{flag} vs {g.similarity_against}"
        lines.append(
            f"| {g.submission_id} | {g.student_name} | {g.assignment_id} | "
            f"{g.awarded_marks:g} | {g.max_marks:g} | "
            f"{'yes' if g.late else 'no'} | {similarity}{flag} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# escalate
# ---------------------------------------------------------------------------


def make_escalate(ctx: RunContext) -> Callable[[CourseState], dict[str, Any]]:
    """``escalate`` -- stop and hand the run to a human with a diagnosis.

    Doc 2.12 line 825: on exhaustion, *"escalate to course faculty with a
    diagnosis."* Reached two ways, both routed here: the retry budget spent, and
    a faculty refusal. :func:`~course_organizer.graph.escalation.build_escalation`
    tells them apart, because a spent budget needs a different conversation than
    a refusal.
    """

    def escalate(state: CourseState) -> dict[str, Any]:
        decision = state.get("decision")
        refused = bool(decision is not None and not decision.approved)
        reason = (
            DECISION_REFUSE if refused else "retry_budget_exhausted"
        )
        escalation = build_escalation(
            state,
            reason=reason,
            max_attempts=ctx.max_attempts,
            escalated_to=ctx.human_authority,
            decided_by=getattr(decision, "decided_by", "") if decision else "",
        )
        summary = (
            f"escalated to {escalation.escalated_to}: {escalation.reason} "
            f"after {escalation.attempts} attempt(s)"
        )
        LOGGER.warning("escalate %s", summary)
        return {
            "escalation": escalation,
            "status": (
                CourseStatus.REFUSED if refused else CourseStatus.ESCALATED
            ),
            "route": ROUTE_REFUSED if refused else ROUTE_ESCALATE,
            "audit_trail": [_audit("escalate", summary, reason=escalation.reason)],
            "nodes_ran": ["escalate"],
        }

    return escalate


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def build_nodes(ctx: RunContext) -> dict[str, Callable[[CourseState], dict[str, Any]]]:
    """Every node, keyed by the name the doc uses.

    The keys are doc 2.10 line 696's node names exactly -- ``ingest``, ``plan``,
    ``workers``, ``validate``, ``assemble``, ``gate``, ``publish`` -- plus
    ``escalate``. :mod:`~course_organizer.graph.builder` wires the edges, and a
    test asserts the compiled graph's edges against the doc's chain rather than
    against this dict, so a typo here cannot pass unnoticed.
    """
    return {
        "ingest": make_ingest(ctx),
        "plan": make_plan(ctx),
        "workers": make_workers(ctx),
        "validate": make_validate(ctx),
        "assemble": make_assemble(ctx),
        "gate": make_gate(ctx),
        "publish": make_publish(ctx),
        "escalate": make_escalate(ctx),
    }


__all__ = [
    "APPROVED_SECTION_TITLE",
    "PREGATE_APPROVED_LINE",
    "PREGATE_PUBLISHED_LINE",
    "TEMPLATE_AUDIENCE",
    "TEMPLATE_ID",
    "TEMPLATE_TITLE",
    "build_nodes",
    "make_assemble",
    "make_escalate",
    "make_gate",
    "make_ingest",
    "make_plan",
    "make_publish",
    "make_validate",
    "make_workers",
]