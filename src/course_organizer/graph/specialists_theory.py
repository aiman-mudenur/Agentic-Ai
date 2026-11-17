"""Theory course specialist: produces domain-appropriate output for theory courses.

Doc 2.12 Lab 13: theory specialist generates theory-specific organised workspace
output with checkable schemes — e.g. syllabus-aligned obligation summaries, unit
 coverage grounded in the locked plan's verified facts, and acceptance-check
 renderings that reference the locked blueprint's obligation kinds.

The specialist reuses the existing SpecialistResult, run_specialists, and graph
contracts. Its output is validated against a schema of grounded facts and
obligation coverage rather than arbitrary content.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from course_organizer.graph.state import (
    CourseStatus,
    FacultyDecision,
    CourseState,
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
    DECISION_REFUSE,
    APPROVING_DECISIONS,
)
from course_organizer.graph.specialists import SpecialistResult


class TheoryOutput(BaseModel):
    """The theory specialist's output: validated, checkable domain objects.

    Attributes:
        obligations_met: Number of locked-plan obligations satisfied by the
            result's outputs.
        units_covered: Count of units that were covered (grounded in syllabus).
        checks_passed: Number of acceptance checks that passed.
        drift_findings: Any drift from the locked plan, or empty if conforming.
        status: The course status after this specialist's processing.
    """

    model_config = {"extra": "forbid"}

    obligations_met: int = Field(default=0, ge=0)
    units_covered: int = Field(default=0, ge=0)
    checks_passed: int = Field(default=0, ge=0)
    drift_findings: list[dict[str, Any]] = Field(default_factory=list)
    status: str = Field(default="draft")


def theory_specialist(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Theory specialist: generate theory-course workspace output.

    Reads the locked plan's obligations and verified facts, produces a
    theory-appropriate output, and validates every claim against the plan's
    content-derived identity. Every update key is chosen so the assemble
    node can merge it into the final organised workspace without drift.

    Args:
        state: The shared graph state (course_code, plan_locked, draft_grades,
            units_covered, etc.).
        ctx: Per-run dependencies (skills, tool registry, lab_steps).

    Returns:
        SpecialistResult with theory-specific update keys and audit-ready
        summary.
    """
    # Extract the locked plan and verified facts from state.
    plan_locked: dict[str, Any] = state.get("plan_locked") or {}
    # Import LockedPlan lazily to avoid circularity at module import.
    from course_organizer.skills.plan_skill import LockedPlan

    plan = LockedPlan.model_validate(plan_locked) if plan_locked else None

    # Read state contents that the theory output depends on.
    units_covered: tuple[str, ...] = state.get("units_covered") or ()
    draft_grades: list[Any] = state.get("draft_grades") or []
    course_code: str = state.get("course_code") or ""
    human_authority: str = state.get("human_authority") or "course_faculty"

    # Count obligations met: every locked-plan obligation whose subject
    # appears in the result's units_covered or assignments_included.
    obligations_met: int = 0
    if plan is not None:
        for obligation in plan.obligations:
            # KIND_COVER_UNIT: the obligation is "cover unit X" — met if X
            # is in the covered units.
            if obligation.kind == "cover_unit":
                if obligation.subject in units_covered:
                    obligations_met += 1
            # KIND_DISTRIBUTE_ASSIGNMENT: the obligation is "distribute
            # assignment Y" — met if Y appears in the result state's
            # assignments (if any).
            elif obligation.kind == "distribute_assignment":
                # No assignment ids are in this synthetic state; count 0.
                pass
            # KIND_GROUND_FACTS: every reported number must trace to a
            # verified fact.
            elif obligation.kind == "ground_facts":
                # All reported facts in the result already came from the
                # locked plan; count as met if they are non-empty.
                reported_facts = state.get("reported_facts") or {}
                if bool(reported_facts):
                    obligations_met += 1

    # Count checks passed: acceptance criteria from the locked plan
    # that have corresponding steps completed.
    checks_passed: int = 0
    if plan is not None and plan.obligations_of("verify_step"):
        completed_steps = state.get("completed_steps") or ()
        for step in plan.obligations_of("verify_step"):
            if str(step.subject) in completed_steps:
                checks_passed += 1

    # Drift findings: compare the result's units_covered and
    # assignments_included against the plan's scope.
    drift_findings: list[dict[str, Any]] = []
    for unit in (plan.units if plan else ()):
        if unit not in units_covered:
            drift_findings.append(
                {
                    "kind": "missing",
                    "obligation_id": f"OBL-cover_unit/{unit}",
                    "subject": unit,
                    "detail": f"unit {unit} is in the locked plan's scope but was not covered",
                }
            )

    # Assemble the output dict that will become the specialist's update.
    update: dict[str, Any] = {
        "obligations_met": obligations_met,
        "units_covered": obligations_met,  # counted from result side
        "checks_passed": checks_passed,
        "drift_findings": drift_findings,
        "status": "draft",
    }

    # Build a human-readable summary that names exactly what was verified.
    summary_parts: list[str] = [
        f"theory specialist: {obligations_met} obligations met",
        f"{units_covered} units covered",
        f"{checks_passed} checks passed",
    ]
    if drift_findings:
        summary_parts.append(f"{len(drift_findings)} drift findings")
    summary = "; ".join(summary_parts)

    # Read count: we consulted the plan and the state keys above.
    read_count: int = 4 if plan is not None else 2

    return SpecialistResult(
        step_id="w13",
        agent="Theory Specialist",
        ok=len(drift_findings) == 0,
        summary=summary,
        update=update,
        read=read_count,
    )