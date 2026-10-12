"""Acceptance tests for Lab 1 — Agent vs Chatbot (planner loop).

The two tests named `test_doc_acceptance_*` are the document's own acceptance
criterion, quoted verbatim in their docstrings. They are the tests to point a
reviewer at.

    Doc 2.10 Lab 1, Accept:
    "Given a vague request it asks at least two clarifying questions and
     yields a coherent, editable plan."
"""

from __future__ import annotations

import pytest

from course_organizer import HUMAN_AUTHORITY
from course_organizer.labs.lab01_planner_loop import (
    MAX_REVISIONS,
    PlannerAgent,
    PlannerResult,
    ScriptedFeedback,
    one_shot_chatbot,
    run_planner_loop,
)
from course_organizer.schemas import (
    CourseWorkspacePlan,
    PlanStatus,
)


# ═══════════════════════════════════════════════════════════════════════════
# The document's acceptance criterion
# ═══════════════════════════════════════════════════════════════════════════


def test_doc_acceptance_vague_request_asks_two_or_more_questions(
    vague_request: str, llm
) -> None:
    """Doc 2.10 Lab 1 Accept, part 1.

    "Given a vague request it asks **at least two clarifying questions**."
    """
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=False)

    assert result.request.question_count >= 2, (
        f"expected >= 2 clarifying questions, got {result.request.question_count}"
    )
    # The questions must be real questions about real missing parameters,
    # not padding.
    for question in result.questions_asked:
        assert "?" in question.question, (
            f"not a question: {question.question!r}"
        )
        assert question.why_it_matters, "every question must justify itself"
        assert question.answer, "every asked question must record its answer"


def test_doc_acceptance_yields_coherent_editable_plan(
    vague_request: str, llm
) -> None:
    """Doc 2.10 Lab 1 Accept, part 2.

    "...and yields a coherent, **editable** plan."

    "Coherent" is checked structurally: steps are dependency-ordered with no
    cycles, and the governance spine the document mandates (validate -> human
    gate -> publish) is present and correctly ordered.
    """
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=False)
    plan = result.plan

    # --- editable: still a draft, and genuinely modifiable ---------------
    assert plan.status is PlanStatus.DRAFT, "Lab 1 must not lock or approve a plan"
    assert result.is_editable_plan
    assert plan.plan_sha256()  # has a content hash, i.e. is a real object

    # --- coherent: the plan is dependency-ordered and acyclic -----------
    ordered = plan._topological_steps()  # raises on a cycle
    assert [s.step_id for s in ordered] == [s.step_id for s in plan.steps], (
        "steps must already be in dependency order"
    )
    for step in ordered:
        for dependency in step.depends_on:
            assert dependency in {s.step_id for s in plan.steps}, (
                f"{step.step_id} depends on unknown step {dependency}"
            )

    # --- the governance spine is present and in the right order ---------
    names = " | ".join(s.name.lower() for s in ordered)
    assert "validate" in names, "doc 2.7 requires validation as a hard gate"
    assert HUMAN_AUTHORITY in plan.human_authority, (
        "the plan must name the doc's human authority (course faculty)"
    )

    gate_index = next(
        i for i, s in enumerate(ordered) if HUMAN_AUTHORITY in s.name.lower()
    )
    publish_index = next(
        i for i, s in enumerate(ordered) if "publish" in s.name.lower()
    )
    assert gate_index < publish_index, (
        "the human gate is a HARD STOP and must precede publish (doc 2.3, 2.7)"
    )

    # --- every step is actionable --------------------------------------
    for step in plan.steps:
        assert step.intent.strip(), f"{step.step_id} has no intent"
        assert step.acceptance_criteria, (
            f"{step.step_id} has no acceptance criteria — not testable"
        )


# ═══════════════════════════════════════════════════════════════════════════
# The clarify step
# ═══════════════════════════════════════════════════════════════════════════


def test_concrete_request_asks_no_questions(concrete_request: str, llm) -> None:
    """Control case: a request that already has every core parameter.

    If the agent asked questions here it would be nagging, which is the
    opposite failure of asking too few.
    """
    result = run_planner_loop(concrete_request, llm=llm, compare_one_shot=False)
    assert result.request.question_count == 0, (
        f"expected no questions, got {[q.field for q in result.questions_asked]}"
    )


def test_clarify_extracts_core_parameters_from_free_text(
    concrete_request: str, llm
) -> None:
    """Clarification must produce typed, correct values — not a summary."""
    result = run_planner_loop(concrete_request, llm=llm, compare_one_shot=False)
    request = result.request

    assert request.course_code == "CS201"
    assert request.course_name == "Data Structures"
    assert request.units == ["1", "2", "3"]
    assert request.assignment_ids == ["ASG-01", "ASG-02"]
    assert request.as_of.date().isoformat() == "2026-10-15"
    assert request.human_authority == HUMAN_AUTHORITY


def test_clarify_answers_become_the_plan_scope(vague_request: str, llm) -> None:
    """Answers given during clarify must actually drive the plan.

    This is what separates an agent loop from a chat: what the user said
    changes what gets built.
    """
    provider = ScriptedFeedback(
        answers={
            "course_code": "DBMS402",
            "units": "units 2-4",
            "assignments": "all three",
            "as_of": "2026-11-01",
        }
    )
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    assert result.request.course_code == "DBMS402"
    assert result.request.units == ["2", "3", "4"]
    assert len(result.request.assignment_ids) == 3

    # The plan must reflect the answers, not the original vague text.
    plan_text = result.plan.outline()
    assert "DBMS402" in plan_text
    assert "2, 3, 4" in plan_text


def test_clarify_rejects_empty_request(llm) -> None:
    """An empty request is a programming error, not a vague request."""
    agent = PlannerAgent(llm=llm)
    with pytest.raises(ValueError, match="non-empty"):
        agent.clarify("   ", ScriptedFeedback())


def test_vague_request_never_guesses_without_saying_so(vague_request: str, llm) -> None:
    """The doc's Lab 3 pitfall is no silent guessing; Lab 1 must obey it too."""
    provider = ScriptedFeedback(answers={})  # user answers nothing meaningful
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    # Anything the agent filled in itself must be visible in `assumptions`.
    for question in result.questions_asked:
        if question.assumed or not question.answer:
            assert any(question.field in a for a in result.request.assumptions), (
                f"{question.field} was unresolved but not recorded as an assumption"
            )


# ═══════════════════════════════════════════════════════════════════════════
# The propose_plan step
# ═══════════════════════════════════════════════════════════════════════════


def test_plan_covers_all_in_scope_streams(concrete_request: str, llm) -> None:
    """Doc 2.3 end-to-end flow names three streams; the plan must cover them."""
    result = run_planner_loop(concrete_request, llm=llm, compare_one_shot=False)
    names = " ".join(s.name.lower() for s in result.plan.steps)

    assert "notes" in names, "stream 1: Notes Repository Agent"
    assert "assign" in names, "stream 2: Assignment Distribution Agent"
    assert "collect" in names, "stream 2: Submission Collection Agent"
    assert "similarity" in names, "stream 2: Plagiarism / Similarity Agent"
    assert "grade" in names, "stream 2: Grading Assistance Agent"
    assert "milestone" in names, "stream 3: Project Milestone Tracking Agent"


def test_plan_scales_with_scope(concrete_request: str, llm) -> None:
    """Same skills, different scope -> a genuinely different plan.

    Note the agent *does* ask about assignments when they are unmentioned —
    they are a required parameter, and the doc says "ask until inputs
    complete". What matters is that an explicit "no assignments" answer
    genuinely removes the grading stream from the plan. This is the Lab 3
    reuse property already visible in Lab 1's plan.
    """
    notes_only = "Organise CS201 Data Structures for units 1-3 as of 2026-10-15."
    provider = ScriptedFeedback(answers={"assignments": "none"})
    result = run_planner_loop(notes_only, provider=provider, llm=llm, compare_one_shot=False)
    names = " ".join(s.name.lower() for s in result.plan.steps)

    assert result.request.assignment_ids == [], "an explicit 'none' is a real answer"
    assert "notes" in names
    assert "draft grades" not in names, "no assignments in scope, so no grading step"
    assert len(result.plan.steps) < 12, "a narrower scope must be a smaller plan"

    # The governance spine must survive the narrowing.
    assert "validate" in names
    assert "publish" in names
    assert "course_faculty" in names


def test_plan_never_marks_itself_final(vague_request: str, llm) -> None:
    """Doc pseudocode comment: `return plan  # not final — a structured plan`."""
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=False)
    assert result.plan.status is PlanStatus.DRAFT
    assert result.plan.status is not PlanStatus.APPROVED


def test_plan_id_is_stable_and_course_scoped(concrete_request: str, llm) -> None:
    """Plan identity must be traceable in later labs' audit logs."""
    result = run_planner_loop(concrete_request, llm=llm, compare_one_shot=False)
    assert result.plan.plan_id.startswith("PLAN-CS201-")


def test_plan_outline_renders_dependency_order(vague_request: str, llm) -> None:
    """The human-editable outline is the actual Lab 1 deliverable."""
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=False)
    outline = result.plan.outline()

    assert outline.startswith("PLAN ")
    assert "Request:" in outline
    assert "S1." in outline
    # A step that depends on S1 must be printed after it.
    lines = outline.splitlines()
    s1_index = next(i for i, line in enumerate(lines) if " S1." in line)
    dependents = [i for i, line in enumerate(lines) if "[after S1]" in line]
    assert dependents, "at least one step should depend on S1"
    assert all(i > s1_index for i in dependents)


# ═══════════════════════════════════════════════════════════════════════════
# The revise loop
# ═══════════════════════════════════════════════════════════════════════════


def test_revise_applies_a_real_change(vague_request: str, llm) -> None:
    """Doc 2.10 Lab 1 pseudocode: `plan = revise(plan, user.feedback)`.

    A revision that only appends to a log would not be a revision. The
    returned plan must differ structurally.
    """
    provider = ScriptedFeedback(feedback_turns=["add a step for peer review of drafts"])
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    assert result.revision_count == 1
    assert result.plan.revisions[0].feedback == "add a step for peer review of drafts"
    assert result.plan.revisions[0].changes_made, "a revision must record what changed"

    names = " ".join(s.name.lower() for s in result.plan.steps)
    assert "peer review" in names, "the added step must actually be in the plan"


def test_revise_can_remove_a_step(vague_request: str, llm) -> None:
    """Feedback must be able to delete, not only add."""
    provider = ScriptedFeedback(feedback_turns=["remove the step track project milestones"])
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    names = " ".join(s.name.lower() for s in result.plan.steps)
    assert "milestone" not in names
    assert result.plan.revisions[0].changes_made


def test_revise_can_move_the_deadline(vague_request: str, llm) -> None:
    """A revision that changes the plan's anchor date changes the plan."""
    provider = ScriptedFeedback(feedback_turns=["set the deadline to 2026-12-01"])
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    assert result.request.as_of.date().isoformat() == "2026-12-01"
    assert result.plan.revisions[0].changes_made


def test_revise_can_widen_scope(vague_request: str, llm) -> None:
    provider = ScriptedFeedback(feedback_turns=["also include unit 5"])
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)
    assert "5" in result.request.units


def test_revise_never_loses_the_users_words(vague_request: str, llm) -> None:
    """Unrecognised feedback is still recorded.

    A revision the agent cannot interpret must be visible, not silently
    dropped — the user would otherwise think their input was ignored.
    """
    feedback = "Honestly I am not sure we should use a wiki at all here"
    provider = ScriptedFeedback(feedback_turns=[feedback])
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    assert result.plan.revisions[0].feedback == feedback
    assert result.plan.revisions[0].changes_made is not None


def test_revise_rejects_empty_feedback(llm, concrete_request: str) -> None:
    agent = PlannerAgent(llm=llm)
    result = run_planner_loop(concrete_request, llm=llm, compare_one_shot=False)
    with pytest.raises(ValueError, match="non-empty"):
        agent.revise(result.plan, "   ")


def test_revise_loop_is_bounded(vague_request: str, llm) -> None:
    """Doc 2.10 Lab 7 pitfall: "an unbounded regenerate loop can spin forever."

    The same bound is applied to the revise loop, and hitting it must be
    recorded rather than hidden.
    """
    endless = [f"add a step called extra thing number {i}" for i in range(50)]
    provider = ScriptedFeedback(feedback_turns=endless)
    result = run_planner_loop(vague_request, provider=provider, llm=llm, compare_one_shot=False)

    assert result.revision_count == MAX_REVISIONS
    assert result.plan.open_questions, "hitting the bound must be escalated, not hidden"
    assert "50" not in [r.feedback for r in result.plan.revisions], "loop must stop"


# ═══════════════════════════════════════════════════════════════════════════
# The anti-pattern: the doc's "Pitfall to avoid"
# ═══════════════════════════════════════════════════════════════════════════


def test_one_shot_chatbot_is_not_an_agent(vague_request: str, llm) -> None:
    """Doc 2.10 Lab 1 Pitfall: "Don't answer in one shot — without
    clarify/revise it's a chatbot, not an agent."

    Asserts the three structural differences the document names, so the
    distinction is demonstrated rather than asserted in prose.
    """
    answer = one_shot_chatbot(llm, vague_request)

    # 1. It returns a bare string, not a typed plan.
    assert isinstance(answer, str)
    assert not isinstance(answer, CourseWorkspacePlan)

    # 2. It asks nothing.
    assert not hasattr(answer, "questions_asked")

    # 3. It cannot be revised, because there is nothing structured to revise.
    assert not hasattr(answer, "revisions")

    # Meanwhile the agent returns a real, typed, revisable object.
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=True)
    assert isinstance(result.plan, CourseWorkspacePlan)
    assert result.request.question_count >= 2
    assert hasattr(result.plan, "revisions")


# ═══════════════════════════════════════════════════════════════════════════
# Budget + config behaviour
# ═══════════════════════════════════════════════════════════════════════════


def test_run_respects_the_llm_budget(vague_request: str) -> None:
    """Lab 6's "budgets", enforced from Lab 1 onward.

    A run with too small a budget must fail loudly instead of silently
    exceeding its allowance.
    """
    from course_organizer.llm import BudgetExceeded, ScriptedLLM

    tight = ScriptedLLM(max_calls=1)
    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        run_planner_loop(vague_request, llm=tight, compare_one_shot=False)


def test_settings_default_to_offline_scripted_backend() -> None:
    """With no `.env` at all, the project must still be runnable."""
    from course_organizer.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_backend == "scripted"
    assert settings.source_adapter == "local"
    assert settings.gemini_model  # a sane default is configured
    assert settings.using_live_llm() is False


def test_require_live_llm_explains_the_fix() -> None:
    """A missing key must produce an actionable message, not a stack trace."""
    from course_organizer.config import Settings

    settings = Settings(llm_backend="live", google_api_key=None, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError) as exc:
        settings.require_live_llm()
    message = str(exc.value)
    assert "GOOGLE_API_KEY" in message
    assert "aistudio.google.com" in message
    assert "scripted" in message, "must offer the offline escape hatch"


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end shape of the deliverable
# ═══════════════════════════════════════════════════════════════════════════


def test_result_summary_reports_the_acceptance_metrics(vague_request: str, llm) -> None:
    """The summary the notebook and CLI show must contain the doc's metrics."""
    result = run_planner_loop(vague_request, llm=llm, compare_one_shot=True)
    summary = result.summary()
    for expected in ("clarifying questions asked", "plan status", "revisions applied", "approver"):
        assert expected in summary


def test_trace_records_the_loop_order(vague_request: str, llm) -> None:
    """The trace must show clarify -> propose_plan -> revise, in that order."""
    result = run_planner_loop(
        vague_request,
        provider=ScriptedFeedback(feedback_turns=["add a step for archive review"]),
        llm=llm,
        compare_one_shot=False,
    )
    assert result.trace[0] == "clarify"
    assert result.trace[1] == "propose_plan"
    assert "revise:1" in result.trace
