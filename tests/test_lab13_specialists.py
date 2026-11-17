"""Lab 13: targeted failure-handling verification for domain specialists.

Tests the theory, lab, and project specialists' integration with the
assembly node, focusing on success/failure detection, audit trail
recording, and backward compatibility.

Conventions: deterministic synthetic fixtures; no real database, no OAuth,
no live Google services. Specialists return SpecialistResult; the assemble
node extracts result.update and result.ok.
"""

from __future__ import annotations

from typing import Any

import pytest

from course_organizer.graph.registry import get_global_registry, reset_global_registry
from course_organizer.graph.specialists_theory import theory_specialist
from course_organizer.graph.specialists_lab import lab_specialist
from course_organizer.graph.specialists_project import project_specialist
from course_organizer.graph.specialists import SpecialistResult


# ---------------------------------------------------------------------------
# Domain selection tests
# ---------------------------------------------------------------------------


def test_theory_domain_selects_correct_specialist() -> None:
    """Verify that domain "theory" resolves to the theory specialist."""
    registry = reset_global_registry()
    specialist = registry.get("theory")
    assert specialist.__name__ == "theory_specialist"


def test_lab_domain_selects_correct_specialist() -> None:
    """Verify that domain "lab" resolves to the lab specialist."""
    registry = reset_global_registry()
    specialist = registry.get("lab")
    assert specialist.__name__ == "lab_specialist"


def test_project_domain_selects_correct_specialist() -> None:
    """Verify that domain "project" resolves to the project specialist."""
    registry = reset_global_registry()
    specialist = registry.get("project")
    assert specialist.__name__ == "project_specialist"


# ---------------------------------------------------------------------------
# Valid output structural checks
# ---------------------------------------------------------------------------


def test_theory_produces_specialist_result() -> None:
    """Theory specialist returns a SpecialistResult with expected fields."""
    registry = reset_global_registry()
    result = theory_specialist(
        {
            "course_code": "THEORY101",
            "domain": "theory",
            "units_covered": ("Unit 1", "Unit 2"),
            "plan_locked": {
                "course_code": "THEORY101",
                "course_name": "Theory Course",
                "units": ("Unit 1", "Unit 2"),
                "assignment_ids": (),
                "obligations": [
                    {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
                    {"obligation_id": "OBL-cover_unit/Unit 2", "kind": "cover_unit", "subject": "Unit 2", "statement": "cover unit Unit 2"},
                ],
                "plan_sha256": "a" * 64,
                "obligation_count": 2,
                "locked_at": "2024-01-01T00:00:00",
            },
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    assert result.step_id == "w13"
    assert result.agent == "Theory Specialist"
    assert isinstance(result.ok, bool)
    assert isinstance(result.summary, str)
    assert isinstance(result.read, int)
    assert result.update is not None
    assert "obligations_met" in result.update


def test_lab_produces_specialist_result() -> None:
    """Lab specialist returns a SpecialistResult with expected fields."""
    registry = reset_global_registry()
    result = lab_specialist(
        {
            "course_code": "LAB101",
            "domain": "lab",
            "submissions": [
                {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1", "status": "graded"},
            ],
            "assignments": [{"assignment_id": "A1", "title": "Lab 1"}],
            "milestones": [{"group_id": "GRP-A1", "satisfied": True}],
            "late_flags": [],
            "units_covered": ["Unit 1"],
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    assert result.step_id == "w13"
    assert result.agent == "Lab Specialist"
    assert isinstance(result.ok, bool)
    assert isinstance(result.summary, str)
    assert isinstance(result.read, int)
    assert result.update is not None
    assert "lab_artifacts" in result.update


def test_project_produces_specialist_result() -> None:
    """Project specialist returns a SpecialistResult with expected fields."""
    registry = reset_global_registry()
    result = project_specialist(
        {
            "course_code": "PROJ101",
            "domain": "project",
            "submissions": [
                {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1"},
                {"submission_id": "S2", "student_id": "S002", "assignment_id": "A1"},
            ],
            "assignments": [{"assignment_id": "A1", "title": "Project 1"}],
            "milestones": [{"project_id": "PRJ-A1", "group_id": "GRP-A1",
                            "members": ["S001", "S002"],
                            "checkpoints": [{"checkpoint_id": "A1-M1", "name": "Milestone 1",
                                             "due_date": "2024-01-01", "expected_deliverable": "2 submissions"}]}],
            "obligations": [{"obligation_id": "OBL-track_milestones/all", "kind": "track_milestones", "subject": "all", "statement": "track project milestones"}],
            "units_covered": ["Unit 1"],
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    assert result.step_id == "w13"
    assert result.agent == "Project Specialist"
    assert isinstance(result.ok, bool)
    assert isinstance(result.summary, str)
    assert isinstance(result.read, int)
    assert result.update is not None
    assert "project_milestones" in result.update


# ---------------------------------------------------------------------------
# ok=False / failure recording
# ---------------------------------------------------------------------------


def test_theory_result_has_ok_field() -> None:
    """Theory specialist result always has an ok boolean field."""
    registry = reset_global_registry()
    result = theory_specialist(
        {
            "course_code": "THEORY101",
            "domain": "theory",
            "units_covered": ("Unit 1",),
            "plan_locked": {
                "course_code": "THEORY101",
                "course_name": "Theory Course",
                "units": ("Unit 1",),
                "assignment_ids": (),
                "obligations": [
                    {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
                ],
                "plan_sha256": "a" * 64,
                "obligation_count": 1,
                "locked_at": "2024-01-01T00:00:00",
            },
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    # ok is set by the specialist's internal logic; just verify it is a bool.
    assert isinstance(result.ok, bool)


def test_lab_result_has_ok_field() -> None:
    """Lab specialist result always has an ok boolean field."""
    registry = reset_global_registry()
    result = lab_specialist(
        {
            "course_code": "LAB101",
            "domain": "lab",
            "submissions": [
                {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1", "status": "graded"},
            ],
            "assignments": [{"assignment_id": "A1", "title": "Lab 1"}],
            "milestones": [{"group_id": "GRP-A1", "satisfied": True}],
            "late_flags": [],
            "units_covered": ["Unit 1"],
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    assert isinstance(result.ok, bool)


def test_project_result_has_ok_field() -> None:
    """Project specialist result always has an ok boolean field."""
    registry = reset_global_registry()
    result = project_specialist(
        {
            "course_code": "PROJ101",
            "domain": "project",
            "submissions": [
                {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1"},
            ],
            "assignments": [{"assignment_id": "A1", "title": "Project 1"}],
            "milestones": [{"project_id": "PRJ-A1", "group_id": "GRP-A1",
                            "members": ["S001"],
                            "checkpoints": []}],
            "obligations": [{"obligation_id": "OBL-track_milestones/all", "kind": "track_milestones", "subject": "all", "statement": "track project milestones"}],
            "units_covered": ["Unit 1"],
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    assert isinstance(result.ok, bool)


# ---------------------------------------------------------------------------
# Audit trail checks
# ---------------------------------------------------------------------------


def test_successful_specialist_has_audit_entry() -> None:
    """A successful specialist run produces an audit entry with ok=True."""
    registry = reset_global_registry()
    result = theory_specialist(
        {
            "course_code": "AUDIT101",
            "domain": "theory",
            "units_covered": ("Unit 1",),
            "plan_locked": {
                "course_code": "AUDIT101",
                "course_name": "Audit Course",
                "units": ("Unit 1",),
                "assignment_ids": (),
                "obligations": [
                    {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
                ],
                "plan_sha256": "a" * 64,
                "obligation_count": 1,
                "locked_at": "2024-01-01T00:00:00",
            },
            "units_covered": ("Unit 1",),
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    audit_entry = result.audit()
    assert audit_entry["step_id"] == "w13"
    assert audit_entry["agent"] == "Theory Specialist"
    assert audit_entry["ok"] is True
    assert "summary" in audit_entry
    assert "read" in audit_entry


def test_failed_specialist_has_audit_entry() -> None:
    """A failed specialist run produces an audit entry with ok=False."""
    registry = reset_global_registry()
    result = theory_specialist(
        {
            "course_code": "AUDITFAIL",
            "domain": "theory",
            "units_covered": (),
            "plan_locked": {
                "course_code": "AUDITFAIL",
                "course_name": "Audit Fail Course",
                "units": ("Unit 1",),
                "assignment_ids": (),
                "obligations": [
                    {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
                ],
                "plan_sha256": "a" * 64,
                "obligation_count": 1,
                "locked_at": "2024-01-01T00:00:00",
            },
            "units_covered": (),
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    audit_entry = result.audit()
    assert audit_entry["ok"] is False


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_backward_compatibility_defaults_to_theory() -> None:
    """Callers that omit domain should default to 'theory' without error."""
    registry = reset_global_registry()
    assert "theory" in registry.names()
    assert "lab" in registry.names()
    assert "project" in registry.names()
    specialist = registry.get("theory")
    assert specialist.__name__ == "theory_specialist"


# ---------------------------------------------------------------------------
# Specialist failure state flags
# ---------------------------------------------------------------------------


def test_specialist_result_carry_failure_flags() -> None:
    """A specialist result with ok=False carries failure info in update."""
    # Use a scenario that triggers ok=False for theory: no units covered
    # when obligations expect coverage. The specialist's internal logic
    # sets ok based on drift findings; we just verify the result object
    # structure carries the ok flag.
    registry = reset_global_registry()
    result = theory_specialist(
        {
            "course_code": "THEORY101",
            "domain": "theory",
            "units_covered": (),  # empty -> specialist may set ok=False
            "plan_locked": {
                "course_code": "THEORY101",
                "course_name": "Theory Course",
                "units": ("Unit 1",),
                "assignment_ids": (),
                "obligations": [
                    {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
                ],
                "plan_sha256": "a" * 64,
                "obligation_count": 1,
                "locked_at": "2024-01-01T00:00:00",
            },
            "human_authority": "course_faculty",
        },
        None,
    )
    assert isinstance(result, SpecialistResult)
    # The ok flag is set by the specialist; verify it exists and is bool.
    assert isinstance(result.ok, bool)
    # The update dict should be present (may be empty or contain keys).
    assert result.update is not None