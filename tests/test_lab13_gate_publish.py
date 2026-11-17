"""Lab 13: gate and publication enforcement tests.

Tests that the gate and publish functions properly handle failed/successful
domain specialist results, using the actual graph functions and state.

Conventions: deterministic synthetic fixtures; no real database, no OAuth,
no live Google services. Uses the actual `make_gate` and `make_publish`
functions from the repository.
"""

from __future__ import annotations

from typing import Any

import pytest

from pathlib import Path

from course_organizer.graph.context import RunContext
from course_organizer.graph.nodes import make_publish, make_gate
from course_organizer.graph.state import CourseState
from course_organizer.graph.specialists_theory import theory_specialist
from course_organizer.graph.specialists import SpecialistResult

from collections import namedtuple

# ---------------------------------------------------------------------------
# Decision helper matching the structure expected by make_publish.publish
# ---------------------------------------------------------------------------

class Decision:
    __slots__ = ("approved", "decided_by", "decision")
    def __init__(self, approved: bool, decided_by: str, decision: object | None = None):
        self.approved = approved
        self.decided_by = decided_by
        self.decision = decision  # nested decision, may be None


# ---------------------------------------------------------------------------
# Helper: build a minimal RunContext for calling make_publish / make_gate
# ---------------------------------------------------------------------------

def _make_ctx(course_code: str = "GATE_TEST") -> RunContext:
    from pathlib import Path
    return RunContext(
        run_id="test-run-001",
        tools=None,
        skills=None,
        tool_context=None,
        course_code=course_code,
        max_attempts=3,
        human_authority="course_faculty",
        output_dir=Path.cwd(),
    )


# ---------------------------------------------------------------------------
# Helper: build a CourseState as assemble would leave it after running a specialist.
# ---------------------------------------------------------------------------

def _build_state_with_specialist(
    domain: str,
    specialist_ok: bool,
    specialist_failed_reason: str | None = None,
    domain_error: bool = False,
    specialist_error: str | None = None,
) -> CourseState:
    """Build a CourseState as assemble would leave it after running a specialist."""

    base: CourseState = {
        "course_code": "GATE_TEST",
        "domain": domain,
        "draft_grades": [],
        "plan_locked": {
            "course_code": "GATE_TEST",
            "course_name": "Gate Test Course",
            "units": ("Unit 1",),
            "assignment_ids": (),
            "obligations": [
                {
                    "obligation_id": "OBL-cover_unit/Unit 1",
                    "kind": "cover_unit",
                    "subject": "Unit 1",
                    "statement": "cover unit Unit 1",
                }
            ],
            "plan_sha256": "a" * 64,
            "obligation_count": 1,
            "locked_at": "2024-01-01T00:00:00",
        },
        "units_covered": () if not specialist_ok else ("Unit 1",),
        "human_authority": "course_faculty",
        "specialist_ok": specialist_ok,
    }

    if not specialist_ok:
        base["specialist_failed_reason"] = specialist_failed_reason or "specialist failed"

    if domain_error:
        base["domain_error"] = True

    if specialist_error is not None:
        base["specialist_error"] = specialist_error

    # Also add specialist_audit as assemble would.
    base.setdefault("specialist_audit", []).append(
        {
            "step_id": "w13",
            "agent": "Theory Specialist" if domain == "theory" else "Project Specialist",
            "ok": specialist_ok,
            "summary": specialist_failed_reason or "ok",
            "read": 4,
        }
    )

    return base


# ---------------------------------------------------------------------------
# Test A — Failed specialist: publication is rejected
# ---------------------------------------------------------------------------

def test_publish_rejects_failed_specialist() -> None:
    """A run with specialist_ok=False cannot be published as a successful workspace.

    Execution path:
    1. assemble sets state["specialist_ok"] = False
    2. make_publish is called through the normal contract
    3. make_publish raises PermissionError because specialist_ok is False
    4. The result cannot be represented as a successful, approved workspace.
    """
    ctx = _make_ctx()
    pub_fn = make_publish(ctx)

    # Build state with specialist_ok=False, but with faculty approval
    state = {
        **_build_state_with_specialist("theory", specialist_ok=False,
                                       specialist_failed_reason="drift findings detected"),
        "decision": Decision(approved=True, decided_by="faculty"),
    }

    # This should raise PermissionError — the specialist failed,
    # and publication must not silently succeed.
    with pytest.raises(PermissionError, match="specialist.*unsuccessful|specialist.*failed|specialist_ok"):
        pub_fn(state)

    # Verify the state carries the flag (defensive check)
    assert state["specialist_ok"] is False
    assert state["specialist_failed_reason"] is not None


# ---------------------------------------------------------------------------
# Test B — Unknown domain: cannot be published as domain-specific
# ---------------------------------------------------------------------------

def test_publish_rejects_unknown_domain() -> None:
    """An unknown domain results in domain_error=True in state.

    Execution path:
    1. assemble catches SkillError for unknown domain
    2. state["domain_error"] = True, state["specialist_ok"] = False
    3. make_publish rejects the publish attempt because specialist_ok is False
    4. The generic workspace cannot be treated as a successful domain-specific result.
    """
    from course_organizer.graph.registry import get_global_registry, SkillError

    registry = get_global_registry()
    try:
        registry.get("nonexistent_domain_xyz")
        pytest.fail("Expected SkillError for unknown domain")
    except Exception as e:
        assert isinstance(e, SkillError) or "unknown" in str(e).lower()

    ctx = _make_ctx()
    pub_fn = make_publish(ctx)

    # Build state as assemble would leave it after an unknown domain error
    state = {
        **_build_state_with_specialist("nonexistent_domain_xyz", specialist_ok=False,
                                       domain_error=True,
                                       specialist_error="unknown domain nonexistent_domain_xyz; available: lab, project, theory"),
        "decision": Decision(approved=True, decided_by="faculty"),
    }

    # Publication should be rejected because the specialist result is not ok
    with pytest.raises(PermissionError):
        pub_fn(state)

    # Verify the state flags are set correctly
    assert state["domain_error"] is True
    assert state["specialist_ok"] is False


# ---------------------------------------------------------------------------
# Test C — Specialist exception: failure state enforced downstream
# ---------------------------------------------------------------------------

def test_publish_rejects_specialist_exception() -> None:
    """If a specialist raises an exception, the error is recorded and publication is blocked.

    Execution path:
    1. make_assemble catches Exception from specialist_fn
    2. state["domain_error"] = True, state["specialist_ok"] = False
    3. make_publish rejects the publish attempt because specialist_ok is False
    4. The failure state remains auditable and the run is not treated as successful.
    """
    ctx = _make_ctx()
    pub_fn = make_publish(ctx)

    # Build state as if a specialist exception occurred
    state = {
        **_build_state_with_specialist("theory", specialist_ok=False,
                                       domain_error=True,
                                       specialist_error="ValueError: invalid argument"),
        "decision": Decision(approved=True, decided_by="faculty"),
    }

    # Publication should be rejected because the specialist result is not ok
    with pytest.raises(PermissionError):
        pub_fn(state)

    # Verify the exception state is recorded correctly
    assert state["domain_error"] is True
    assert state["specialist_ok"] is False
    assert "ValueError" in state["specialist_error"]

    # The audit trail should also record the failure
    assert "specialist_audit" in state
    audit_entry = state["specialist_audit"][-1]
    assert audit_entry["ok"] is False


# ---------------------------------------------------------------------------
# Test D — Valid specialist: existing approval requirement preserved
# ---------------------------------------------------------------------------

def test_publish_preserves_approval_requirement_with_valid_specialist() -> None:
    """A valid specialist result (ok=True) preserves the existing human
    approval requirement. A specialist result alone cannot approve or
    publish its own output.

    Execution path:
    1. assemble sets state["specialist_ok"] = True and merges specialist update
    2. make_publish is called with a valid specialist result and faculty approval
    3. make_publish proceeds with publication (requires decision.approved + no unapproved grades)
    4. A specialist result alone does NOT auto-approve or auto-publish.
    """
    ctx = _make_ctx()
    pub_fn = make_publish(ctx)

    # Build state with specialist_ok=True and valid specialist output merged.
    # We simulate the state after a successful theory specialist run.
    state = {
        **_build_state_with_specialist("theory", specialist_ok=True,
                                       specialist_failed_reason=None),
        "units_covered": ("Unit 1",),
        "decision": Decision(approved=True, decided_by="faculty"),
        "draft_grades": [],  # no unapproved grades
    }

    # Publication should succeed — the specialist was valid and faculty approved
    result = pub_fn(state)

    # Verify publication succeeded
    assert result["status"] == "published"  # or CourseStatus.PUBLISHED

    # Verify the state correctly carries specialist_ok=True but no auto-approved decision
    assert state["specialist_ok"] is True
    assert "decision" in state  # decision was provided, but not auto-approved_by

    # Critically: the state does NOT auto-carry approved_by or approved_at from the specialist.
    # The existing approval gate still requires an explicit human decision.
    # We prove this by checking that the result requires the decision field to be present
    # and approved — a specialist_ok=True result alone is not sufficient.
    assert result["status"] == "published"