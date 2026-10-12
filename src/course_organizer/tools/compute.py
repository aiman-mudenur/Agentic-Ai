"""The domain calculator/checker -- `v = tool.compute(a, b); assert v.valid`.

Document 2.10, Lab 2, asks for "a domain calculator/checker, so the planner acts
on real data". Its pseudocode is::

    a = tool.read('the notes/materials store')
    b = tool.read('assignment & rubric definitions')
    v = tool.compute(a, b)
    assert v.valid

The last line is the interesting one. `assert v.valid` means the planner must
be able to *check its own inputs before acting on them*. So this tool is a
composite: it calls the two readers through the registry, then computes real
arithmetic over what came back.

Two properties make it worth having:

**Nested calls stay on the ledger.** `compute_scope_validation` reaches the
store only by calling `read_notes_store` and `read_assignment_rubrics`, so the
ledger records three calls, two at depth 1. A composite tool therefore cannot
hide a data access. That is enforced by `ToolContext`, which carries a registry
and no filesystem handle.

**A validator that always says "valid" proves nothing.** The seeded corpus is
deliberately valid so the document's `assert v.valid` holds, and the test suite
and notebook both corrupt it in memory to show the checker actually catches
things. A checker with no failing case is untested code.

The six checks are all arithmetic or referential facts, each traceable to a
field the document defines:

| check_id | What it verifies | Doc basis |
|----------|------------------|-----------|
| `units_exist` | every in-scope unit is declared by the course | 2.6 notes tagged by unit |
| `notes_coverage` | every in-scope unit has at least one note | 2.3 Notes Repository |
| `version_integrity` | version numbers contiguous from 1, across all notes | 2.12 "keep clear version history" |
| `assignments_exist` | every in-scope assignment id exists | 2.6 assignment definitions |
| `rubric_arithmetic` | criterion maxima sum to `max_marks`; weights sum to 1.0 | 2.7 check 0 |
| `schedule_sane` | `release_date <= due_date` | 2.6 due date |
| `faculty_approved` | every definition is approved by course faculty | 2.12 "never ... without faculty approval" |
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.schemas import _Base
from course_organizer.tools.base import ToolContext, ToolError, ToolRegistry

logger = logging.getLogger(__name__)

DOC_REF = "2.10 Lab 2"


# ==========================================================================
# Models
# ==========================================================================


class ScopeQuery(_Base):
    """Arguments for `compute_scope_validation`.

    Mirrors the fields of Lab 1's `ClarifiedRequest`, because this tool's job
    is to check that the clarified scope is actually satisfiable by the data.

    Attributes:
        course_code: The course to validate.
        units: In-scope units. Empty means "every declared unit".
        assignment_ids: In-scope assignments. Empty means "no assignment work".
    """

    course_code: str = Field(min_length=1, examples=["CS201"])
    units: list[str] = Field(default_factory=list, examples=[["1", "2", "3"]])
    assignment_ids: list[str] = Field(default_factory=list)


class ValidationCheck(_Base):
    """One verified arithmetic or referential fact.

    Attributes:
        check_id: Stable identifier, so a failure is quotable in a report.
        title: Human-readable name.
        passed: Whether the check holds.
        detail: What was found, in words.
        evidence: The computed numbers behind the verdict. Kept as data so a
            reviewer can re-check the arithmetic without re-running the agent.
    """

    check_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    passed: bool
    detail: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ScopeValidationResult(_Base):
    """Verified output of the compute step. This is `v` in the pseudocode.

    Attributes:
        course_code: Echo of the query.
        units: The units this result was computed over. Empty means every
            declared unit.
        assignment_ids: The assignments this result was computed over. Empty
            means every definition, or none at all if `units` was also empty.
        valid: True only if every check passed. `assert v.valid` reads this.
        checks: Each check, in a stable order.
        verified_facts: The tool-computed numbers a prompt might otherwise
            assert. This mapping is the heart of the pitfall defence: every
            key here is a value that came from the store, never from a caller.
            **Read these against `course_wide`.** A result computed over a
            subset reports subset totals, which are correct for that subset and
            wrong for the course as a whole.
        notes_note_count: Notes read, carried through for traceability.
        assignments_found: Assignment ids found.
        source_tools: Which tools contributed, with their call ids, so the
            result is self-documenting about its own provenance.
    """

    course_code: str
    units: list[str] = Field(default_factory=list)
    assignment_ids: list[str] = Field(default_factory=list)
    valid: bool
    checks: list[ValidationCheck] = Field(default_factory=list)
    verified_facts: dict[str, Any] = Field(default_factory=dict)
    notes_note_count: int = 0
    assignments_found: list[str] = Field(default_factory=list)
    source_tools: list[str] = Field(default_factory=list)

    @property
    def course_wide(self) -> bool:
        """True when nothing was narrowed, so the facts describe the course.

        A result with `units=['1', '2']` is not course-wide: its
        `unit_count` and `total_available_marks` describe that subset. Callers
        that intend to check a course-wide claim must check this first.
        """
        return not self.units and not self.assignment_ids

    def scope_label(self) -> str:
        """A short human label for this result's scope, for reports."""
        if self.course_wide:
            return "course_wide"
        parts = []
        if self.units:
            parts.append(f"units {','.join(self.units)}")
        if self.assignment_ids:
            parts.append(f"assignments {','.join(self.assignment_ids)}")
        return "scoped(" + "; ".join(parts) + ")"

    def failed(self) -> list[ValidationCheck]:
        """Every failing check, for a diagnosis-oriented report."""
        return [check for check in self.checks if not check.passed]

    def check(self, check_id: str) -> ValidationCheck:
        """Look up one check by id.

        Raises:
            KeyError: If no such check ran, naming the ids that did.
        """
        for check in self.checks:
            if check.check_id == check_id:
                return check
        raise KeyError(
            f"no check {check_id!r}; ran {[c.check_id for c in self.checks]}"
        )

    def fact(self, name: str) -> Any:
        """Read one verified fact by name.

        Raises:
            KeyError: If the fact was not computed.
        """
        if name not in self.verified_facts:
            raise KeyError(
                f"no verified fact {name!r}; computed {sorted(self.verified_facts)}"
            )
        return self.verified_facts[name]


# ==========================================================================
# The compute tool
# ==========================================================================


def register_compute_scope_validation(registry: ToolRegistry) -> ToolRegistry:
    """Register `compute_scope_validation` on a registry.

    Args:
        registry: The registry to add to. Must already carry the two readers,
            since this tool calls them.

    Returns:
        The same registry, for chaining.

    Raises:
        ToolError: If the readers are not registered yet.
    """

    @registry.tool(
        "compute_scope_validation",
        summary=(
            "Compute and validate the clarified scope against real data. Reads "
            "the notes store and the assignment/rubric definitions, then checks "
            "unit coverage, version integrity, rubric arithmetic, the release "
            "and due date ordering, and faculty approval. Call this before "
            "acting on any scope: it is the gate that stops the planner from "
            "working from assumptions."
        ),
        input_model=ScopeQuery,
        output_model=ScopeValidationResult,
        doc_ref=f"{DOC_REF} pseudocode v = tool.compute(a, b); assert v.valid",
    )
    def compute_scope_validation(
        query: ScopeQuery, ctx: ToolContext
    ) -> ScopeValidationResult:
        """Compute verified values and check the scope is satisfiable.

        This tool never touches the store itself. It calls `read_notes_store`
        and `read_assignment_rubrics` through the registry, so both nested
        calls appear on the ledger at depth 1.

        Args:
            query: The scope to validate.
            ctx: Tool context, used for the nested reads.

        Returns:
            A `ScopeValidationResult` whose `valid` field is what the
            document's `assert v.valid` reads.

        Raises:
            ToolError: If a nested read fails, or the course is unknown.
        """
        units = list(query.units)

        # -- a = tool.read('the notes/materials store') --------------------
        # Units are validated against the declared set first, so a request for
        # a unit that does not exist is reported as a finding rather than
        # silently returning an empty read.
        notes_result = ctx.call(
            "read_notes_store",
            {
                "course_code": query.course_code,
                "units": units,
                "current_only": True,
            },
        )
        notes = notes_result.value

        # -- b = tool.read('assignment & rubric definitions') --------------
        assignment_result = ctx.call(
            "read_assignment_rubrics",
            {
                "course_code": query.course_code,
                "assignment_ids": list(query.assignment_ids),
            },
        )
        assignments = assignment_result.value

        if notes is None or assignments is None:  # pragma: no cover - ctx.call raises
            raise ToolError("nested reader returned no value")

        declared = set(notes.units_declared)
        requested_units = units or sorted(declared, key=lambda u: (len(u), u))
        unknown_units = [u for u in requested_units if u not in declared]
        covered = set(notes.units_covered)
        uncovered = [u for u in requested_units if u not in covered]

        checks: list[ValidationCheck] = []

        # -- check 1: units_exist ------------------------------------------
        checks.append(
            ValidationCheck(
                check_id="units_exist",
                title="Every in-scope unit is declared by the course",
                passed=not unknown_units,
                detail=(
                    f"all {len(requested_units)} requested unit(s) are declared"
                    if not unknown_units
                    else f"{len(unknown_units)} requested unit(s) do not exist: "
                    f"{', '.join(unknown_units)}. Declared: "
                    f"{', '.join(notes.units_declared) or '(none)'}"
                ),
                evidence={
                    "requested_units": requested_units,
                    "declared_units": notes.units_declared,
                    "unknown_units": unknown_units,
                },
            )
        )

        # -- check 2: notes_coverage ---------------------------------------
        checks.append(
            ValidationCheck(
                check_id="notes_coverage",
                title="Every in-scope unit has notes in the store",
                passed=not uncovered,
                detail=(
                    f"{len(covered)} of {len(requested_units)} requested unit(s) "
                    f"have notes; {notes.note_count} note(s) matched"
                    if not uncovered
                    else f"{len(uncovered)} unit(s) have no notes: "
                    f"{', '.join(uncovered)}"
                ),
                evidence={
                    "requested_units": requested_units,
                    "covered_units": sorted(covered, key=lambda u: (len(u), u)),
                    "uncovered_units": uncovered,
                    "note_count": notes.note_count,
                    "unit_coverage": notes.unit_coverage(),
                },
            )
        )

        # -- check 3: version_integrity ------------------------------------
        # `NoteMaterial` already rejects duplicate and non-contiguous version
        # numbers at construction, so this re-verifies across the whole store
        # and reports the totals, catching a corpus assembled from several
        # files where each file is individually valid.
        version_notes = [n for n in notes.notes if len(n.versions) > 1]
        gaps = [
            n.note_id
            for n in notes.notes
            if sorted(v.version for v in n.versions) != list(range(1, len(n.versions) + 1))
        ]
        checks.append(
            ValidationCheck(
                check_id="version_integrity",
                title="Note version history is contiguous from 1 with no gaps",
                passed=not gaps,
                detail=(
                    f"{notes.version_total} version(s) across {notes.note_count} "
                    f"note(s); {len(version_notes)} note(s) carry history. "
                    "History retained, no version removed."
                    if not gaps
                    else f"version numbering is not contiguous for: {', '.join(gaps)}"
                ),
                evidence={
                    "version_total": notes.version_total,
                    "note_count": notes.note_count,
                    "notes_with_history": [n.note_id for n in version_notes],
                    "inconsistent": gaps,
                },
            )
        )

        # -- check 4: assignments_exist ------------------------------------
        # Only meaningful when assignments are in scope; an out-of-scope
        # assignment stream must not fail the whole validation.
        requested_assignments = list(query.assignment_ids)
        assignments_in_scope = bool(requested_assignments)
        missing_assignments = list(assignments.missing_ids)
        checks.append(
            ValidationCheck(
                check_id="assignments_exist",
                title="Every in-scope assignment has a definition",
                passed=not missing_assignments,
                detail=(
                    "no assignments in scope; this check is vacuously satisfied"
                    if not assignments_in_scope
                    else (
                        f"all {len(requested_assignments)} requested assignment(s) "
                        f"resolved to definitions"
                        if not missing_assignments
                        else f"{len(missing_assignments)} assignment(s) not found: "
                        f"{', '.join(missing_assignments)}"
                    )
                ),
                evidence={
                    "requested": requested_assignments,
                    "found": assignments.found_ids,
                    "missing": missing_assignments,
                    "in_scope": assignments_in_scope,
                },
            )
        )

        # -- check 5: rubric_arithmetic ------------------------------------
        # This is doc 2.7 check 0's prerequisite: the cap on a draft grade is
        # meaningless if the rubric itself does not add up to max_marks.
        rubric_problems = list(assignments.rubric_problems)
        checks.append(
            ValidationCheck(
                check_id="rubric_arithmetic",
                title="Rubric criteria sum to max_marks and weights sum to 1.0",
                passed=not rubric_problems,
                detail=(
                    f"{assignments.criterion_count()} criterion/criteria across "
                    f"{len(assignments.assignments)} assignment(s) add up correctly"
                    if not rubric_problems
                    else "; ".join(rubric_problems)
                ),
                evidence={
                    "problems": rubric_problems,
                    "marks_by_assignment": assignments.marks_by_assignment,
                    "criterion_count": assignments.criterion_count(),
                },
            )
        )

        # -- check 6: schedule_sane ----------------------------------------
        out_of_order = [
            a.assignment_id
            for a in assignments.assignments
            if a.release_date is not None and a.release_date > a.due_date
        ]
        checks.append(
            ValidationCheck(
                check_id="schedule_sane",
                title="Every assignment is released on or before it is due",
                passed=not out_of_order,
                detail=(
                    "every assignment's release_date <= due_date"
                    if not out_of_order
                    else f"released after the deadline: {', '.join(out_of_order)}"
                ),
                evidence={
                    "violations": out_of_order,
                    "schedule": {
                        a.assignment_id: {
                            "release": a.release_date.isoformat() if a.release_date else None,
                            "due": a.due_date.isoformat(),
                        }
                        for a in assignments.assignments
                    },
                },
            )
        )

        # -- check 7: faculty_approved -------------------------------------
        # Doc 2.12: never publish a grade or feedback without faculty approval.
        # An assignment is distributable only if faculty approved its brief and
        # rubric, so this is checked before any work is planned against it.
        unapproved = [
            a.assignment_id
            for a in assignments.assignments
            if a.approved_by != HUMAN_AUTHORITY
        ]
        checks.append(
            ValidationCheck(
                check_id="faculty_approved",
                title=f"Every assignment is approved by {HUMAN_AUTHORITY}",
                passed=not unapproved,
                detail=(
                    f"all {len(assignments.assignments)} definition(s) name "
                    f"{HUMAN_AUTHORITY} as approver"
                    if not unapproved
                    else f"approved by an agent or unknown party: "
                    f"{', '.join(unapproved)}"
                ),
                evidence={
                    "expected_approver": HUMAN_AUTHORITY,
                    "violations": unapproved,
                    "approvers": {
                        a.assignment_id: a.approved_by for a in assignments.assignments
                    },
                },
            )
        )

        # -- verified_facts -------------------------------------------------
        # Everything a prompt is most likely to get wrong, computed here.
        # `units_with_notes` stays a COUNT: Lab 2's claim patterns bind to it
        # as a number, and changing its type to a list would silently break
        # every test that asserts on it. The per-unit detail is a separate key
        # so both shapes are available without either breaking the other.
        verified_facts: dict[str, Any] = {
            "course_code": query.course_code,
            "course_name": notes.course_name,
            "unit_count": len(notes.units_declared),
            "units_with_notes": len(notes.units_covered),
            "units_covered": list(notes.units_covered),
            "notes_by_unit": notes.notes_by_unit(),
            "note_count": notes.note_count,
            "version_total": notes.version_total,
            "assignment_count": len(assignments.assignments),
            "total_available_marks": assignments.total_available_marks,
            "marks_by_assignment": assignments.marks_by_assignment,
            "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        return ScopeValidationResult(
            course_code=query.course_code,
            units=units,
            assignment_ids=list(query.assignment_ids),
            valid=all(check.passed for check in checks),
            checks=checks,
            verified_facts=verified_facts,
            notes_note_count=notes.note_count,
            assignments_found=assignments.found_ids,
            source_tools=["read_notes_store", "read_assignment_rubrics"],
        )

    return registry


def build_default_registry(run_id: str) -> ToolRegistry:
    """Build the Lab 2 registry: the two readers plus the checker.

    Args:
        run_id: Correlates tool calls with the logging run id and the ledger.

    Returns:
        A registry with all three Lab 2 tools registered.
    """
    from course_organizer.tools.readers import (
        register_read_assignment_rubrics,
        register_read_notes_store,
    )

    registry = ToolRegistry(run_id=run_id)
    register_read_notes_store(registry)
    register_read_assignment_rubrics(registry)
    register_compute_scope_validation(registry)
    return registry


def build_lab04_registry(run_id: str) -> ToolRegistry:
    """Build the registry Lab 4 needs: the three reads plus the checker.

    Lab 4 adds the third document read -- ``read('submission records')`` -- on
    top of Lab 2's three tools, because Lab 4's inputs are "the notes/materials
    store; history of prior assignment submissions; current draft".

    The memory layer obtains every archived record through this registry and
    never through the store directly, which is what keeps the seed directory
    reachable from exactly one module.

    Args:
        run_id: Correlates tool calls with the logging run id and the ledger.

    Returns:
        A registry with all four tools registered.
    """
    from course_organizer.tools.readers import (
        register_read_assignment_rubrics,
        register_read_notes_store,
        register_read_submission_records,
    )

    registry = ToolRegistry(run_id=run_id)
    register_read_notes_store(registry)
    register_read_assignment_rubrics(registry)
    register_read_submission_records(registry)
    register_compute_scope_validation(registry)
    return registry


__all__ = [
    "ScopeQuery",
    "ScopeValidationResult",
    "ValidationCheck",
    "build_default_registry",
    "register_compute_scope_validation",
]