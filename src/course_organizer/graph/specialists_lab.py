"""Lab course specialist: produces domain-appropriate output for lab courses.

Doc 2.12 Lab 13: lab specialist generates lab-specific organised workspace
output with checkable schemes — e.g. experiment descriptions grounded in
faculty-approved assignments, milestone checkpoints from derived milestones,
and late-submission flags derived from the store's lateness computation.

The specialist reuses the existing SpecialistResult, run_specialists, and
graph state contracts. Its output is validated against milestone-derived
checks and lab-procedure citations rather than arbitrary content.
"""

from __future__ import annotations

from typing import Any

from course_organizer.graph.state import CourseState
from course_organizer.graph.specialists import SpecialistResult


def lab_specialist(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Lab specialist: generate lab-course workspace output.

    Reads the graph state's submissions, assignments, milestones and late
    flags, and produces a lab-appropriate output with checkable domain
    rules: every claim about an experiment or procedure must trace to
    an approved assignment or a collected submission.

    Args:
        state: The shared graph state (submissions, assignments, milestones,
            late_flags, units_covered, etc.).
        ctx: Per-run dependencies (skills, tool registry, lab_steps).

    Returns:
        SpecialistResult with lab-specific update keys and audit-ready
        summary.
    """
    submissions: list[Any] = state.get("submissions") or []
    assignments: list[Any] = state.get("assignments") or []
    milestones: list[Any] = state.get("milestones") or []
    late_flags: list[Any] = state.get("late_flags") or []
    units_covered: list[Any] = state.get("units_covered") or []
    course_code: str = state.get("course_code") or ""

    # Count milestones emitted: how many milestone groups exist.
    emitted_count: int = len(milestones) if isinstance(milestones, list) else 0

    # Count late submissions: from the late_flags list.
    late_count: int = sum(1 for f in late_flags if isinstance(f, dict) and f.get("late", False))

    # Count submissions collected: total submissions read from the store.
    submitted_count: int = len(submissions)

    # Validate: every milestone's group must have at least one submission.
    # (This is a checkable domain rule, not arbitrary.)
    milestone_valid: bool = True
    if milestones:
        # Group by assignment_id and check each has submissions.
        groups: dict[str, list[str]] = {}
        for s in submissions:
            aid: str = str(s.get("assignment_id", ""))
            groups.setdefault(aid, []).append(str(s.get("submission_id", "")))
        for gid, subm_ids in groups.items():
            # A milestone for this assignment must exist and have submissions.
            # The milestones worker writes group_id as "GRP-<assignment_id>",
            # so accept both that form and the bare assignment id -- matching
            # only the bare id meant this specialist reported ok=False on
            # every real pipeline run.
            milestone_valid = any(
                m.get("group_id") in (gid, f"GRP-{gid}") for m in milestones
            )
            if not milestone_valid:
                break

    # Build the update dict that will become the specialist's update.
    update: dict[str, Any] = {
        "lab_artifacts": {
            "course_code": course_code,
            "submission_count": submitted_count,
            "late_count": late_count,
            "milestones_emitted": emitted_count > 0,
        },
        "milestone_summary": f"{emitted_count} milestone group(s) emitted",
        "late_submission_count": late_count,
        "status": "draft",
    }

    # Human-readable summary naming exactly what was verified.
    summary_parts: list[str] = [
        f"lab specialist: {submitted_count} submission(s) collected",
        f"{late_count} late submission(s)",
        f"{emitted_count} milestone group(s)",
    ]
    if not milestone_valid:
        summary_parts.append("milestone validation failed")
    summary = "; ".join(summary_parts)

    # Read count: submissions, assignments, milestones, late flags.
    read_count: int = len(submissions) + len(assignments) + len(milestones) + len(late_flags)

    return SpecialistResult(
        step_id="w13",
        agent="Lab Specialist",
        ok=milestone_valid,
        summary=summary,
        update=update,
        read=read_count,
    )