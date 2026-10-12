"""Lab 7 runner tests: the graph actually executing, end to end.

Where `test_lab07_graph.py` tests the pieces, this file runs the compiled
graph and checks the behaviours doc 2.10 promises:

* one command reaches the gate with a validated draft (line 690's acceptance
  criterion)
* the back-edge retries and the bound stops it (line 693's pitfall)
* a resumed run does not redo the nodes before the gate
* nothing publishes without a recorded human decision (line 830's guardrail)

Every test runs the real compiled graph against the real seed corpus. There are
no fakes here on purpose: a mocked ``invoke`` cannot show whether the
checkpointer was passed to ``compile()``, and that is one of the mistakes this
lab is most likely to make.
"""

from __future__ import annotations

import logging
import re
import typing

import pytest

from course_organizer.graph import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
    DECISION_REFUSE,
    DOC_CHECKS,
    MAX_ATTEMPTS,
    CourseStatus,
    FacultyDecision,
    build_escalation,
    build_graph,
    build_run_context,
    load_gate_request,
    resume_run,
    run_pipeline,
)
from course_organizer.graph.runner import (
    CHECKPOINTED_TYPES,
    CHECKPOINT_FILENAME,
    GraphRun,
    checkpoint_path,
    checkpoint_serializer,
    open_checkpointer,
)
from course_organizer.graph import compiled_nodes, state as state_mod, topology_lines

#: Doc 2.10's happy path, in order. Compared against the run's own record of
#: what executed, so a miswired node fails the test rather than being described
#: accurately in the docs.
CLEAN_PATH = ("ingest", "plan", "workers", "validate", "assemble")

#: Words that turn up in a typing annotation but name no class of ours. Dropped
#: so the allowlist guard looks only at identifiers that could be a model.
_TYPING_WORDS = frozenset({
    "Annotated", "Any", "Awaitable", "Callable", "dict", "float", "int",
    "list", "Mapping", "Optional", "Sequence", "str", "Union", "None",
})


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out"


# ---------------------------------------------------------------------------
# The clean run: one command, a validated draft, waiting at the gate
# ---------------------------------------------------------------------------


@pytest.fixture
def clean(out):
    return run_pipeline(course_code="CS201", run_id="clean", root=out, output_dir=out)


def test_a_clean_run_reaches_the_gate_without_publishing(clean):
    assert clean.awaiting_gate
    assert clean.status is CourseStatus.DRAFT
    assert not clean.published


def test_a_clean_run_executes_the_doc_path_and_stops_at_the_gate(clean):
    assert tuple(clean.nodes_ran()) == CLEAN_PATH


def test_a_clean_run_validates_on_its_first_pass(clean):
    assert clean.attempts_made() == 1
    report = clean.state["validation"]
    assert report.passed
    assert report.failing == []
    assert tuple(report.checks_run) == DOC_CHECKS


def test_all_four_doc_checks_actually_ran(clean):
    """A report claiming four checks while running three is a silent gap."""
    assert len(clean.state["validation"].checks_run) == 4


def test_every_node_appends_exactly_one_audit_entry(clean):
    trail = clean.state["audit_trail"]
    assert [e["node"] for e in trail] == list(CLEAN_PATH)
    assert all(e["summary"] for e in trail)


def test_the_gate_request_carries_everything_a_reviewer_needs(clean):
    """Doc 2.11 line 594: the reviewer sees rubric-aligned drafts and flags."""
    request = clean.gate_request
    assert request is not None
    assert request.course_code == "CS201"
    assert request.draft_grades, "the reviewer must see the grades"
    assert request.validation.passed
    assert request.units_covered
    assert request.assignments_included
    assert request.summary


def test_every_grade_offered_to_the_reviewer_is_still_a_draft(clean):
    """Doc 2.2 line 455: agents draft and flag; a human approves."""
    assert {g.status for g in clean.state["draft_grades"]} == {"draft"}
    assert all(not g.approved_by for g in clean.state["draft_grades"])


def test_every_draft_grade_carries_feedback_and_a_similarity_score(clean):
    for grade in clean.state["draft_grades"]:
        assert grade.feedback
        assert grade.similarity_score is not None
        assert grade.max_marks > 0


def test_the_run_assembled_a_document_before_asking_anyone(clean):
    """The document exists and is in the formatter's pre-approval state.

    ``ready_for_review`` is Lab 3's ``DocumentStatus``: the drift check passed,
    so the document is ready *for a human to look at*. It is not a claim that a
    human has looked at it -- the document's own approval section says
    ``Approved : no``, and every grade in it is still ``draft``. Those are
    different things and conflating them is how a draft becomes a publication
    by accident.
    """
    assert clean.state["document_id"]
    assert clean.state["document_status"] == "ready_for_review"
    assert clean.state["document_body"]
    # The document must not claim to be approved or published at this point.
    assert "Approved : no" in clean.state["document_body"]
    assert "Published: no" in clean.state["document_body"]


def test_a_clean_run_produces_no_drift(clean):
    """Doc 2.12 line 832: validation rejects a deviation before the human sees it."""
    assert clean.state["drift_findings"] == []


def test_every_draft_respects_the_rubric_maximum(clean):
    ceilings = {
        a["assignment_id"]: a["max_marks"] for a in clean.state["assignments"]
    }
    for grade in clean.state["draft_grades"]:
        assert grade.awarded_marks <= ceilings[grade.assignment_id]


def test_the_plan_is_locked_before_any_work_starts(clean):
    """Doc 2.2 line 472: the blueprint is locked before work begins."""
    ran = clean.nodes_ran()
    assert ran.index("plan") < ran.index("workers")
    assert clean.state["plan_sha256"]
    assert clean.state["obligation_count"] > 0


def test_a_run_is_reproducible_for_the_same_course(out):
    """Same input, same output -- doc 2.12's audit guardrail depends on it."""
    first = run_pipeline(course_code="CS201", run_id="r1", root=out, output_dir=out)
    second = run_pipeline(course_code="CS201", run_id="r2", root=out, output_dir=out)
    assert [g.awarded_marks for g in first.state["draft_grades"]] == [
        g.awarded_marks for g in second.state["draft_grades"]
    ]
    assert first.state["similarity_scores"] == second.state["similarity_scores"]


def test_a_second_course_produces_a_different_workspace(out):
    cs = run_pipeline(course_code="CS201", run_id="a", root=out, output_dir=out)
    db = run_pipeline(course_code="DBMS402", run_id="b", root=out, output_dir=out)
    assert cs.state["course_name"] != db.state["course_name"]
    assert cs.state["plan_sha256"] != db.state["plan_sha256"]


# ---------------------------------------------------------------------------
# The back-edge and its bound -- doc 2.10 line 693's pitfall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail_until,expected_attempts",
    [(0, 1), (1, 2), (2, 3)],
)
def test_the_back_edge_retries_until_validation_passes(out, fail_until, expected_attempts):
    run = run_pipeline(
        course_code="CS201",
        run_id=f"retry{fail_until}",
        root=out,
        output_dir=out,
        fail_until_attempt=fail_until,
    )
    assert run.attempts_made() == expected_attempts
    assert run.state["validation"].passed
    assert run.awaiting_gate


def test_a_retried_run_records_every_pass_it_made(out):
    run = run_pipeline(
        course_code="CS201", run_id="log", root=out, output_dir=out,
        fail_until_attempt=2,
    )
    assert [e["pass"] for e in run.state["attempt_log"]] == [1, 2, 3]


def test_a_retried_run_reruns_workers_for_every_pass(out):
    run = run_pipeline(
        course_code="CS201", run_id="nodes", root=out, output_dir=out,
        fail_until_attempt=2,
    )
    assert run.nodes_ran().count("workers") == 3
    assert run.nodes_ran().count("validate") == 3


def test_the_retry_appends_rather_than_replacing_the_attempt_log(out):
    """A reducer mistake here would erase the evidence the back-edge ran."""
    run = run_pipeline(
        course_code="CS201", run_id="append", root=out, output_dir=out,
        fail_until_attempt=1,
    )
    assert len(run.state["attempt_log"]) == 2


def test_the_bound_stops_a_run_that_never_converges(out):
    run = run_pipeline(
        course_code="CS201", run_id="never", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    assert run.attempts_made() == MAX_ATTEMPTS
    assert run.status is CourseStatus.ESCALATED
    assert not run.awaiting_gate


def test_an_exhausted_run_never_reaches_the_gate(out):
    run = run_pipeline(
        course_code="CS201", run_id="nogate", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    assert "gate" not in run.nodes_ran()
    assert run.state.get("published_path") is None


def test_exhaustion_escalates_with_a_diagnosis_and_the_failing_checks(out):
    run = run_pipeline(
        course_code="CS201", run_id="diag", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    esc = run.state["escalation"]
    assert esc.reason == "retry_budget_exhausted"
    assert esc.raised_by == "validate"
    assert esc.escalated_to == "course_faculty"
    assert esc.diagnosis
    assert esc.failing
    assert esc.pending_draft["draft_grades"], "the human needs the work, not just the error"


def test_the_loop_is_bounded_by_the_design_not_by_the_recursion_limit(out):
    """The bound is the state counter; ``recursion_limit`` is a backstop.

    If the backstop were what stopped the run, ``invoke`` would have raised
    ``GraphRecursionError``. It returned normally, so the ``MAX_ATTEMPTS`` check
    is what ended it.
    """
    from langgraph.errors import GraphRecursionError  # noqa: F401

    run = run_pipeline(
        course_code="CS201", run_id="bound", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    assert run.status is CourseStatus.ESCALATED
    assert len(run.nodes_ran()) < 32


def test_escalation_never_leaves_the_run_looking_published(out):
    run = run_pipeline(
        course_code="CS201", run_id="safe", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    assert not run.published
    assert {g.status for g in run.state["draft_grades"]} == {"draft"}


# ---------------------------------------------------------------------------
# The gate: suspend, resume, and do not redo the work
# ---------------------------------------------------------------------------


@pytest.fixture
def approved(out):
    run = run_pipeline(course_code="CS201", run_id="ok", root=out, output_dir=out)
    done = resume_run("ok", DECISION_APPROVE, root=out)
    return run, done


def test_resuming_an_approved_run_publishes_it(approved):
    _, done = approved
    assert done.status is CourseStatus.PUBLISHED
    assert done.published
    assert done.state["published_path"]


def test_resume_runs_only_the_gate_and_publish(approved):
    """Doc 2.5's chain is not walked again.

    The gate node re-enters from the top -- that is how ``interrupt`` works --
    but the nodes before it are not re-executed, so the run gains exactly two
    node records. If this ever regressed, the pipeline would be re-reading the
    corpus and re-drafting every grade on every approval.
    """
    before, done = approved
    before_nodes = before.nodes_ran()
    after = done.nodes_ran()
    assert after[: len(before_nodes)] == before_nodes
    assert after[len(before_nodes) :] == ["gate", "publish"]
    assert after.count("workers") == 1
    assert after.count("validate") == 1


def test_resume_preserves_the_grades_the_reviewer_was_shown(approved):
    before, done = approved
    shown = {g.submission_id: g.awarded_marks for g in before.state["draft_grades"]}
    published = {g.submission_id: g.awarded_marks for g in done.state["draft_grades"]}
    assert published == shown


def test_publishing_writes_the_document_to_disk(approved, tmp_path):
    _, done = approved
    from pathlib import Path

    path = Path(done.state["published_path"])
    assert path.exists()
    assert path.read_text(encoding="utf-8")


def test_the_published_document_names_who_approved_it(approved):
    _, done = approved
    text = done.state["document_body"]
    assert "course_faculty" in text
    for grade in done.state["draft_grades"]:
        assert grade.approved_by == "course_faculty"


def test_every_published_grade_carries_all_three_approval_fields(approved):
    _, done = approved
    for grade in done.state["draft_grades"]:
        assert grade.status == "published"
        assert grade.approved_by
        assert grade.approved_at


# ---------------------------------------------------------------------------
# The three approving decisions and the refusal
# ---------------------------------------------------------------------------


def test_edit_replaces_only_the_named_submission(out):
    run = run_pipeline(course_code="CS201", run_id="e", root=out, output_dir=out)
    target = run.state["draft_grades"][0]
    done = resume_run(
        "e",
        FacultyDecision(decision=DECISION_EDIT, edits={target.submission_id: 12.0}),
        root=out,
    )
    changed = {g.submission_id: g.awarded_marks for g in done.state["draft_grades"]}
    assert changed[target.submission_id] == 12.0
    others = [g for g in done.state["draft_grades"] if g.submission_id != target.submission_id]
    assert all(
        g.awarded_marks == row.awarded_marks
        for g, row in zip(others, [r for r in run.state["draft_grades"] if r.submission_id != target.submission_id])
    )


def test_edit_rescales_the_criterion_breakdown_to_the_new_total(out):
    run = run_pipeline(course_code="CS201", run_id="c", root=out, output_dir=out)
    target = run.state["draft_grades"][0]
    done = resume_run(
        "c",
        FacultyDecision(decision=DECISION_EDIT, edits={target.submission_id: 10.0}),
        root=out,
    )
    row = next(g for g in done.state["draft_grades"] if g.submission_id == target.submission_id)
    if row.criterion_marks:
        assert round(sum(row.criterion_marks.values()), 2) == 10.0


def test_an_edit_cannot_push_a_grade_above_the_rubric_maximum(out):
    """Doc 2.7 check 0 holds after human intervention, not just before it."""
    run = run_pipeline(course_code="CS201", run_id="over", root=out, output_dir=out)
    target = run.state["draft_grades"][0]
    done = resume_run(
        "over",
        FacultyDecision(decision=DECISION_OVERRIDE, override_marks={target.submission_id: 9999.0}),
        root=out,
    )
    row = next(g for g in done.state["draft_grades"] if g.submission_id == target.submission_id)
    assert row.awarded_marks <= row.max_marks


def test_override_supplies_the_marks_verbatim_when_they_are_legal(out):
    run = run_pipeline(course_code="CS201", run_id="ov", root=out, output_dir=out)
    target = run.state["draft_grades"][0]
    done = resume_run(
        "ov",
        FacultyDecision(decision=DECISION_OVERRIDE, override_marks={target.submission_id: 7.5}),
        root=out,
    )
    row = next(g for g in done.state["draft_grades"] if g.submission_id == target.submission_id)
    assert row.awarded_marks == 7.5
    assert done.published


@pytest.mark.parametrize(
    "decision", [DECISION_APPROVE, DECISION_EDIT, DECISION_OVERRIDE]
)
def test_each_of_the_docs_three_answers_authorises_publication(out, decision):
    """Doc 2.5 line 538: the gate returns approve, edit or override."""
    run_id = f"d{decision}"
    run = run_pipeline(course_code="CS201", run_id=run_id, root=out, output_dir=out)
    assert run.awaiting_gate
    done = resume_run(run_id, decision, root=out)
    assert done.published
    assert done.status is CourseStatus.PUBLISHED


def test_a_refusal_publishes_nothing_and_escalates(out):
    run = run_pipeline(course_code="CS201", run_id="no", root=out, output_dir=out)
    done = resume_run(
        "no",
        FacultyDecision(decision=DECISION_REFUSE, reason="rubric needs revision"),
        root=out,
    )
    assert done.status is CourseStatus.REFUSED
    assert not done.published
    assert done.state.get("published_path") is None
    assert done.state["escalation"].reason == "refuse"


def test_a_refused_run_leaves_every_grade_a_draft(out):
    run = run_pipeline(course_code="CS201", run_id="rd", root=out, output_dir=out)
    done = resume_run("rd", DECISION_REFUSE, root=out)
    assert {g.status for g in done.state["draft_grades"]} == {"draft"}
    assert all(not g.approved_by for g in done.state["draft_grades"])


def test_a_refusal_is_recorded_as_a_gate_escalation_with_a_reason(out):
    run = run_pipeline(course_code="CS201", run_id="rr", root=out, output_dir=out)
    done = resume_run(
        "rr",
        FacultyDecision(decision=DECISION_REFUSE, reason="rubric needs revision"),
        root=out,
    )
    esc = done.state["escalation"]
    assert esc.raised_by == "gate"
    assert "refused publication" in esc.diagnosis
    assert "rubric needs revision" in str(done.state["decision"])


def test_a_refusal_never_writes_a_file(out):
    run = run_pipeline(course_code="CS201", run_id="rf", root=out, output_dir=out)
    before = set(p for p in out.rglob("*") if p.is_file())
    done = resume_run("rf", DECISION_REFUSE, root=out)
    after = set(p for p in out.rglob("*") if p.is_file())
    assert after - before <= {checkpoint_path(out, "rf") / CHECKPOINT_FILENAME}


# ---------------------------------------------------------------------------
# The checkpointer: the gate has to survive the process
# ---------------------------------------------------------------------------


def test_a_suspended_run_leaves_a_checkpoint_file_behind(clean, out):
    assert checkpoint_path(out, "clean").exists()


def test_the_gate_request_can_be_read_back_without_resuming(clean, out):
    """A reviewer can be shown the pending request by a separate process."""
    request = load_gate_request("clean", root=out)
    assert request is not None
    assert request.course_code == "CS201"
    assert request.draft_grades


def test_reading_the_request_does_not_disturb_the_run(clean, out):
    before = clean.nodes_ran()
    load_gate_request("clean", root=out)
    done = resume_run("clean", DECISION_APPROVE, root=out)
    assert done.nodes_ran()[: len(before)] == before
    assert done.published


def test_asking_for_an_unknown_runs_request_returns_nothing(out):
    assert load_gate_request("never-ran", root=out) is None


def test_the_checkpoint_file_is_created_if_absent(tmp_path):
    path = tmp_path / "nested" / "graph.sqlite"
    with open_checkpointer(path) as saver:
        assert saver is not None
    assert path.exists()


def test_resuming_a_run_that_never_suspended_is_a_caller_error(out):
    run = run_pipeline(course_code="CS201", run_id="fin", root=out, output_dir=out)
    done = resume_run("fin", DECISION_APPROVE, root=out)
    assert done.published
    with pytest.raises(RuntimeError, match="not suspended"):
        done.resume(build_graph(run.context), DECISION_APPROVE)


def test_two_runs_do_not_share_a_checkpoint(out):
    first = run_pipeline(course_code="CS201", run_id="one", root=out, output_dir=out)
    second = run_pipeline(course_code="CS201", run_id="two", root=out, output_dir=out)
    assert checkpoint_path(out, "one") != checkpoint_path(out, "two")
    assert first.thread_id != second.thread_id
    resume_run("one", DECISION_REFUSE, root=out)
    still_waiting = load_gate_request("two", root=out)
    assert still_waiting is not None, "refusing one run must not resume the other"


# ---------------------------------------------------------------------------
# The summary line, which is what the CLI and the evidence files print
# ---------------------------------------------------------------------------


def test_a_suspended_summary_says_it_is_waiting_for_the_human(clean):
    text = clean.summary()
    assert "gate=awaiting_faculty" in text
    assert f"run={clean.run_id}" in text


def test_a_published_summary_carries_the_path(approved):
    _, done = approved
    assert "published=" in done.summary()
    assert done.state["published_path"] in done.summary()


def test_an_escalated_summary_names_the_reason(out):
    run = run_pipeline(
        course_code="CS201", run_id="s", root=out, output_dir=out,
        fail_until_attempt=99,
    )
    assert "escalated=retry_budget_exhausted" in run.summary()


def test_the_summary_is_ascii(clean):
    assert all(ord(ch) < 128 for ch in clean.summary())


# ---------------------------------------------------------------------------
# An unknown course fails loudly instead of producing an empty workspace
# ---------------------------------------------------------------------------


def test_an_unknown_course_raises_rather_than_reporting_an_empty_scope(out):
    """A silent empty result would validate trivially and publish nothing."""
    with pytest.raises(Exception) as excinfo:
        run_pipeline(course_code="NOPE404", run_id="x", root=out, output_dir=out)
    assert "NOPE404" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The checkpoint's type allowlist
#
# LangGraph refuses by default to rebuild a state value from bytes unless the
# type is one it recognises. It logs a warning per type per process, and says
# the warning's own remedy: "this will be blocked in a future version". The
# allowlist is the fix the warning asks for. These tests make sure it stays
# correct as the state grows.
# ---------------------------------------------------------------------------


def test_every_checkpointed_type_is_declared_to_langgraph():
    """Each allowlisted type must be the real class LangGraph will look up.

    ``allowed_msgpack_modules`` accepts classes, which is more convenient than
    the ``(module, qualname)`` pairs the warning message prints -- and also why
    this asserts against the imported symbol rather than a name string.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = checkpoint_serializer()
    assert isinstance(serde, JsonPlusSerializer)
    assert CHECKPOINTED_TYPES, "an empty allowlist would restore the warnings"

    for cls in CHECKPOINTED_TYPES:
        assert cls.__module__.startswith("course_organizer."), (
            "an allowlist naming an arbitrary importable type would be a "
            f"deserialisation gadget, not a fix: {cls}"
        )
        # The same object the module itself exports, so a stale copy cannot be
        # accepted here and then ignored by the lookup at load time.
        assert getattr(state_mod, cls.__name__) is cls


def test_the_allowlist_covers_the_non_json_types_the_state_holds():
    """The state holds pydantic models and an enum; those are the ones needing it.

    Driven by walking the declared annotations for names that resolve to a type
    this module exports, so adding a model to ``CourseState`` without adding it
    to the allowlist fails here rather than at the next resume.

    The annotations are ``ForwardRef`` objects, not strings, because
    ``state.py`` uses ``from __future__ import annotations``. Reading them as
    strings would make this test vacuously pass, which is why it also asserts
    that it found something.
    """
    from course_organizer.graph.state import CourseState

    annotations = CourseState.__annotations__
    assert annotations, "CourseState lost every annotation; the guard is vacuous"

    referenced: set[type] = set()
    # .values(), deliberately: iterating the mapping would hand _annotation_names
    # a field *name*, resolve nothing, and pass vacuously.
    for annotation in annotations.values():
        for name in _annotation_names(annotation):
            obj = getattr(state_mod, name, None)
            if isinstance(obj, type) and obj.__module__ == state_mod.__name__:
                referenced.add(obj)
    assert referenced, (
        "no type owned by state.py is reachable from CourseState's annotations, "
        "so the guard has stopped guarding -- re-check it by hand"
    )
    missing = referenced - set(CHECKPOINTED_TYPES)
    assert not missing, (
        "referenced by CourseState but not allowlisted: "
        f"{sorted(t.__name__ for t in missing)}"
    )


def _annotation_names(annotation) -> set[str]:
    """Every class name mentioned anywhere inside a typing annotation."""
    names: set[str] = set()
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, typing.ForwardRef):
            # ``from __future__ import annotations`` makes these the norm here.
            stack.append(current.__forward_arg__)
            continue
        if isinstance(current, str):
            names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", current))
            continue
        if isinstance(current, type):
            names.add(current.__name__)
        stack.extend(typing.get_args(current))
    return names - _TYPING_WORDS


def test_a_resume_emits_no_unregistered_type_warning(clean, out, caplog):
    """The observable difference: the warnings are gone on a real resume."""
    decision = FacultyDecision(
        decision=DECISION_APPROVE, decided_by="Dr. Iyer",
        reason="marks and flags both check out",
    )
    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde"):
        resume_run(clean.run_id, decision, root=out)
    noisy = [
        r.getMessage() for r in caplog.records
        if "unregistered type" in r.getMessage()
    ]
    assert not noisy, noisy


def test_the_checkpoint_still_round_trips_state_that_crosses_it(out):
    """An allowlist must not cost a value. The gate request is the witness."""
    run = run_pipeline(course_code="CS201", run_id="rt", root=out, output_dir=out)
    request = load_gate_request("rt", root=out)
    assert request is not None
    assert request.draft_grades, "no grades survived the checkpoint"
    assert request.validation.passed is True
    # The rows come back as DraftGradeRow, not dicts -- that is the deserialise.
    assert type(request.draft_grades[0]).__name__ == "DraftGradeRow"
    assert request.attempt == run.state["attempts"]
    # And the allowlist is load-bearing, not decorative: these four are exactly
    # the types the warning named, and each is a value the gate depends on.
    assert {t.__name__ for t in CHECKPOINTED_TYPES} >= {
        "GateRequest", "DraftGradeRow", "ValidationOutcome", "CourseStatus",
    }


# ---------------------------------------------------------------------------
# Inspecting the graph must be quieter than running it
# ---------------------------------------------------------------------------


def test_inspecting_the_wiring_logs_no_warning(out, caplog):
    """``build_graph`` still warns with no checkpointer -- correctly.

    The CLI compiles a throwaway graph just to print the topology, and that
    should not train a reader to ignore the warning that matters, so
    inspection goes through ``inspect_graph`` with an explicit in-memory saver.
    """
    ctx = build_run_context("inspect", course_code="CS201", output_dir=out)
    with caplog.at_level(logging.WARNING):
        lines = topology_lines(ctx)
        nodes = compiled_nodes(ctx)
    assert not caplog.records, [r.getMessage() for r in caplog.records]
    assert "escalate" in nodes
    assert any("validate -> workers" in line for line in lines)


def test_compiled_nodes_reports_the_same_nodes_as_the_doc_chain(out):
    """Read off the compiled graph, so a miswired node cannot pass this."""
    from course_organizer.graph import DOC_NODE_CHAIN

    ctx = build_run_context("chain", course_code="CS201", output_dir=out)
    nodes = set(compiled_nodes(ctx))
    assert set(DOC_NODE_CHAIN) <= nodes
    assert "escalate" in nodes