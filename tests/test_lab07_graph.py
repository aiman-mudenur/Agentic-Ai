"""Lab 7 tests: state, doc 2.7's validation, the six specialists, escalation,
and the compiled topology.

The organising idea of this file is that **nothing is asserted against a
declared constant when it can be asserted against the compiled graph.** Doc
2.10's pseudocode and this module both name the same seven nodes, but if the
wiring were wrong the constants would still agree with each other. So the
topology tests read ``compiled.get_graph()``, and the constants exist only to
say what the document says.

Every test runs against the seed corpus with the scripted LLM backend, so no
API key and no network. That is a property of the earlier labs' fixtures and
this file inherits it deliberately: a reviewer must be able to re-run the
acceptance criteria without credentials.
"""

from __future__ import annotations

import pytest

from course_organizer.graph import (
    APPROVING_DECISIONS,
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
    DECISION_REFUSE,
    DOC_BACK_EDGE,
    DOC_CHAIN,
    DOC_CHECKS,
    DOC_NODE_CHAIN,
    GATE_ROUTES,
    MAX_ATTEMPTS,
    RECURSION_BACKSTOP,
    RUBRIC_TOLERANCE,
    ROUTE_APPROVED,
    ROUTE_REFUSED,
    ROUTE_RETRY,
    SPECIALIST_INTENTS,
    SPECIALIST_NAMES,
    VALIDATE_ROUTES,
    CheckContext,
    CourseStatus,
    DraftGradeRow,
    Escalation,
    FacultyDecision,
    GateRequest,
    ValidationOutcome,
    build_escalation,
    build_graph,
    build_nodes,
    build_run_context,
    check_draft_grade_within_rubric_maximum,
    check_late_submissions_flagged_accurately,
    check_no_grade_published_without_faculty_approval,
    check_similarity_score_attached,
    describe_topology,
    explain_failure,
    graph_edges,
    missing_checks,
    next_route,
    run_specialists,
)
from course_organizer.graph.specialists import (
    _jaccard,
    _shingles,
    _split_criteria,
    CRITERION_WEIGHTS,
    SIMILARITY_FLAG_THRESHOLD,
)
from course_organizer.graph.validation import CHECKS

# ---------------------------------------------------------------------------
# The doc's own vocabulary
# ---------------------------------------------------------------------------


def test_the_seven_node_chain_is_the_docs_chain():
    assert DOC_NODE_CHAIN == DOC_CHAIN
    assert DOC_CHAIN == (
        "ingest",
        "plan",
        "workers",
        "validate",
        "assemble",
        "gate",
        "publish",
    )


def test_the_back_edge_is_validate_back_to_workers():
    assert DOC_BACK_EDGE == ("validate", "workers")


def test_escalate_is_the_one_node_the_doc_does_not_draw():
    """Doc 2.12 line 825 requires it; doc 2.10 line 696 omits it.

    Both are true and the difference is the point: the pseudocode draws the
    happy paths, the guardrail table states what happens when none of them
    work. So ``escalate`` exists and is deliberately not in the chain.
    """
    assert "escalate" not in DOC_CHAIN
    assert VALIDATE_ROUTES["escalate"] == "escalate"
    # GATE_ROUTES is keyed by *route label*, not by the decision string.
    assert GATE_ROUTES[ROUTE_REFUSED] == "escalate"
    assert GATE_ROUTES[ROUTE_APPROVED] == "publish"


def test_max_attempts_is_the_doc_name_for_the_bound():
    assert MAX_ATTEMPTS >= 2
    # The recursion backstop must be above anything the design can produce, or
    # "the loop terminated" would not distinguish the design stopping it from
    # the framework refusing to continue.
    assert RECURSION_BACKSTOP > 2 + 2 * (MAX_ATTEMPTS + 1) + 4


# ---------------------------------------------------------------------------
# Topology, read off the compiled graph
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    return build_run_context("test-topology", output_dir=tmp_path)


@pytest.fixture
def compiled(ctx):
    return build_graph(ctx)


def _edges(graph):
    return {(s, t) for s, t, _ in graph_edges(graph)}


def test_every_doc_node_is_compiled_into_the_graph(compiled):
    ids = {str(n) for n in compiled.get_graph().nodes}
    for name in DOC_CHAIN + ("escalate",):
        assert name in ids, f"{name} missing from the compiled graph"


def test_the_forward_chain_matches_the_doc(compiled):
    present = _edges(compiled)
    for source, target in (
        ("__start__", "ingest"),
        ("ingest", "plan"),
        ("plan", "workers"),
        ("assemble", "gate"),
        ("publish", "__end__"),
        ("escalate", "__end__"),
    ):
        assert (source, target) in present


def test_workers_points_at_validate_which_is_what_makes_the_back_edge_loop(compiled):
    """The edge most likely to be missing, and the one whose absence is silent.

    Verified by probe against langgraph 1.2.12: with only the conditional
    ``validate --fail--> workers`` edge and no forward ``workers -> validate``,
    a permanently failing run converges after a single attempt and no error is
    raised. So this asserts the forward edge exists.
    """
    assert ("workers", "validate") in _edges(compiled)


def test_validate_can_reach_workers_assemble_and_escalate(compiled):
    targets = {t for s, t, c in graph_edges(compiled) if s == "validate"}
    assert {"workers", "assemble", "escalate"} <= targets


def test_the_three_validate_destinations_are_conditional(compiled):
    conditional = {
        t for s, t, c in graph_edges(compiled) if s == "validate" and c
    }
    assert {"workers", "assemble", "escalate"} <= conditional


def test_gate_can_reach_publish_and_escalate(compiled):
    targets = {t for s, t, c in graph_edges(compiled) if s == "gate"}
    assert {"publish", "escalate"} <= targets


def test_the_gate_is_reachable_and_publish_is_only_reachable_through_it(compiled):
    """Doc 2.12 line 830: nothing publishes without passing the gate."""
    into_publish = {s for s, t, _ in graph_edges(compiled) if t == "publish"}
    assert into_publish == {"gate"}


def test_the_topology_renderer_is_deterministic_and_ascii(compiled):
    """Two renders are byte-identical, so it is safe in an evidence file."""
    assert describe_topology(compiled) == describe_topology(compiled)
    assert all(ord(ch) < 128 for line in describe_topology(compiled) for ch in line)


def test_the_topology_renderer_reports_the_back_edge(compiled):
    text = "\n".join(describe_topology(compiled))
    assert "workers -> validate" in text
    assert "validate -> workers" in text


def test_build_nodes_supplies_every_node_the_builder_wires(compiled):
    ids = {str(n) for n in compiled.get_graph().nodes}
    built = set(build_nodes(ctx)) if ctx is not None else set()
    assert built <= ids
    assert built == set(DOC_CHAIN) | {"escalate"}


# ---------------------------------------------------------------------------
# Doc 2.7's four checks
# ---------------------------------------------------------------------------


def test_all_four_doc_checks_are_implemented():
    assert missing_checks() == ()
    assert tuple(CHECKS) == DOC_CHECKS
    assert len(DOC_CHECKS) == 4


def test_the_rubric_tolerance_is_zero_because_the_doc_says_never():
    assert RUBRIC_TOLERANCE == 0.0


def _row(**kw):
    base = dict(
        submission_id="S1",
        assignment_id="A1",
        awarded_marks=15.0,
        max_marks=20.0,
    )
    base.update(kw)
    return DraftGradeRow(**base)


def test_a_grade_at_exactly_the_maximum_is_allowed():
    ok, detail = check_draft_grade_within_rubric_maximum(
        [_row(awarded_marks=20.0)], {"A1": 20.0}
    )
    assert ok
    assert "within" in detail


def test_a_grade_even_fraction_over_the_maximum_fails_and_names_both_numbers():
    ok, detail = check_draft_grade_within_rubric_maximum(
        [_row(awarded_marks=20.01)], {"A1": 20.0}
    )
    assert not ok
    assert "20.01" in detail and "20" in detail and "S1" in detail


def test_an_empty_grade_set_fails_the_rubric_check():
    ok, detail = check_draft_grade_within_rubric_maximum([], {})
    assert not ok
    assert "no draft grades" in detail


def test_late_flags_are_checked_against_the_store_not_against_themselves(specialists_ctx):
    ok, detail = check_late_submissions_flagged_accurately(
        [_row(late=True)], {"S1": 3600}
    )
    assert ok
    assert "agree with the store" in detail
    assert "1 flagged late" in detail


def test_a_late_mismatch_reports_the_stores_own_seconds(specialists_ctx):
    """The failing branch must name the number, or the check is not actionable."""
    ok, detail = check_late_submissions_flagged_accurately(
        [_row(late=False)], {"S1": 3600}
    )
    assert not ok
    assert "3600" in detail


def test_a_mislabelled_late_flag_is_caught():
    ok, detail = check_late_submissions_flagged_accurately(
        [_row(late=False)], {"S1": 3600}
    )
    assert not ok
    assert "on time" in detail


def test_a_false_positive_late_flag_is_caught_too():
    """Accuracy is two-sided; the false positive is the half usually skipped."""
    ok, detail = check_late_submissions_flagged_accurately(
        [_row(late=True)], {"S1": 0}
    )
    assert not ok
    assert "late" in detail


def test_a_submission_missing_from_the_grades_is_a_late_failure():
    ok, detail = check_late_submissions_flagged_accurately([_row()], {"S9": 10})
    assert not ok
    assert "S9" in detail


def test_similarity_check_demands_a_score_not_a_low_one():
    """Doc 2.12 line 831: the check asks whether a score *exists*.

    A score of 0.97 passes. The check does not judge it -- flagging is the
    similarity agent's job and the faculty's to resolve, so the count in the
    detail is read from ``similarity_flagged``, never recomputed here.
    """
    ok, detail = check_similarity_score_attached(
        [_row(similarity_score=0.97, similarity_flagged=True)]
    )
    assert ok
    assert "0.97" not in detail
    assert "1 flagged" in detail


def test_an_absent_similarity_score_is_not_a_zero_score():
    ok, detail = check_similarity_score_attached([_row(similarity_score=None)])
    assert not ok
    assert "no similarity score" in detail


def test_every_draft_passes_the_approval_check_before_the_gate():
    ok, detail = check_no_grade_published_without_faculty_approval([_row()])
    assert ok
    assert "still a draft" in detail


def test_a_published_row_with_no_approver_fails_the_approval_check():
    ok, detail = check_no_grade_published_without_faculty_approval(
        [_row(status="published")]
    )
    assert not ok
    assert "nobody's name" in detail


def test_a_published_row_naming_an_approver_passes():
    ok, _ = check_no_grade_published_without_faculty_approval(
        [_row(status="published", approved_by="course_faculty")]
    )
    assert ok


# ---------------------------------------------------------------------------
# The diagnosis, which the escalation depends on
# ---------------------------------------------------------------------------


def test_a_diagnosis_names_the_check_the_value_and_the_remedy():
    ctx = CheckContext([_row(awarded_marks=25.0)], {"A1": 20.0}, {})
    from course_organizer.graph.validation import validate

    report = validate(ctx, attempt=1)
    assert not report.passed
    assert "draft_grade_within_rubric_maximum" in report.diagnosis
    assert "25" in report.diagnosis and "20" in report.diagnosis
    assert "re-score" in report.diagnosis


def test_a_passing_report_carries_no_diagnosis():
    from course_organizer.graph.validation import validate

    ctx = CheckContext(
        [_row(similarity_score=0.1)], {"A1": 20.0}, {}
    )
    report = validate(ctx, attempt=1)
    assert report.passed
    assert report.diagnosis == ""
    assert report.failing == []


def test_a_diagnosis_never_doubles_a_full_stop():
    """Bug 38 in Lab 6 was a doubled full stop in a diagnosis string.

    Re-checked here because the same prose helper is reused, and a doubled
    stop is a sign the sentence was assembled by concatenation.
    """
    from course_organizer.graph.validation import validate

    ctx = CheckContext(
        [_row(awarded_marks=99.0, similarity_score=None, late=False)],
        {"A1": 20.0},
        {"S1": 900},
    )
    text = validate(ctx, attempt=2).diagnosis
    assert ".." not in text
    assert "Attempt 2" in text


def test_a_diagnosis_never_opens_with_whitespace():
    """Bug: the retry ordinal was built as ``" Attempt 3 "``.

    The leading space was baked into the fragment rather than placed at the
    join, so every diagnosis for a retry opened with a space -- and the
    escalation, which interpolates it as ``"...forever. {diagnosis}"``, then
    rendered a double space. A string that starts with whitespace is the
    signature of a concatenation artefact, so it is worth failing on directly
    rather than only through the rendered output.
    """
    from course_organizer.graph.validation import validate

    ctx = CheckContext(
        [_row(awarded_marks=99.0, similarity_score=None, late=False)],
        {"A1": 20.0},
        {"S1": 900},
    )
    for attempt in (1, 2, 3):
        text = validate(ctx, attempt=attempt).diagnosis
        assert text == text.strip(), f"attempt {attempt} diagnosis is padded: {text!r}"


def test_an_escalation_diagnosis_contains_no_doubled_space():
    """The consumer's half of the bug: ``"...forever. {diagnosis}"``.

    The escalation interpolates the diagnosis after a full stop, so a diagnosis
    that opens with a space becomes a double space in the text a human reads.
    """
    from course_organizer.graph.validation import validate

    ctx = CheckContext(
        [_row(awarded_marks=99.0, similarity_score=None, late=False)],
        {"A1": 20.0},
        {"S1": 900},
    )
    esc = build_escalation(
        {"validation": validate(ctx, attempt=3), "attempts": 3,
         "draft_grades": [], "assignments": []},
        reason="retry_budget_exhausted", max_attempts=3,
    )
    assert "  " not in esc.diagnosis, esc.diagnosis


def test_explain_failure_on_an_empty_list_returns_empty():
    assert explain_failure([], []) == ""


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


def _report(passed: bool) -> ValidationOutcome:
    return ValidationOutcome(passed=passed, checks_run=list(DOC_CHECKS))


def test_a_passing_report_routes_forward():
    assert next_route(_report(True), attempts=1, max_attempts=3) == "pass"


def test_a_failing_report_routes_back_to_workers_while_budget_remains():
    assert next_route(_report(False), attempts=1, max_attempts=3) == "fail"
    assert next_route(_report(False), attempts=2, max_attempts=3) == "fail"


def test_a_failing_report_escalates_once_the_budget_is_spent():
    assert next_route(_report(False), attempts=3, max_attempts=3) == "escalate"
    assert next_route(_report(False), attempts=9, max_attempts=3) == "escalate"


def test_passing_on_the_last_permitted_attempt_still_advances():
    """The order in ``next_route`` is the whole point of this test.

    Checking the budget before ``passed`` would fail a run that succeeded
    exactly at the bound -- the worst possible outcome, because the work was
    good and the system would escalate anyway.
    """
    assert next_route(_report(True), attempts=3, max_attempts=3) == "pass"
    assert next_route(_report(True), attempts=99, max_attempts=3) == "pass"


# ---------------------------------------------------------------------------
# Faculty decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision,expected",
    [
        (DECISION_APPROVE, True),
        (DECISION_EDIT, True),
        (DECISION_OVERRIDE, True),
        (DECISION_REFUSE, False),
    ],
)
def test_only_a_refusal_blocks_publication(decision, expected):
    assert FacultyDecision(decision=decision).approved is expected


def test_the_three_approving_answers_are_exactly_the_docs_three():
    assert set(APPROVING_DECISIONS) == {
        DECISION_APPROVE,
        DECISION_EDIT,
        DECISION_OVERRIDE,
    }


def test_a_decision_rejects_an_unknown_answer():
    """A typo must not silently become a refusal.

    Without the validator, ``"aprove"`` would leave ``approved`` False and the
    run would escalate recording that the reviewer *declined* -- the worst
    possible reading of a typo, because nothing in the audit would show the
    reviewer had actually tried to approve.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FacultyDecision(decision="aprove")


def test_a_decision_normalises_case_and_whitespace():
    """A near-miss is honoured rather than rejected.

    Case and stray whitespace are handled for the human, not against them: a
    reviewer who types ``Approve`` meant to approve, and a validator that threw
    on that would be pushing the cost of a capital letter onto the one person
    the doc says is the final authority.
    """
    assert FacultyDecision(decision="  APPROVE ").decision == "approve"
    assert FacultyDecision(decision=" Refuse ").decision == "refuse"
    assert FacultyDecision(decision="approve").approved is True


# ---------------------------------------------------------------------------
# The six Phase-A specialists
# ---------------------------------------------------------------------------


def test_there_are_six_specialists_in_doc_roster_order():
    assert len(SPECIALIST_NAMES) == 6
    assert SPECIALIST_NAMES == (
        "Notes Repository Agent",
        "Assignment Distribution Agent",
        "Submission Collection Agent",
        "Plagiarism/Similarity Agent",
        "Grading Assistance Agent",
        "Project Milestone Tracking Agent",
    )


def test_each_specialist_has_a_step_id_agent_intent_and_criterion():
    assert len(SPECIALIST_INTENTS) == 6
    for step_id, agent, intent, criterion in SPECIALIST_INTENTS:
        assert step_id.startswith("w")
        assert agent in SPECIALIST_NAMES
        assert intent and criterion


@pytest.fixture
def specialists_ctx(tmp_path):
    return build_run_context("test-specialists", output_dir=tmp_path)


def test_the_specialists_run_in_roster_order_and_each_audits_itself(specialists_ctx):
    from course_organizer.graph.runner import run_pipeline

    # A full pass through `workers`, isolated.
    state = {
        "course_code": "CS201",
        "course_name": "Data Structures",
        "attempts": 0,
        "fail_until_attempt": 0,
    }
    results, merged = run_specialists(state, specialists_ctx)
    assert [r.agent for r in results] == list(SPECIALIST_NAMES)
    for r in results:
        entry = r.audit()
        assert entry["agent"] in SPECIALIST_NAMES
        assert entry["ok"] is True
        assert entry["summary"]


def test_every_specialist_feeds_the_next_one(specialists_ctx):
    """The bug this asserts: six specialists reading one input snapshot.

    ``similarity`` computes the scores and ``grading`` attaches them. If every
    specialist saw only the node's input state, the pipeline would silently
    lose the scores between two specialists that both reported success.
    """
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, merged = run_specialists(state, specialists_ctx)
    assert merged["submissions"], "collection produced nothing"
    assert merged["similarity_scores"], "similarity produced no scores"
    assert merged["assignments"], "distribution produced no briefs"
    scores = merged["similarity_scores"]
    for grade in merged["draft_grades"]:
        assert grade.similarity_score == scores[grade.submission_id]


def test_runs_are_deterministic_for_the_same_course(specialists_ctx):
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, first = run_specialists(state, specialists_ctx)
    _, second = run_specialists(state, specialists_ctx)
    assert first["similarity_scores"] == second["similarity_scores"]
    assert [g.awarded_marks for g in first["draft_grades"]] == [
        g.awarded_marks for g in second["draft_grades"]
    ]


def test_only_one_specialist_can_be_selected(specialists_ctx):
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    results, merged = run_specialists(state, specialists_ctx, only=["w1"])
    assert [r.step_id for r in results] == ["w1"]
    assert "notes_by_unit" in merged
    assert "draft_grades" not in merged


# ---------------------------------------------------------------------------
# The similarity measure is a real measurement
# ---------------------------------------------------------------------------


def test_identical_text_scores_one():
    a = _shingles("the quick brown fox jumps over the lazy dog")
    assert _jaccard(a, a) == 1.0


def test_unrelated_text_scores_near_zero():
    a = _shingles("linked lists append walk the list to the last node")
    b = _shingles("binary search trees balance the root on every insert")
    assert _jaccard(a, b) == 0.0


def test_an_empty_document_scores_zero_rather_than_dividing_by_zero():
    assert _jaccard(_shingles(""), frozenset()) == 0.0


def test_the_measure_is_deterministic():
    text = "reverse a singly linked list in place with three pointers"
    assert _shingles(text) == _shingles(text)


def test_stopwords_do_not_create_false_similarity():
    a = _shingles("this is the answer that was given in the paper")
    b = _shingles("that is this answer paper was in given")
    # Same content words in a different order is still zero trigram overlap.
    assert _jaccard(a, b) == 0.0


def test_every_submission_gets_a_similarity_score(specialists_ctx):
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, merged = run_specialists(state, specialists_ctx)
    scores = merged["similarity_scores"]
    assert len(scores) == len(merged["submissions"])
    assert all(isinstance(v, float) for v in scores.values())
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_a_flagged_submission_names_its_counterparty(specialists_ctx):
    """A flag that says only "high" is not actionable."""
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, merged = run_specialists(state, specialists_ctx)
    for flag in merged["similarity_flags"]:
        assert flag["against"], "a flag must say who to compare against"
        assert flag["against"] != flag["submission_id"]
        assert flag["threshold"] == SIMILARITY_FLAG_THRESHOLD


def test_the_similarity_agent_never_sets_a_penalty(specialists_ctx):
    """Doc 2.12 line 831: flag for review, never adjudicate."""
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, merged = run_specialists(state, specialists_ctx)
    for flag in merged["similarity_flags"]:
        assert set(flag) == {
            "submission_id",
            "score",
            "threshold",
            "against",
            "source",
            "reason",
        }
    for grade in merged["draft_grades"]:
        assert grade.status == "draft"


# ---------------------------------------------------------------------------
# The injected defect, and the criterion split
# ---------------------------------------------------------------------------


def test_the_injected_defect_reports_itself_and_scores_nothing(specialists_ctx):
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 2}
    _, merged = run_specialists(state, specialists_ctx)
    assert merged["similarity_scores"] == {}
    assert all(g.similarity_score is None for g in merged["draft_grades"])


def test_no_injection_means_a_clean_pass(specialists_ctx):
    state = {"course_code": "CS201", "attempts": 0, "fail_until_attempt": 0}
    _, merged = run_specialists(state, specialists_ctx)
    assert merged["similarity_scores"]


def test_criterion_marks_sum_to_the_awarded_total():
    rubric = [
        {"criterion_id": "correctness"},
        {"criterion_id": "analysis"},
        {"criterion_id": "presentation"},
    ]
    for awarded in (0.0, 1.0, 7.77, 13.33, 20.0):
        parts = _split_criteria(rubric, awarded)
        assert round(sum(parts.values()), 2) == round(awarded, 2), awarded


def test_a_criterion_count_that_does_not_match_the_weights_still_sums():
    rubric = [{"criterion_id": "a"}, {"criterion_id": "b"}, {"criterion_id": "c"},
              {"criterion_id": "d"}]
    parts = _split_criteria(rubric, 10.0)
    assert len(parts) == 4
    assert round(sum(parts.values()), 2) == 10.0


def test_an_empty_rubric_splits_to_nothing():
    assert _split_criteria([], 10.0) == {}


def test_the_three_weights_are_declared_for_the_three_criterion_rubrics():
    assert len(CRITERION_WEIGHTS) == 3


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


def test_an_exhausted_budget_escalates_with_a_diagnosis_and_the_draft():
    state = {
        "attempts": 3,
        "validation": ValidationOutcome(
            passed=False,
            failing=["similarity_score_attached"],
            diagnosis="no score attached",
            checks_run=list(DOC_CHECKS),
            attempt=3,
        ),
        "draft_grades": [_row()],
        "units_covered": ["1"],
        "assignments": [{"assignment_id": "A1"}],
    }
    esc = build_escalation(state, reason="retry_budget_exhausted", max_attempts=3)
    assert esc.reason == "retry_budget_exhausted"
    assert esc.raised_by == "validate"
    assert esc.attempts == 3 and esc.max_attempts == 3
    assert "no score attached" in esc.diagnosis
    assert "rather than retrying forever" in esc.diagnosis
    assert esc.pending_draft["draft_grades"]
    assert esc.pending_draft["failing_checks"] == ["similarity_score_attached"]


def test_a_refusal_escalates_from_the_gate_and_says_nothing_was_published():
    state = {
        "attempts": 1,
        "validation": ValidationOutcome(passed=True, checks_run=list(DOC_CHECKS)),
        "draft_grades": [_row()],
    }
    esc = build_escalation(
        state,
        reason="refuse",
        max_attempts=3,
        decided_by="course_faculty",
    )
    assert esc.raised_by == "gate"
    assert "refused publication" in esc.diagnosis
    assert "No grade was published" in esc.diagnosis


def test_escalation_names_the_human_the_doc_names():
    esc = build_escalation(
        {"attempts": 1, "validation": ValidationOutcome()},
        reason="retry_budget_exhausted",
        max_attempts=3,
    )
    assert esc.escalated_to == "course_faculty"


def test_an_escalation_keeps_the_attempt_log_so_the_retries_are_evidenced():
    state = {
        "attempts": 3,
        "validation": ValidationOutcome(passed=False),
        "attempt_log": [{"pass": 1}, {"pass": 2}, {"pass": 3}],
    }
    esc = build_escalation(
        state, reason="retry_budget_exhausted", max_attempts=3
    )
    assert esc.pending_draft["attempt_log"] == [
        {"pass": 1},
        {"pass": 2},
        {"pass": 3},
    ]


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------


def test_every_key_a_node_returns_is_declared_in_the_state():
    """LangGraph silently drops state keys the TypedDict does not declare.

    Found the hard way: ``ingest`` returned ``scope_verified_facts``,
    ``plan`` never saw it, the plan locked an empty scope, and the drift check
    reported every distributed assignment as out of scope. Nothing raised.
    """
    import ast
    from pathlib import Path

    from course_organizer.graph.state import CourseState

    declared = set(CourseState.__annotations__)
    package = Path(__file__).resolve().parents[1] / "src" / "course_organizer" / "graph"
    offenders: list[str] = []
    for name in ("nodes.py", "specialists.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Only the top-level `return {...}` of a node/factory, which is
            # what LangGraph merges into the state channels.
            if not fn.name.startswith(("make_", "ingest", "plan", "workers",
                                      "validate", "assemble", "gate",
                                      "publish", "escalate")):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            if key.value not in declared and "_" in key.value:
                                offenders.append(f"{name}:{fn.name}:{key.value}")
    assert not offenders, (
        "these keys are returned at the top level of a node but are not "
        f"declared in CourseState, so LangGraph discards them: {sorted(offenders)}"
    )


def test_a_gate_request_can_be_built_from_nothing_but_a_course():
    assert GateRequest(course_code="CS201").course_code == "CS201"
    assert GateRequest(course_code="X").draft_grades == []


def test_an_escalation_requires_a_reason_and_a_diagnosis():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Escalation(reason="x")