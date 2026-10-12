"""Doc 2.12 line 825's escalation: *"on exhaustion, escalate to course faculty
with a diagnosis."*

One function, two callers, one rule: **a person is about to take over, so tell
them enough to decide.** Doc 2.12 line 826 makes the same demand of the human's
*options* -- they should be *"effortless and logged"* -- which is the reason this
module attaches the draft rather than just the failure list. A human handed "3
attempts failed" restarts the run; a human handed the draft can fix the one
submission that broke.

The two reasons are kept distinct because they need different actions:

``retry_budget_exhausted``
    Something about the data is wrong in a way regeneration did not fix. The
    human needs the per-check detail and the draft to correct it.

``refuse``
    The human said no. Re-running will not change their mind. The escalation
    here is a *record*, not a request -- it exists so the refusal is logged with
    the run it refused, which doc 2.12 line 834's audit guardrail requires.
"""

from __future__ import annotations

from typing import Any, Mapping

from course_organizer.graph.state import Escalation

#: Doc 2.4 line 523 names the escalation target. Kept as a constant because it
#: is the doc's word and three modules quote it.
DEFAULT_ESCALATION_TARGET = "course_faculty"


def build_escalation(
    state: Mapping[str, Any],
    *,
    reason: str,
    max_attempts: int,
    escalated_to: str = DEFAULT_ESCALATION_TARGET,
    decided_by: str = "",
) -> Escalation:
    """Build the escalation, with the draft attached.

    Args:
        state: The run's state at the moment it stopped.
        reason: ``retry_budget_exhausted`` or ``refuse``.
        max_attempts: The bound that was spent.
        escalated_to: The named human. Doc 2.2 line 467.
        decided_by: For a refusal, who refused. Empty for exhaustion, where
            nobody refused -- a machine ran out of attempts.

    Returns:
        The escalation. ``pending_draft`` carries the grades as they stood, so
        the human does not have to reproduce the run to see the work.
    """
    validation = state.get("validation")
    failing = list(getattr(validation, "failing", []) or [])
    attempts = int(state.get("attempts", 0))
    grades = state.get("draft_grades", []) or []

    if reason == "refuse":
        diagnosis = (
            f"{decided_by or 'the reviewing human'} refused publication after "
            f"{attempts} validation pass(es). No grade was published; the "
            f"{len(grades)} draft grade(s) are retained unapproved for "
            f"inspection. Nothing further happens unless a person asks for it."
        )
    else:
        detail = getattr(validation, "diagnosis", "") or "no diagnosis recorded"
        diagnosis = (
            f"Validation still failing after {attempts} of {max_attempts} "
            f"permitted attempt(s); the run stopped rather than retrying "
            f"forever. {detail} The draft below is the version that failed, so "
            f"the failing rows can be corrected without re-running."
        )

    pending = {
        "document_id": state.get("document_id", ""),
        "document_status": state.get("document_status", ""),
        "units_covered": list(state.get("units_covered", []) or []),
        "assignments_included": [
            a["assignment_id"] for a in state.get("assignments", [])
        ],
        "draft_grades": [
            g.model_dump(mode="json") if hasattr(g, "model_dump") else dict(g)
            for g in grades
        ],
        "similarity_flags": list(state.get("similarity_flags", []) or []),
        "attempt_log": list(state.get("attempt_log", []) or []),
        "failing_checks": failing,
    }

    return Escalation(
        reason=reason,
        diagnosis=diagnosis,
        raised_by="validate" if reason != "refuse" else "gate",
        attempts=attempts,
        max_attempts=max_attempts,
        failing=failing,
        escalated_to=escalated_to,
        pending_draft=pending,
    )


__all__ = [
    "DEFAULT_ESCALATION_TARGET",
    "build_escalation",
]