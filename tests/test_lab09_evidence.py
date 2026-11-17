"""Lab 9 — grounding and evidence.

Every test here is about one rule: no candidate advances on an ungrounded
claim. Nothing touches the network, the database, or Google — sources are
synthetic dicts with real ids, and the one integration point (the graph's
validate node) is covered by the Lab 7 runner suite running the 5th check.
"""

from __future__ import annotations

import json

import pytest

from course_organizer.evidence import (
    CandidateEvidence,
    ClaimEvidence,
    Requester,
    SourceIndex,
    build_evidence_table,
    build_grade_evidence,
    content_hash_of,
    parse_locator,
)
from course_organizer.graph.state import DOC_CHECKS
from course_organizer.graph.validation import (
    CHECKS,
    CheckContext,
    check_evidence_grounded,
    validate,
)


def _index() -> SourceIndex:
    """Synthetic but fully-specified sources. Ids are arbitrary; every locator
    below resolves against exactly these records, never invented ones."""
    return SourceIndex(
        units={
            "1": {"course_code": "CS201", "note_count": 3},
            "2": {"course_code": "CS201", "note_count": 1},
        },
        notes={
            "N1": {
                "course_code": "CS201", "unit": "1", "current_version": 2,
                "versions": {
                    1: "linked lists: nodes hold data and a next pointer",
                    2: "linked lists: nodes hold data and a next pointer; sentinel edition",
                },
                "hashes": {2: content_hash_of(
                    "linked lists: nodes hold data and a next pointer; sentinel edition")},
            },
        },
        rubrics={
            "A1": {
                "course_code": "CS201", "unit": "1",
                "criteria": [
                    {"description": "correctness of the implementation", "max_marks": 12},
                    {"description": "clarity of the explanation", "max_marks": 8},
                ],
            },
        },
        submissions={
            "S1": {"student_id": "STU1", "course_code": "CS201", "unit": "1",
                   "status": "submitted", "submitted_at": "2026-01-01T00:00:00+00:00"},
        },
    )


FACULTY = Requester(role="faculty")
OWNER = Requester(role="student", student_id="STU1")
STRANGER = Requester(role="student", student_id="STU2")


def _grade_claims() -> list[ClaimEvidence]:
    return [
        ClaimEvidence(candidate_id="S1", claim="submission S1 collected",
                      locator="submission:S1", excerpt="status submitted"),
        ClaimEvidence(candidate_id="S1", claim="criterion carries max 12",
                      locator="rubric:A1:criterion:0",
                      excerpt="correctness of the implementation"),
        ClaimEvidence(candidate_id="S1", claim="unit 1 in scope",
                      locator="unit:1",
                      excerpt="unit 1 in CS201 with 3 retained note(s)"),
    ]


# ---------------------------------------------------------------------------
# The locator vocabulary
# ---------------------------------------------------------------------------


def test_all_four_locator_forms_parse():
    assert parse_locator("unit:1").kind == "syllabus"
    assert parse_locator("note:N1:version:2").version == 2
    assert parse_locator("rubric:A1:criterion:0").index == 0
    assert parse_locator("submission:S1").target_id == "S1"


def test_malformed_locators_raise_with_the_vocabulary():
    for bad in ["", "rubric:A1", "note:N1", "submission:", "unit:",
                "similarity:0.9", "note:N1:version:x", "rubric:A1:criterion:-1"]:
        with pytest.raises(ValueError):
            parse_locator(bad)


# ---------------------------------------------------------------------------
# Valid evidence
# ---------------------------------------------------------------------------


def test_a_fully_grounded_table_is_supported_with_version_and_hash():
    table = build_evidence_table(
        "S1",
        _grade_claims() + [ClaimEvidence(
            candidate_id="S1", claim="notes current",
            locator="note:N1:version:2",
            excerpt="sentinel edition")],
        _index(), requester=FACULTY, course_scope="CS201", unit_scope=["1", "2"])
    assert table.supported, table.reasons()
    note_item = next(i for i in table.items if i.source_type == "note")
    assert note_item.source_version == "2"
    assert note_item.content_hash == content_hash_of(
        "linked lists: nodes hold data and a next pointer; sentinel edition")


# ---------------------------------------------------------------------------
# Missing / invalid / out-of-scope / stale / inaccessible / unsupported
# ---------------------------------------------------------------------------


def test_unknown_submission_is_missing_not_invented():
    table = build_evidence_table(
        "SX", [ClaimEvidence(candidate_id="SX", claim="collected",
                             locator="submission:SX", excerpt="status submitted")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "unknown submission 'SX'" in table.reasons()[0]


def test_record_from_another_course_is_out_of_scope():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="unit 9",
                             locator="unit:9", excerpt="whatever")],
        SourceIndex(units={"9": {"course_code": "CS999", "note_count": 1}}),
        requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "out of scope" in table.reasons()[0] and "CS999" in table.reasons()[0]


def test_unit_narrowing_rejects_units_outside_the_locked_scope():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="unit 1",
                             locator="unit:1",
                             excerpt="unit 1 in CS201 with 3 retained note(s)")],
        _index(), requester=FACULTY, course_scope="CS201", unit_scope=["2"])
    assert not table.supported
    assert "outside the locked scope" in table.reasons()[0]


def test_stranger_student_is_denied_with_no_leaked_excerpt():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="collected",
                             locator="submission:S1", excerpt="status submitted")],
        _index(), requester=STRANGER, course_scope="CS201")
    assert not table.supported
    item = table.items[0]
    assert "inaccessible" in item.verdict.reason
    assert item.excerpt == "" and "STU1" not in item.verdict.reason


def test_owner_student_and_faculty_both_resolve():
    for requester in (OWNER, FACULTY):
        table = build_evidence_table(
            "S1", [ClaimEvidence(candidate_id="S1", claim="collected",
                                 locator="submission:S1", excerpt="status submitted")],
            _index(), requester=requester, course_scope="CS201")
        assert table.supported, table.reasons()


def test_superseded_note_version_is_stale():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="old notes",
                             locator="note:N1:version:1",
                             excerpt="nodes hold data and a next pointer")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "stale version" in table.reasons()[0] and "version 2" in table.reasons()[0]


def test_old_version_resolves_when_currency_is_not_required():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="old notes",
                             locator="note:N1:version:1",
                             excerpt="nodes hold data and a next pointer")],
        _index(), requester=FACULTY, course_scope="CS201", require_current=False)
    assert table.supported, table.reasons()


def test_unretained_note_version_is_an_invalid_reference():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="notes",
                             locator="note:N1:version:9", excerpt="anything")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "invalid reference" in table.reasons()[0]


def test_rubric_index_past_the_end_is_an_invalid_reference():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="criterion",
                             locator="rubric:A1:criterion:7", excerpt="anything")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "invalid reference" in table.reasons()[0]


def test_similarity_alone_never_grounds_a_claim():
    """A score is metadata about a comparison, not a source: there is no
    locator form for it, and a score-flavoured excerpt matches no record."""
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="copied",
                             locator="submission:S1",
                             excerpt="similarity 0.97 proves copying")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "not evidence" in table.reasons()[0]


def test_empty_excerpt_is_unsupported():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="collected",
                             locator="submission:S1", excerpt="  ")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported


def test_no_claims_is_missing_evidence():
    table = build_evidence_table("S1", [], _index(), requester=FACULTY,
                                 course_scope="CS201")
    assert not table.supported
    assert "missing evidence" in table.reasons()[0]


def test_one_bad_item_fails_the_whole_table():
    table = build_evidence_table(
        "S1", _grade_claims() + [ClaimEvidence(
            candidate_id="S1", claim="ghost", locator="submission:SX",
            excerpt="status submitted")],
        _index(), requester=FACULTY, course_scope="CS201", unit_scope=["1"])
    assert not table.supported
    assert len(table.reasons()) == 1 and "SX" in table.reasons()[0]


def test_malformed_locator_in_a_table_is_a_verdict_not_a_crash():
    table = build_evidence_table(
        "S1", [ClaimEvidence(candidate_id="S1", claim="x",
                             locator="rubric:A1", excerpt="y")],
        _index(), requester=FACULTY, course_scope="CS201")
    assert not table.supported
    assert "malformed locator" in table.reasons()[0]


# ---------------------------------------------------------------------------
# The check itself, including checkpoint-shaped input
# ---------------------------------------------------------------------------


def _row(**kw):
    from course_organizer.graph.state import DraftGradeRow

    base = dict(submission_id="S1", assignment_id="A1", awarded_marks=15.0,
                max_marks=20.0, similarity_score=0.1)
    base.update(kw)
    return DraftGradeRow(**base)


def _tables_for(*sids: str) -> dict:
    index = _index()
    out = {}
    for sid in sids:
        out[sid] = build_grade_evidence(
            {"submission_id": sid, "assignment_id": "A1"}, index,
            course_scope="CS201", unit_scope=["1"]).model_dump(mode="json")
    return out


def test_check_fails_closed_with_no_table():
    from course_organizer.graph.validation import check_evidence_grounded

    ok, detail = check_evidence_grounded([_row()], {})
    assert not ok and "no evidence table" in detail and "S1" in detail


def test_check_rejects_garbage_tables_without_raising():
    from course_organizer.graph.validation import check_evidence_grounded

    ok, detail = check_evidence_grounded([_row()], {"S1": {"bogus": 1}})
    assert not ok and "unreadable" in detail


def test_check_accepts_checkpoint_json_round_trips():
    from course_organizer.graph.validation import check_evidence_grounded

    tables = json.loads(json.dumps(_tables_for("S1")))
    ok, detail = check_evidence_grounded([_row()], tables)
    assert ok, detail
    assert "grounded" in detail


def test_check_names_the_ungrounded_submission():
    from course_organizer.graph.validation import check_evidence_grounded

    tables = _tables_for("S1")
    ok, detail = check_evidence_grounded([_row(), _row(submission_id="S9")], tables)
    assert not ok and "S9" in detail and "S1" not in detail


# ---------------------------------------------------------------------------
# Wired into validate() as the fifth check
# ---------------------------------------------------------------------------


def test_evidence_grounded_is_the_fifth_doc_check():
    assert DOC_CHECKS[-1] == "evidence_grounded"
    assert "evidence_grounded" in CHECKS
    assert len(DOC_CHECKS) == 5


def test_validate_fails_a_grade_with_no_table_naming_the_check():
    ctx = CheckContext([_row()], {"A1": 20.0}, {}, {})
    report = validate(ctx, attempt=1)
    assert not report.passed
    assert "evidence_grounded" in report.failing
    assert "evidence_grounded" in report.diagnosis
    assert "cite a real" in report.diagnosis


def test_validate_passes_a_grounded_grade_with_five_checks_run():
    tables = _tables_for("S1")
    ctx = CheckContext([_row()], {"A1": 20.0}, {}, tables)
    report = validate(ctx, attempt=1)
    assert report.passed, report.diagnosis
    assert tuple(report.checks_run) == DOC_CHECKS
    assert len(report.checks_run) == 5
