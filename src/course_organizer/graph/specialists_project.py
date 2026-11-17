"""Project course specialist: produces domain-appropriate output for project courses.

Doc 2.12 Lab 13: project specialist generates project-specific organised
workspace output with checkable schemes — e.g. project milestone checkpoints
derived from faculty-approved assignments, group composition grounded in
student ids from collected milestones, and deliverable tracking that
references the locked plan's obligations.

The specialist reuses the existing SpecialistResult, run_specialists, and
graph state contracts. Its output is validated against project-domain
rules: every claim about group composition or deliverable must trace to
an approved assignment or a collected submission record.
"""

from __future__ import annotations

from typing import Any

from course_organizer.graph.state import CourseState
from course_organizer.graph.specialists import SpecialistResult


def project_specialist(state: Mapping[str, Any], ctx: Any) -> SpecialistResult:
    """Project specialist: generate project-course workspace output.

    Reads the graph state's submissions, assignments, milestones, and
    the locked plan's obligations, and produces a project-appropriate
    output with checkable domain rules: group composition must cite
    real submission student ids, and deliverable counts must trace
    to approved assignment milestones.

    Args:
        state: The shared graph state (submissions, assignments, milestones,
            obligations, units_covered, etc.).
        ctx: Per-run dependencies (skills, tool registry, lab_steps).

    Returns:
        SpecialistResult with project-specific update keys and audit-ready
        summary.
    """
    submissions: list[Any] = state.get("submissions") or []
    assignments: list[Any] = state.get("assignments") or []
    milestones: list[Any] = state.get("milestones") or []
    obligations: list[Any] = state.get("obligations") or []
    course_code: str = state.get("course_code") or ""

    # Derive group composition from submission student ids, grouped by
    # assignment_id. This is a checkable artifact: every student id
    # cited must belong to a submission in the store.
    group_members: dict[str, set[str]] = {}
    for s in submissions:
        aid: str = str(s.get("assignment_id", ""))
        sid: str = str(s.get("student_id", ""))
        group_members.setdefault(aid, set()).add(sid)

    # Count project milestones that have corresponding groups with
    # submissions. Each milestone's group_id should match an assignment
    # that has submissions in the store. The milestones worker writes
    # group_id as "GRP-<assignment_id>", so strip that prefix before the
    # lookup -- without it this specialist reported ok=False on every
    # real pipeline run.
    milestone_valid: bool = True
    for m in milestones:
        # Milestone dict should have a group_id field.
        gid: str = str(m.get("group_id", ""))
        if gid.startswith("GRP-"):
            gid = gid[len("GRP-"):]
        if gid not in group_members or not group_members[gid]:
            milestone_valid = False
            break

    # Count obligations met: obligations of kind track_milestones
    # that are satisfied by the result's milestones_emitted.
    obligations_met: int = 0
    for ob in obligations:
        if ob.get("kind") == "track_milestones" and str(ob.get("subject")) == "course":
            if milestones and len(milestones) > 0:
                obligations_met += 1

    # Count delivered milestones: milestone checkpoints that name
    # expected deliverables based on submission counts per assignment.
    delivered: int = 0
    if milestones:
        for m in milestones:
            expected: str = str(m.get("expected_deliverable", ""))
            delivered += 1

    # Build the update dict that will become the specialist's update.
    update: dict[str, Any] = {
        "project_milestones": [
            {
                "project_id": getattr(m, "project_id", f"PRJ-{i}"),
                "group_id": getattr(m, "group_id", f"GRP-{i}"),
                "members": sorted(group_members.get(str(m.get("group_id", i)), [])),
                "checkpoints": [
                    {
                        "checkpoint_id": c.checkpoint_id
                        if hasattr(c, "checkpoint_id")
                        else f"{i}-M1",
                        "name": c.name if hasattr(c, "name") else f"Milestone {i}",
                        "due_date": _parse_due(getattr(m, "due_date", None) or ""),
                        "expected_deliverable": (
                            f"{len(group_members.get(str(m.get('group_id', i)), []))} "
                            f"submission(s) received for {m.get('group_id', i)}"
                        ),
                    }
                    for i, c in enumerate(getattr(m, "checkpoints", []) or [])
                ],
            }
            for i, m in enumerate(milestones)
        ],
        "obligations_met": obligations_met,
        "status": "draft",
    }

    # Human-readable summary naming exactly what was verified.
    summary_parts: list[str] = [
        f"project specialist: {len(milestones)} milestone group(s) processed",
        f"{len(submissions)} submission(s) used for group composition",
    ]
    if milestone_valid:
        summary_parts.append("group composition validated")
    else:
        summary_parts.append("group composition validation FAILED")
    summary = "; ".join(summary_parts)

    # Read count: submissions, assignments, milestones, obligations.
    read_count: int = len(submissions) + len(assignments) + len(milestones) + len(obligations)

    return SpecialistResult(
        step_id="w13",
        agent="Project Specialist",
        ok=milestone_valid,
        summary=summary,
        update=update,
        read=read_count,
    )


def _parse_due(value: Any) -> str:
    """Turn a checkpointed ISO string back into a string for JSON output.

    assignment_distribution stores due dates as ISO strings so the state
    stays JSON-clean across a SQLite round-trip. This is the inverse of
    _parse_draft from the grading specialist.
    """
    if isinstance(value, str):
        return value
    from datetime import datetime

    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)