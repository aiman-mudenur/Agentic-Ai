"""Lab 14 Capstone Tests - End-to-End Domain Specialist Integration.

Tests the full pipeline: API domain selection -> graph seeding -> specialist
execution -> validation/grounding -> assemble with domain-specific output ->
gate -> publication.

Uses synthetic fixtures; no real database, no OAuth, no live Google services.
All course codes use CS201 (the only course in the seed corpus).

Isolation: every test uses a unique run id *and* points the checkpointer root
and the publish output directory at pytest's ``tmp_path``. LangGraph keys
checkpoints by thread (``thread-<run_id>``), so a run id that already has a
``graph.sqlite`` on disk resumes that old thread instead of executing fresh --
which silently returns a previous run's state. Isolating both the checkpoint
root and the run id is what makes these tests order- and repeat-independent.
"""

from __future__ import annotations

import uuid

import pytest

from course_organizer.graph.registry import get_global_registry
from course_organizer.graph.runner import resume_run, run_pipeline
from course_organizer.graph.specialists import SpecialistResult


# ===========================================================================
# Helpers: isolated pipeline runs
# ===========================================================================


def _run_to_gate(tmp_path, course_code: str = "CS201", domain: str | None = "theory"):
    """Run the pipeline to the gate in an isolated, per-test directory.

    ``domain=None`` omits the domain kwarg entirely so the default-to-theory
    path is exercised exactly as an API caller that never heard of domains
    would hit it. Returns the ``GraphRun`` handle.
    """
    kwargs = {} if domain is None else {"domain": domain}
    return run_pipeline(
        course_code=course_code,
        run_id=f"cap_{uuid.uuid4().hex[:8]}",
        root=tmp_path,
        output_dir=tmp_path / "publish",
        **kwargs,
    )


def _resume(run, decision: str):
    """Resume a suspended run, reopening its checkpointer under tmp_path."""
    return resume_run(run_id=run.run_id, decision=decision, root=tmp_path_for(run))


def tmp_path_for(run) -> "object":
    """Recover the tmp root a run was started under, from its output dir.

    ``run_pipeline`` published nothing yet at the gate, but the context's
    output_dir was ``<tmp>/publish``; the checkpoint root is its parent.
    """
    return run.context.output_dir.parent


def _failing_specialist(state, ctx) -> SpecialistResult:
    """A specialist that completes without raising but reports failure."""
    return SpecialistResult(
        step_id="w13",
        agent="Capstone Failing Specialist",
        ok=False,
        summary="synthetic specialist failure injected by the capstone test",
        read=0,
    )


def _raising_specialist(state, ctx) -> SpecialistResult:
    """A specialist that raises, exercising assemble's except path."""
    raise RuntimeError("synthetic specialist explosion injected by the capstone test")


def _inject_domain(monkeypatch, domain: str, specialist) -> None:
    """Register a synthetic domain for one test, restored on teardown.

    ``monkeypatch.setitem`` on the registry's table both adds the entry and
    removes it afterwards, so the global registry other test modules rely on
    (Lab 13 resets it) is never permanently altered.
    """
    registry = get_global_registry()
    monkeypatch.setitem(registry._specialists, domain, specialist)


# ===========================================================================
# A. Theory domain
# ===========================================================================


def test_theory_domain_specialist_selected(tmp_path) -> None:
    """Theory domain: theory specialist selected; domain-specific output reaches workspace."""
    run = _run_to_gate(tmp_path, domain="theory")
    assert run.awaiting_gate, "Pipeline should pause at faculty approval gate"
    # The seed carried the requested domain and it survived the whole chain.
    assert run.state.get("domain") == "theory"
    # Theory specialist succeeded: specialist_ok=True, no domain_error
    assert run.state.get("specialist_ok") is True
    assert run.state.get("domain_error") is False
    # Drift findings empty -> specialist ok
    assert run.state.get("drift_findings") == []
    # Validation passed
    val = run.state.get("validation")
    assert val is not None
    assert val.passed is True  # ValidationOutcome is a pydantic model; use .passed not .get()
    # Document produced with organised-course template
    # document_status is a top-level key (not inside the 'document' dict)
    doc_status = run.state.get("document_status")
    assert doc_status == "ready_for_review", f"Expected ready_for_review, got {doc_status}"
    # Also verify the document body contains course workspace info
    doc = run.state.get("document", {})
    assert "Course Workspace" in doc.get("body", "")
    # The gate named the specialist outcome for the reviewer.
    req = run.gate_request
    assert req is not None
    assert req.domain == "theory"
    assert req.specialist_ok is True
    assert req.domain_error is False


# ===========================================================================
# B. Lab domain -- domain parameter flows into CourseState (added in Phase 3),
# so make_assemble uses the declared domain instead of defaulting to "theory".
# The lab specialist IS selected when domain="lab" is passed.
# ===========================================================================


def test_lab_domain_specialist_selected(tmp_path) -> None:
    """Lab domain: lab specialist selected; lab-specific output in workspace."""
    run = _run_to_gate(tmp_path, domain="lab")
    assert run.awaiting_gate
    # Domain is preserved in CourseState; make_assemble used "lab"
    assert run.state.get("domain") == "lab"
    # Specialist succeeded (lab specialist returns ok=True when valid)
    assert run.state.get("specialist_ok") is True
    assert run.state.get("domain_error") is False
    # Drift findings status
    assert run.state.get("drift_findings") is not None
    # Document produced with lab-specific output in workspace
    doc = run.state.get("document", {})
    body = doc.get("body", "")
    # Lab specialist output includes references to lab steps/milestones;
    # the document body contains domain-specific content from the specialist.
    assert "lab" in body.lower(), (
        f"Expected lab-specific output in document body, got: {body[:200]}"
    )


# ===========================================================================
# C. Project domain -- Same as Lab: domain parameter flows into CourseState,
# so make_assemble uses the declared domain.
# ===========================================================================


def test_project_domain_specialist_selected(tmp_path) -> None:
    """Project domain: project specialist selected; project output in workspace."""
    run = _run_to_gate(tmp_path, domain="project")
    assert run.awaiting_gate
    # Domain is preserved in CourseState; make_assemble used "project"
    assert run.state.get("domain") == "project"
    # Specialist succeeded (project specialist returns ok=True when valid)
    assert run.state.get("specialist_ok") is True
    assert run.state.get("domain_error") is False
    # Drift findings status
    assert run.state.get("drift_findings") is not None
    # Document produced with project-specific output in workspace
    doc = run.state.get("document", {})
    body = doc.get("body", "")
    assert "Project Specialist" in body or "milestone" in body.lower(), (
        f"Expected project-specific output in document body, got: {body[:200]}"
    )


# ===========================================================================
# D. Failed specialist
# ===========================================================================


def test_failed_specialist_blocked_from_publication(tmp_path, monkeypatch) -> None:
    """Failed specialist: when the specialist returns ok=False, publication is
    blocked even with faculty approval.

    Execution path:
    1. assemble records specialist_ok=False and the failure reason.
    2. The gate still suspends -- the named human decides, and is told.
    3. Resuming with 'approve' reaches publish, which re-checks and raises
       PermissionError. Nothing is written to disk.
    """
    _inject_domain(monkeypatch, "capfail", _failing_specialist)
    run = _run_to_gate(tmp_path, domain="capfail")
    assert run.awaiting_gate
    # The state carries the specialist outcome, not just dictionary keys:
    assert run.state.get("specialist_ok") is False
    assert run.state.get("domain_error") is False
    # The failure reason reaches the state and the reviewer's request.
    assert run.state.get("specialist_failed_reason")
    assert run.gate_request.specialist_ok is False
    # Approval is not sufficient: publish must refuse and write nothing.
    with pytest.raises(PermissionError, match="specialist"):
        _resume(run, "approve")
    assert not any((tmp_path / "publish").glob("*.md")), (
        "a failed specialist must not publish a workspace file"
    )


# ===========================================================================
# E. Unknown domain and specialist exception
# ===========================================================================


def test_unknown_domain_records_error(tmp_path) -> None:
    """Unknown domain: the assemble node's except block catches the
    SkillError, sets domain_error=True and specialist_ok=False in state,
    and the run is not treated as successful."""
    run = _run_to_gate(tmp_path, domain="invalid_domain")
    assert run.awaiting_gate
    # The requested domain is preserved verbatim -- an invalid domain is
    # recorded as-is, never silently replaced with theory.
    assert run.state.get("domain") == "invalid_domain"
    # Assemble caught the unknown-domain error and recorded it
    assert run.state.get("domain_error") is True
    assert run.state.get("specialist_ok") is False
    # The reviewer is told, in the gate request itself.
    assert run.gate_request.domain_error is True
    assert run.gate_request.specialist_ok is False


def test_unknown_domain_cannot_publish_even_when_approved(tmp_path) -> None:
    """An unknown domain cannot publish even if faculty approves.

    Approval is necessary but not sufficient: publish re-checks the
    specialist outcome at the point of publication and raises
    PermissionError, writing no workspace file.
    """
    run = _run_to_gate(tmp_path, domain="invalid_domain")
    assert run.awaiting_gate
    with pytest.raises(PermissionError, match="specialist"):
        _resume(run, "approve")
    assert not any((tmp_path / "publish").glob("*.md")), (
        "an unknown-domain run must not publish a workspace file"
    )


def test_specialist_exception_not_treated_as_success(tmp_path, monkeypatch) -> None:
    """Specialist exception: the assemble node catches exceptions from the
    specialist_fn and records domain_error=True, specialist_ok=False, and
    records the error in the audit trail. The run cannot be mistaken for
    a successful specialist execution, and cannot publish."""
    _inject_domain(monkeypatch, "capboom", _raising_specialist)
    run = _run_to_gate(tmp_path, domain="capboom")
    assert run.awaiting_gate
    # The assemble node caught the exception and recorded it in state
    assert run.state.get("domain_error") is True
    assert run.state.get("specialist_ok") is False
    assert "synthetic specialist explosion" in str(run.state.get("specialist_error", ""))
    # Approval cannot launder a crashed specialist into a published workspace.
    with pytest.raises(PermissionError, match="specialist"):
        _resume(run, "approve")
    assert not any((tmp_path / "publish").glob("*.md"))


# ===========================================================================
# F. Faculty approval and publication
# ===========================================================================


def test_approval_before_publication(tmp_path) -> None:
    """Run pauses at gate; no publication before a decision is supplied.
    Resume with 'approve' permits publication."""
    run = _run_to_gate(tmp_path)
    assert run.awaiting_gate
    # No published_path before resume
    assert run.state.get("published_path") is None
    assert not any((tmp_path / "publish").glob("*.md"))
    # Resume with approval
    run2 = resume_run(run_id=run.run_id, decision="approve", root=tmp_path)
    assert run2.published
    assert run2.state.get("published_path") is not None


def test_refusal_does_not_publish(tmp_path) -> None:
    """Faculty refusal does not publish the workspace."""
    run = _run_to_gate(tmp_path)
    assert run.awaiting_gate
    # Resume with refusal
    run2 = resume_run(run_id=run.run_id, decision="refuse", root=tmp_path)
    assert not run2.published
    assert run2.state.get("published_path") is None
    assert not any((tmp_path / "publish").glob("*.md"))


def test_publication_requires_valid_specialist_and_approval(tmp_path) -> None:
    """Publication only succeeds when specialist_ok=True, approval given,
    and no unapproved grades exist."""
    run = _run_to_gate(tmp_path)
    assert run.awaiting_gate
    assert run.state.get("specialist_ok") is True
    # Resume with approval -- should succeed because specialist was ok,
    # no unapproved grades, and faculty approved
    run2 = resume_run(run_id=run.run_id, decision="approve", root=tmp_path)
    assert run2.published
    assert run2.state.get("published_path") is not None


# ===========================================================================
# G. Backward compatibility
# ===========================================================================


def test_omitting_domain_defaults_to_theory(tmp_path) -> None:
    """Omitting domain retains the established default (theory).
    When domain is omitted, the seed defaults to 'theory' and the theory
    specialist runs."""
    run = _run_to_gate(tmp_path, domain=None)
    assert run.awaiting_gate
    assert run.state.get("domain") == "theory"
    # Theory specialist runs by default (specialist_ok=True, domain_error=False)
    assert run.state.get("specialist_ok") is True
    assert run.state.get("domain_error") is False
    # drift_findings empty -> theory specialist ok
    assert run.state.get("drift_findings") == []
