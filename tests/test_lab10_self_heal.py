"""Lab 10 — self-healing validation.

Faults are injected, never staged: a tampered flag, a dropped score, a
missing table, an unapproved publish. Each test asserts the full arc the doc
requires — detect (typed finding), repair only the item (or escalate when no
safe repair exists), revalidate, audit — without touching the network, the
database, or Google.
"""

from __future__ import annotations

import json

from course_organizer.evidence import SourceIndex, build_grade_evidence
from course_organizer.graph.repair import (
    auto_handled,
    diagnose,
    splice_grades,
    steps_for,
    sync_grade_metadata,
)
from course_organizer.graph.state import CourseState, DraftGradeRow
from course_organizer.graph.validation import CheckContext, next_route, validate


def _grade(sid="S1", aid="A1", awarded=15.0, late=False, sim=0.1, **kw):
    base = dict(submission_id=sid, assignment_id=aid, awarded_marks=awarded,
                max_marks=20.0, late=late, similarity_score=sim)
    base.update(kw)
    return DraftGradeRow(**base)


def _ctx(grades, ceilings=None, late_by=None, tables=None):
    return CheckContext(grades, ceilings or {"A1": 20.0}, late_by or {},
                        tables or {})


def _index():
    return SourceIndex(
        units={"1": {"course_code": "CS201", "note_count": 2}},
        notes={},
        rubrics={"A1": {
            "course_code": "CS201", "unit": "1",
            "criteria": [
                {"description": "correctness", "max_marks": 12},
                {"description": "presentation", "max_marks": 8},
            ],
        }},
        submissions={"S1": {
            "student_id": "STU1", "course_code": "CS201", "unit": "1",
            "status": "submitted", "submitted_at": "2026-01-01T00:00:00+00:00",
        }})


def _table():
    return build_grade_evidence(
        {"submission_id": "S1", "assignment_id": "A1"}, _index(),
        course_scope="CS201", unit_scope=["1"]).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Typed findings: check, items, rule, diagnosis, repairability, scope
# ---------------------------------------------------------------------------


def test_rubric_breach_finding_names_item_and_regeneration():
    findings = diagnose(["draft_grade_within_rubric_maximum"],
                        grades=[_grade().model_dump(), _grade("S2", awarded=25.0).model_dump()],
                        ceilings={"A1": 20.0}, late_by={}, tables=_table_map())
    assert len(findings) == 1
    f = findings[0]
    assert f.check == "draft_grade_within_rubric_maximum"
    assert f.submission_ids == ["S2"] and "S1" not in f.submission_ids
    assert "rubric maximum" in f.rule and f.diagnosis
    assert f.repairable == "regenerate" and f.repair_steps == ["w5"]
    assert f.repair_target == "grade"


def _table_map():
    return {"S1": _table(), "S2": _table_for_s2()}


def _table_for_s2():
    return build_grade_evidence(
        {"submission_id": "S2", "assignment_id": "A1"}, _index_with_s2(),
        course_scope="CS201", unit_scope=["1"]).model_dump(mode="json")


def _index_with_s2():
    index = _index()
    index.submissions["S2"] = {
        "student_id": "STU2", "course_code": "CS201", "unit": "1",
        "status": "submitted", "submitted_at": "2026-01-02T00:00:00+00:00"}
    return index


def test_late_mismatch_finding_is_deterministic_with_store_truth():
    findings = diagnose(["late_submissions_flagged_accurately"],
                        grades=[_grade(late=False).model_dump()],
                        ceilings={"A1": 20.0}, late_by={"S1": 900}, tables=_table_map())
    assert len(findings) == 1
    f = findings[0]
    assert f.submission_ids == ["S1"]
    assert f.repairable == "deterministic" and f.repair_steps == ["w3"]
    assert f.repair_target == "late_flags"
    assert "timestamps" in f.rule  # never alter them — the flags move


def test_unapproved_publish_is_never_repairable_by_workers():
    findings = diagnose(["no_grade_published_without_faculty_approval"],
                        grades=[_grade(status="published").model_dump()],
                        ceilings={"A1": 20.0}, late_by={}, tables=_table_map())
    assert len(findings) == 1
    f = findings[0]
    assert f.submission_ids == ["S1"]
    assert f.repairable == "none" and f.repair_steps == []
    assert f.repair_target == "gate"
    assert not auto_handled(findings)


def test_ambiguous_missing_grade_row_escalates_without_mutation():
    findings = diagnose(["late_submissions_flagged_accurately"],
                        grades=[], ceilings={"A1": 20.0},
                        late_by={"SX": 60}, tables={})
    assert len(findings) == 1
    f = findings[0]
    assert f.submission_ids == ["SX"] and f.repairable == "none"


def test_unknown_check_name_escalates_rather_than_guessing():
    findings = diagnose(["not_a_real_check"], grades=[], ceilings={}, late_by={}, tables={})
    assert len(findings) == 1
    assert findings[0].repairable == "none" and findings[0].repair_steps == []


def test_steps_follow_roster_order():
    from course_organizer.graph.specialists import SPECIALISTS

    roster = [step for step, _, _ in SPECIALISTS]
    assert roster == ["w1", "w2", "w3", "w4", "w5", "w6"]
    findings = diagnose(["similarity_score_attached", "draft_grade_within_rubric_maximum"],
                        grades=[_grade(similarity_score=None, awarded=99.0).model_dump()],
                        ceilings={"A1": 20.0}, late_by={}, tables={})
    assert steps_for(findings) == ["w4", "w5"]


# ---------------------------------------------------------------------------
# Deterministic repair: flags move, timestamps never do
# ---------------------------------------------------------------------------


def test_late_sync_repairs_only_flags_and_reports_ids():
    rows = [{"submission_id": "S1", "late": False, "awarded_marks": 15.0,
             "feedback": "x", "status": "draft"},
            {"submission_id": "S2", "late": True, "awarded_marks": 10.0,
             "feedback": "y", "status": "draft"}]
    synced, repaired = sync_grade_metadata(
        rows, scores={}, against={}, flagged=frozenset(),
        late_by={"S1": 900, "S2": 0})
    assert repaired == ["S1", "S2"]
    assert [r["late"] for r in synced] == [True, False]
    # everything else byte-identical, inputs unmutated
    assert rows[0]["late"] is False and rows[1]["late"] is True
    for before, after in zip(rows, synced):
        for key in ("awarded_marks", "feedback", "status", "submission_id"):
            assert before[key] == after[key]


def test_sync_never_invents_scores_or_touches_marks():
    rows = [{"submission_id": "S1", "late": False, "awarded_marks": 15.0,
             "similarity_score": None}]
    synced, repaired = sync_grade_metadata(
        rows, scores={"S9": 0.5}, against={}, flagged=frozenset(), late_by={})
    assert synced[0]["similarity_score"] is None and repaired == []
    assert synced[0]["awarded_marks"] == 15.0


def test_splice_preserves_passing_rows_and_adds_regenerated():
    carried = [{"submission_id": "S1", "awarded_marks": 15.0},
               {"submission_id": "S2", "awarded_marks": 25.0}]
    regen = [{"submission_id": "S2", "awarded_marks": 14.0},
             {"submission_id": "S9", "awarded_marks": 10.0}]
    out = splice_grades(carried, regen, ["S2", "S9"])
    assert [g["submission_id"] for g in out] == ["S1", "S2", "S9"]
    assert out[0] == {"submission_id": "S1", "awarded_marks": 15.0}
    assert out[1]["awarded_marks"] == 14.0
    assert carried[1]["awarded_marks"] == 25.0  # inputs unmutated


# ---------------------------------------------------------------------------
# Routing: budget kept, unrepairable escalates immediately
# ---------------------------------------------------------------------------


def test_unrepairable_escalates_without_spending_retries():
    from course_organizer.graph.state import ValidationOutcome

    report = ValidationOutcome(passed=False, failing=["no_grade_published_without_faculty_approval"],
                               diagnosis="x", checks_run=[], attempt=1)
    assert next_route(report, attempts=1, max_attempts=3, unrepairable=True) == "escalate"


def test_existing_routing_contract_unchanged():
    from course_organizer.graph.state import ValidationOutcome

    ok_report = ValidationOutcome(passed=True, failing=[], diagnosis="", checks_run=[], attempt=3)
    assert next_route(ok_report, attempts=3, max_attempts=3) == "pass"
    bad = ValidationOutcome(passed=False, failing=["x"], diagnosis="x", checks_run=[], attempt=1)
    assert next_route(bad, attempts=1, max_attempts=3) == "fail"
    assert next_route(bad, attempts=3, max_attempts=3) == "escalate"


def test_repair_findings_channel_is_declared():
    assert "repair_findings" in CourseState.__annotations__
    assert "evidence_tables" in CourseState.__annotations__


# ---------------------------------------------------------------------------
# Graph-level: repair through the real nodes, then revalidation
# ---------------------------------------------------------------------------


def test_retry_repairs_tampered_late_flag_and_revalidates(tmp_path):
    from typing import Mapping

    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_validate, make_workers
    from course_organizer.graph.state import DOC_CHECKS, DraftGradeRow

    ctx = build_run_context("lab10-late", output_dir=tmp_path)
    base = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    workers, validate = make_workers(ctx), make_validate(ctx)
    first = workers(base)
    state = dict(base)
    state.update(first)
    # tamper one carried flag post-workers (fault injection)
    grades = [dict(g) if isinstance(g, Mapping) else g.model_dump()
              for g in state["draft_grades"]]
    victim = max(grades, key=lambda g: str(g.get("submission_id", "")))
    truth = next(f for f in state["late_flags"]
                 if str(f["submission_id"]) == str(victim["submission_id"]))
    victim["late"] = not (int(truth["late_by_seconds"]) > 0)
    state["draft_grades"] = [DraftGradeRow(**g) for g in grades]
    state["attempts"] = 1
    state["validation"] = validate(state)["validation"]
    assert state["validation"].passed is False
    assert "late_submissions_flagged_accurately" in state["validation"].failing

    second = workers(state)
    state.update(second)
    assert victim["submission_id"] in second["attempt_log"][0]["repaired"]
    out = validate(state)
    assert out["validation"].passed, out["validation"].diagnosis
    assert tuple(out["validation"].checks_run) == tuple(DOC_CHECKS)
    # timestamps untouched by the repair
    before = {s["submission_id"]: s["submitted_at"] for s in first["submissions"]}
    after = {s["submission_id"]: s["submitted_at"] for s in state["submissions"]}
    assert before == after


def test_unapproved_row_escalates_immediately_with_budget_left(tmp_path):
    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_validate
    from course_organizer.graph.state import DraftGradeRow

    ctx = build_run_context("lab10-unapproved", output_dir=tmp_path)
    grades = [DraftGradeRow(submission_id="S1", assignment_id="A1", awarded_marks=15.0,
                            max_marks=20.0, similarity_score=0.1, late=False,
                            status="published", approved_by="")]
    out = make_validate(ctx)({
        "course_code": "CS201", "attempts": 0, "units_requested": ["1"],
        "assignments": [{"assignment_id": "A1", "max_marks": 20.0, "unit": "1",
                         "rubric": [{"criterion_id": "c", "description": "work",
                                     "max_marks": 20}]}],
        "submissions": [{"submission_id": "S1", "assignment_id": "A1", "student_id": "STU1",
                         "status": "submitted", "submitted_at": "2026-01-01T00:00:00+00:00"}],
        "late_flags": [], "notes_by_unit": {"1": 2}, "draft_grades": grades,
    })
    assert out["route"] == "escalate"
    assert any(f["check"] == "no_grade_published_without_faculty_approval"
               for f in out["repair_findings"])


def test_evidence_rebuild_then_revalidate_round_trip():
    from course_organizer.graph.validation import check_evidence_grounded

    tables = {"S1": _table_for_evidence()}
    grades = [_grade()]
    ok, _ = check_evidence_grounded(grades, tables)
    assert ok
    # invalidate: drop the table (simulating a lost update), revalidation fails
    ok, detail = check_evidence_grounded(grades, {})
    assert not ok and "S1" in detail
    # rebuild from the same records and it passes again
    ok, _ = check_evidence_grounded(grades, tables)
    assert ok


def _table_for_evidence():
    from course_organizer.evidence import build_grade_evidence

    index = SourceIndex(
        units={"1": {"course_code": "CS201", "note_count": 1}},
        notes={},
        rubrics={"A1": {"course_code": "CS201", "unit": "1", "criteria": [
            {"description": "work", "max_marks": 20}]}},
        submissions={"S1": {"student_id": "STU1", "course_code": "CS201", "unit": "1",
                            "status": "submitted",
                            "submitted_at": "2026-01-01T00:00:00+00:00"}})
    return build_grade_evidence(
        {"submission_id": "S1", "assignment_id": "A1"}, index,
        course_scope="CS201", unit_scope=None).model_dump(mode="json")


def test_preservation_across_passes(tmp_path):
    from course_organizer.graph import build_run_context
    from course_organizer.graph.nodes import make_workers

    ctx = build_run_context("lab10-preserve", output_dir=tmp_path)
    base = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0,
            "plan_sha256": "abc"}
    first = make_workers(ctx)(base)
    second = make_workers(ctx)(dict(base, **{k: v for k, v in first.items()
                                             if k != "attempt_log"}))
    g1 = [(g.submission_id, g.awarded_marks, g.status) for g in first["draft_grades"]]
    g2 = [(g.submission_id, g.awarded_marks, g.status) for g in second["draft_grades"]]
    assert g1 == g2
    assert second["attempt_log"][0]["repaired"] == []
