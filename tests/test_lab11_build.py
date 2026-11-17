"""Lab 11 — tested build factory.

Uses the real graph against the real seed corpus (synthetic course data, no
credentials, no network). Pipeline runs are the honest cost of testing a
factory whose job is running the pipeline; refusal/branch tests run nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from course_organizer.build import (
    AcceptanceReport,
    BuildRequest,
    diff_plans,
    evaluate_acceptance,
    run_build,
    verify_input_plan,
)
from course_organizer.graph.runner import run_pipeline
from course_organizer.graph.state import DOC_CHECKS


def _capture_plan(root: Path, run_id: str, **kw):
    """A genuine locked plan: the pipeline's own, read back from its state."""
    run = run_pipeline(course_code="CS201", run_id=run_id, root=root,
                       output_dir=root / run_id, **kw)
    assert run.awaiting_gate, run.summary()
    return dict(run.state["plan_locked"])


def _request(plan, run_id, **kw):
    args = dict(locked_plan=plan, course_code="CS201", run_id=run_id)
    args.update(kw)
    return BuildRequest(**args)


# ---------------------------------------------------------------------------
# Passing build
# ---------------------------------------------------------------------------


def test_passing_build_is_ready_for_review_and_advances_nothing(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap1")
    result = run_build(_request(plan, "b1"), root=tmp_path / "runs")
    assert result.status == "ready_for_review", result.reason
    assert result.can_advance is True and result.gate_awaiting is True
    assert result.published is None
    assert result.failed_checks == []
    assert result.acceptance_report is not None
    assert result.acceptance_report.passed is True
    assert result.acceptance_report.checks_run == list(DOC_CHECKS)
    assert result.acceptance_report.plan_identity_ok is True
    assert result.artifact.get("document_id")
    assert result.evidence, "grounding tables must ride the result"
    assert len(result.audit_trail) > 0
    report_path = tmp_path / "runs" / "b1" / "build_report.json"
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "ready_for_review"


# ---------------------------------------------------------------------------
# Failed / exhausted builds cannot advance
# ---------------------------------------------------------------------------


def test_exhausted_retries_escalate_without_publishing(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap2")
    result = run_build(
        _request(plan, "b2", max_attempts=2, fail_until_attempt=99),
        root=tmp_path / "runs")
    assert result.status == "escalated", result.reason
    assert result.can_advance is False and result.gate_awaiting is False
    assert result.published is None


def test_repairable_injected_failure_heals_inside_the_build(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap3")
    result = run_build(
        _request(plan, "b3", max_attempts=3, fail_until_attempt=1),
        root=tmp_path / "runs")
    assert result.status == "ready_for_review", result.reason
    assert result.acceptance_report is not None
    assert result.acceptance_report.attempts == 2


# ---------------------------------------------------------------------------
# Refusals happen before anything runs
# ---------------------------------------------------------------------------


def test_malformed_plan_is_refused_with_no_run_started(tmp_path):
    result = run_build(_request({"not": "a plan"}, "ref1"), root=tmp_path / "runs")
    assert result.status == "refused" and "malformed" in result.reason
    assert not (tmp_path / "runs" / "ref1").exists()


def test_tampered_plan_is_refused_with_no_run_started(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap4")
    plan = dict(plan)
    plan["units"] = ["2", "1"]  # reorder scope: hash no longer matches
    result = run_build(_request(plan, "ref2"), root=tmp_path / "runs")
    assert result.status == "refused" and "tampered" in result.reason
    assert not (tmp_path / "runs" / "ref2").exists()


def test_unlocked_plan_is_refused(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap5")
    plan = dict(plan)
    plan["locked_at"] = ""
    result = run_build(_request(plan, "ref3"), root=tmp_path / "runs")
    assert result.status == "refused" and "unlocked" in result.reason
    assert not (tmp_path / "runs" / "ref3").exists()


def test_contradictory_course_is_refused(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap6")
    request = BuildRequest(locked_plan=plan, course_code="NOPE101", run_id="ref4")
    result = run_build(request, root=tmp_path / "runs")
    assert result.status == "refused" and "contradictory" in result.reason
    assert not (tmp_path / "runs" / "ref4").exists()


def test_unknown_course_blocks_as_infrastructure_without_crashing(tmp_path):
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    inputs = PlanInputs(
        course_code="NOPE101", course_name="Nope", units=("1",),
        assignment_ids=("A-1",), include_milestones=True,
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "NOPE101", "unit_count": 1},
        as_of="2026-10-15")
    plan = expand(inputs).lock().model_dump(mode="json")
    result = run_build(_request(plan, "b4", course_code="NOPE101"), root=tmp_path / "runs")
    assert result.status == "blocked", result.reason
    assert "infrastructure" in result.reason
    assert result.can_advance is False and result.published is None


# ---------------------------------------------------------------------------
# Plan mismatch, incomplete reports, published runs
# ---------------------------------------------------------------------------


def test_supported_rescoped_plan_is_honored_not_blocked(tmp_path):
    """The correction working as designed: a valid plan whose scope differs
    from the store default is *executed under its own scope*, not blocked.
    The old post-run-diff-only behavior would have blocked this run."""
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    def _plan(units):
        inputs = PlanInputs(
            course_code="CS201", course_name="Data Structures", units=units,
            assignment_ids=("ASG-01",), include_milestones=True,
            steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                           acceptance_criteria=("x",)),),
            verified_facts={"course_code": "CS201", "unit_count": len(units)},
            as_of="2026-10-15")
        return expand(inputs).lock().model_dump(mode="json")

    input_plan = _plan(("2", "1"))
    assert input_plan["plan_sha256"] != _plan(("1", "2"))["plan_sha256"]
    result = run_build(_request(input_plan, "b5"), root=tmp_path / "runs")
    assert result.status == "ready_for_review", result.reason
    assert result.acceptance_report is not None
    assert result.acceptance_report.plan_identity_ok is True
    assert result.acceptance_report.plan_mismatch_detail == []
    assert result.can_advance is True and result.published is None


def test_post_run_diff_still_blocks_a_divergent_locked_plan(tmp_path):
    """Defense in depth, unit level: if the executed locked plan ever differs
    from the input, acceptance blocks even with all checks passing."""
    from course_organizer.build import diff_plans, evaluate_acceptance
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    def _plan(units):
        inputs = PlanInputs(
            course_code="CS201", course_name="Data Structures", units=units,
            assignment_ids=("ASG-01",), include_milestones=True,
            steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                           acceptance_criteria=("x",)),),
            verified_facts={"course_code": "CS201", "unit_count": len(units)},
            as_of="2026-10-15")
        return expand(inputs).lock()

    run_plan = _plan(("1",)).model_dump(mode="json")
    input_plan = _plan(("2", "1"))
    assert diff_plans(run_plan, input_plan), "different scopes must differ"
    report = evaluate_acceptance(
        {"draft_grades": [], "evidence_tables": {},
         "validation": {"checks_run": [], "failing": [], "passed": False},
         "plan_locked": run_plan, "attempts": 1, "audit_trail": [{}],
         "units_requested": ["1"], "scope_assignment_ids": ["ASG-01"]},
        plan=input_plan, run_id="x", gate_awaiting=False, published=False)
    assert report.plan_identity_ok is False
    assert report.passed is False


def test_incomplete_report_without_validation_blocks(tmp_path):
    plan = _capture_plan(tmp_path / "cap", "cap7")
    report = evaluate_acceptance(
        {"draft_grades": [], "evidence_tables": {}, "audit_trail": [],
         "plan_locked": plan, "attempts": 0},
        plan=_verified(plan), run_id="x", gate_awaiting=True, published=False)
    assert report.passed is False


def test_published_run_never_advances(tmp_path):
    from course_organizer.evidence import SourceIndex, build_grade_evidence

    plan = _capture_plan(tmp_path / "cap", "cap8")
    index = SourceIndex(
        units={"1": {"course_code": "CS201", "note_count": 1}},
        notes={},
        rubrics={"A1": {"course_code": "CS201", "unit": "1", "criteria": [
            {"description": "work", "max_marks": 20}]}},
        submissions={"S1": {"student_id": "STU1", "course_code": "CS201", "unit": "1",
                            "status": "submitted",
                            "submitted_at": "2026-01-01T00:00:00+00:00"}})
    built = build_grade_evidence(
        {"submission_id": "S1", "assignment_id": "A1"}, index,
        course_scope="CS201", unit_scope=None)
    assert built.supported, built.reasons()
    table = built.model_dump(mode="json")
    report = evaluate_acceptance(
        {"draft_grades": [{"submission_id": "S1", "assignment_id": "A1"}],
         "evidence_tables": {"S1": table},
         "validation": {"checks_run": list(DOC_CHECKS), "failing": [], "passed": True},
         "plan_locked": plan, "attempts": 1, "audit_trail": [{}]},
        plan=_verified(plan), run_id="x", gate_awaiting=False, published=True)
    assert report.passed is False


def _verified(plan):
    from course_organizer.build import verify_input_plan

    return verify_input_plan(plan, plan["course_code"])


# ---------------------------------------------------------------------------
# Gate bypass is structurally impossible
# ---------------------------------------------------------------------------


def test_build_module_has_no_resume_or_publish_path():
    import course_organizer.build as build_mod

    tree = ast.parse(Path(build_mod.__file__).read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                calls.add(func.attr)
    assert not (calls & {"resume_run", "resume", "publish"}), calls


def test_failed_build_never_calls_resume(tmp_path, monkeypatch):
    import course_organizer.graph.runner as runner_mod

    def _bomb(*a, **k):
        raise AssertionError("build must never resume")

    monkeypatch.setattr(runner_mod, "resume_run", _bomb)
    plan = _capture_plan(tmp_path / "cap", "cap9")
    result = run_build(
        _request(plan, "b6", max_attempts=1, fail_until_attempt=99),
        root=tmp_path / "runs")
    assert result.status in ("blocked", "escalated")
    assert result.published is None and result.can_advance is False


def _narrow_plan():
    """A valid locked plan scoped to unit 1 / ASG-01 only (store-consistent)."""
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    inputs = PlanInputs(
        course_code="CS201", course_name="Data Structures", units=("1",),
        assignment_ids=("ASG-01",), include_milestones=True,
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "CS201", "unit_count": 1},
        as_of="2026-10-15")
    return expand(inputs).lock().model_dump(mode="json")


def _scope_state(plan_dump, **over):
    state = {
        "course_code": "CS201",
        "scope_verified_facts": {"units_covered": ["1", "2", "3", "4", "5"]},
        "scope_assignment_ids": ["ASG-01", "ASG-02", "ASG-03"],
        "authorized_plan": plan_dump,
    }
    state.update(over)
    return state


def test_authorized_plan_governs_execution_scope(tmp_path):
    from course_organizer.graph.runner import run_pipeline

    narrowed = _narrow_plan()
    run = run_pipeline(course_code="CS201", run_id="gov1", root=tmp_path,
                       output_dir=tmp_path / "gov1", authorized_plan=narrowed)
    assert run.state["units_requested"] == ["1"]
    assert run.state["scope_assignment_ids"] == ["ASG-01"]
    assert run.state["plan_sha256"] == narrowed["plan_sha256"]
    assert run.state["plan_locked"] == narrowed
    graded_aids = set()
    for g in run.state["draft_grades"]:
        graded_aids.add(g["assignment_id"] if isinstance(g, dict)
                        else g.assignment_id)
    assert graded_aids and graded_aids <= {"ASG-01"}, graded_aids
    assert "workers" in run.nodes_ran()


def test_governed_build_is_ready_with_scoped_evidence(tmp_path):
    narrowed = _narrow_plan()
    result = run_build(_request(narrowed, "gov2"), root=tmp_path / "runs")
    assert result.status == "ready_for_review", result.reason
    assert result.acceptance_report is not None
    assert result.acceptance_report.scope_ok is True
    assert set(result.evidence), "grounding tables must ride the result"
    table_aids = set()
    for sid, table in result.evidence.items():
        for item in table.get("items", []):
            loc = item.get("locator", "")
            if loc.startswith("rubric:"):
                table_aids.add(loc.split(":")[1])
    assert table_aids <= {"ASG-01"}, table_aids


def test_unsupported_unit_blocked_before_workers(tmp_path):
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    inputs = PlanInputs(
        course_code="CS201", course_name="Data Structures", units=("1", "ZZZ"),
        assignment_ids=("ASG-01",), include_milestones=True,
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "CS201", "unit_count": 2},
        as_of="2026-10-15")
    bad = expand(inputs).lock().model_dump(mode="json")
    result = run_build(_request(bad, "gov3"), root=tmp_path / "runs")
    assert result.status == "blocked", result.reason
    assert "authorized plan" in result.reason
    assert result.can_advance is False and result.published is None


def test_unknown_assignment_blocked_before_workers(tmp_path):
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    inputs = PlanInputs(
        course_code="CS201", course_name="Data Structures", units=("1",),
        assignment_ids=("ASG-01", "NOPE-9"), include_milestones=True,
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "CS201", "unit_count": 1},
        as_of="2026-10-15")
    bad = expand(inputs).lock().model_dump(mode="json")
    result = run_build(_request(bad, "gov4"), root=tmp_path / "runs")
    assert result.status == "blocked", result.reason
    assert "authorized plan" in result.reason
    assert result.can_advance is False and result.published is None


def test_plan_node_rejects_without_scope_facts(tmp_path):
    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_plan

    import pytest

    ctx = build_run_context("gov-node", output_dir=tmp_path)
    with pytest.raises(ValueError, match="authorized plan"):
        make_plan(ctx)({"course_code": "CS201", "authorized_plan": _narrow_plan()})


def test_plan_node_rejects_tampered_dict_directly(tmp_path):
    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_plan

    import pytest

    ctx = build_run_context("gov-node2", output_dir=tmp_path)
    tampered = dict(_narrow_plan())
    tampered["units"] = ["1", "2"]
    with pytest.raises(ValueError, match="authorized plan tampered"):
        make_plan(ctx)(_scope_state(tampered))


def test_plan_node_rejects_course_and_authority_mismatch(tmp_path):
    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_plan
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    import pytest

    ctx = build_run_context("gov-node3", output_dir=tmp_path)
    with pytest.raises(ValueError, match="contradictory"):
        make_plan(ctx)(_scope_state(_narrow_plan(), course_code="DBMS402"))
    inputs = PlanInputs(
        course_code="CS201", course_name="Data Structures", units=("1",),
        assignment_ids=("ASG-01",), include_milestones=True, human_authority="dean_smith",
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "CS201", "unit_count": 1},
        as_of="2026-10-15")
    rogue = expand(inputs).lock().model_dump(mode="json")
    with pytest.raises(ValueError, match="unauthorized"):
        make_plan(ctx)(_scope_state(rogue))


def test_direct_runner_call_cannot_bypass_verification(tmp_path):
    from course_organizer.graph.runner import run_pipeline

    import pytest

    tampered = dict(_narrow_plan())
    tampered["units"] = ["1", "2"]
    with pytest.raises(ValueError, match="authorized plan"):
        run_pipeline(course_code="CS201", run_id="gov5", root=tmp_path,
                     output_dir=tmp_path / "gov5", authorized_plan=tampered)


def test_build_refuses_unauthorized_authority_before_running(tmp_path):
    from course_organizer.skills import PlanInputs, expand
    from course_organizer.skills.plan_skill import LabStep

    inputs = PlanInputs(
        course_code="CS201", course_name="Data Structures", units=("1",),
        assignment_ids=("ASG-01",), include_milestones=True, human_authority="dean_smith",
        steps=(LabStep(step_id="S1", name="read", agent="notes", intent="read",
                       acceptance_criteria=("x",)),),
        verified_facts={"course_code": "CS201", "unit_count": 1},
        as_of="2026-10-15")
    rogue = expand(inputs).lock().model_dump(mode="json")
    result = run_build(_request(rogue, "gov6"), root=tmp_path / "runs")
    assert result.status == "refused" and "unauthorized" in result.reason
    assert not (tmp_path / "runs" / "gov6").exists()
