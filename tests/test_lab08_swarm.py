"""Lab 8 tests: the pure selection layer, the fan-out topology, and the measured
acceptance criterion.

The organising idea, inherited from the Lab 7 tests, is that **nothing is
asserted against a declared constant when it can be asserted against the compiled
graph or against a measurement.** The swarm names three new nodes; a test that
only checked the names would still pass if the wiring were reversed. So the
topology tests read ``build_swarm_graph(...).get_graph()``, and the timing tests
read the intervals the run itself recorded.

Two further properties this file deliberately holds:

* **The merge recomputes alignment.** A test hands a candidate a lying
  ``self_reported_alignment`` and asserts it does not win. A selector that trusted
  the candidate's own score would be selecting on the thing it is meant to verify.
* **The acceptance is measured, not constructed.** ``parallel stage < sequential
  stage`` reads each run's own ``swarm_timing``. The speedup exists because the
  per-candidate evidence write is real file I/O that releases the GIL; the
  concurrency claim is checked structurally by ``max_overlap >= 2`` so that a
  timing fluke cannot pass the wall-clock check on its own.

Every test runs against the seed corpus with the scripted LLM backend, so no API
key and no network. That is a property of the earlier labs' fixtures and this file
inherits it deliberately.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_type_hints

import pytest

from course_organizer.graph import (
    DECISION_APPROVE,
    DOC_CHECKS,
    KIND_SUBMISSION,
    KIND_UNIT,
    MAX_ATTEMPTS,
    PRIORITY_SUBMISSION,
    PRIORITY_UNIT,
    SUBMISSION_STRATEGIES,
    SWARM_MAX_CONCURRENCY,
    UNIT_STRATEGIES,
    CourseStatus,
    DraftGradeRow,
    SwarmState,
    WorkItem,
    alignment_to_plan,
    build_run_context,
    build_swarm_graph,
    build_work_items,
    inspect_swarm,
    merge_results,
    plan_scope,
    resume_swarm,
    run_swarm,
    timing_summary,
)
from course_organizer.graph.state import append
from course_organizer.graph.swarm import (
    GENEROUS_FACTOR,
    SUBMISSION_OBLIGATIONS,
    UNIT_OBLIGATIONS,
    _BoundedFanOut,
    align_submission,
    align_unit,
    build_base,
    fan_out,
    make_candidate,
    make_merge,
    slug,
    swarm_edges,
    write_candidate_artifact,
)


# ---------------------------------------------------------------------------
# The doc's own vocabulary, and the synthetic corpus the pure tests use
# ---------------------------------------------------------------------------


def test_the_swarm_has_two_kinds_and_submission_is_the_priority():
    """Line 701 names submissions; line 704 says all units. Both are work."""
    assert KIND_SUBMISSION == "submission"
    assert KIND_UNIT == "unit"
    assert PRIORITY_SUBMISSION > PRIORITY_UNIT


def test_each_kind_has_exactly_three_deterministic_strategies():
    """Best-of-N needs a loser to log, so N must exceed one."""
    assert len(SUBMISSION_STRATEGIES) == 3
    assert len(UNIT_STRATEGIES) == 3
    assert len(set(SUBMISSION_STRATEGIES)) == 3
    assert len(set(UNIT_STRATEGIES)) == 3


def test_the_obligation_sets_are_the_doc_screens_restated():
    """Five checks each, so the alignment score has a denominator."""
    assert len(SUBMISSION_OBLIGATIONS) == 5
    assert len(UNIT_OBLIGATIONS) == 5
    assert "within_rubric_maximum" in SUBMISSION_OBLIGATIONS
    assert "similarity_attached" in SUBMISSION_OBLIGATIONS
    assert "no_overclaim" in UNIT_OBLIGATIONS


def test_generous_is_ten_percent_over_the_base():
    assert GENEROUS_FACTOR == pytest.approx(1.10)


def _row(**kw):
    base = dict(
        submission_id="S1",
        assignment_id="A1",
        awarded_marks=15.0,
        max_marks=20.0,
        criterion_marks={"correctness": 8.0, "tests": 7.0},
        similarity_score=0.2,
        late=False,
    )
    base.update(kw)
    return DraftGradeRow(**base)


def _state(**overrides):
    """A tiny two-submission, two-unit corpus, built without any graph run.

    It is deliberately *not* the seed corpus: the pure functions under test are
    about logic, and a hand-built corpus lets each test say exactly which
    obligation it is exercising.
    """
    state = {
        "submissions": [
            {"submission_id": "S1", "assignment_id": "A1"},
            {"submission_id": "S2", "assignment_id": "A1"},
        ],
        "assignments": [
            {
                "assignment_id": "A1",
                "unit": "1",
                "max_marks": 20,
                "rubric": [
                    {"criterion_id": "correctness"},
                    {"criterion_id": "tests"},
                ],
            },
            {
                "assignment_id": "A2",
                "unit": "2",
                "max_marks": 20,
                "rubric": [{"criterion_id": "correctness"}],
            },
        ],
        "units_covered": ["2", "1"],
        "notes_by_unit": {"1": 2, "2": 1},
        "draft_grades": [
            _row(),
            _row(
                submission_id="S2",
                awarded_marks=10.0,
                criterion_marks={"correctness": 10.0},
                similarity_score=0.1,
            ),
        ],
        "late_flags": [],
        "plan_locked": {"units": ["1", "2"], "assignment_ids": ["A1", "A2"]},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Work items: both kinds, submissions first
# ---------------------------------------------------------------------------


def test_build_work_items_produces_both_kinds():
    items = build_work_items(_state())
    assert [i.kind for i in items] == [
        KIND_SUBMISSION,
        KIND_SUBMISSION,
        KIND_UNIT,
        KIND_UNIT,
    ]


def test_build_work_items_puts_submissions_before_units():
    items = build_work_items(_state())
    priorities = [i.priority for i in items]
    assert priorities == [
        PRIORITY_SUBMISSION,
        PRIORITY_SUBMISSION,
        PRIORITY_UNIT,
        PRIORITY_UNIT,
    ]


def test_build_work_items_sorts_units_numerically_not_lexically():
    """Unit 10 must not sort before unit 2, or the work order is nonsense."""
    items = build_work_items(_state(units_covered=["10", "2", "1"]))
    units = [i.item_id for i in items if i.kind == KIND_UNIT]
    assert units == ["1", "2", "10"]


def test_a_submission_item_carries_its_assignment_and_unit():
    items = build_work_items(_state())
    first = items[0]
    assert first.item_id == "S1"
    assert first.assignment_id == "A1"
    assert first.unit == "1"


def test_a_unit_item_key_is_kind_qualified():
    """So a submission id that looks like a unit cannot collide with one."""
    items = build_work_items(_state())
    keys = {i.key for i in items}
    assert keys == {
        "submission:S1",
        "submission:S2",
        "unit:1",
        "unit:2",
    }


def test_every_item_survives_a_dict_round_trip():
    item = WorkItem(kind=KIND_UNIT, item_id="3", unit="3", priority=PRIORITY_UNIT)
    assert WorkItem(**{k: v for k, v in item.to_dict().items() if k != "key"}) == item


def test_plan_scope_reads_units_and_assignments_from_the_plan():
    scope = plan_scope({"units": [1, 2], "assignment_ids": ["A1"]})
    assert scope["units"] == ("1", "2")
    assert scope["assignment_ids"] == ("A1",)


def test_an_absent_plan_is_an_empty_scope_not_a_crash():
    assert plan_scope(None) == {"units": (), "assignment_ids": ()}


# ---------------------------------------------------------------------------
# The base record: the upstream draft, not a re-derivation
# ---------------------------------------------------------------------------


def test_a_submission_base_is_the_draft_workers_produced():
    """Line 573: the swarm selects over drafts. It must not re-mark."""
    base = build_base(_state(), build_work_items(_state())[0])
    assert base["awarded_marks"] == pytest.approx(15.0)
    assert base["max_marks"] == pytest.approx(20.0)
    assert base["criterion_ids"] == ["correctness", "tests"]
    assert base["similarity_score"] == pytest.approx(0.2)


def test_a_submission_base_raises_loudly_when_workers_never_ran():
    state = _state(draft_grades=[])
    with pytest.raises(KeyError) as excinfo:
        build_base(state, WorkItem(kind=KIND_SUBMISSION, item_id="S1"))
    assert "workers" in str(excinfo.value)


def test_a_unit_base_counts_notes_assignments_and_submissions():
    items = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT]
    base = build_base(_state(), items[0])  # unit "1"
    assert base["note_count"] == 2
    assert base["assignment_ids"] == ["A1"]
    assert base["submission_ids"] == ["S1", "S2"]


def test_a_unit_with_no_assignments_has_an_empty_linkage():
    items = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT]
    base = build_base(_state(), items[1])  # unit "2"
    assert base["assignment_ids"] == ["A2"]
    assert base["submission_ids"] == []


def test_the_overclaim_candidate_borrows_a_real_foreign_assignment():
    """A sentinel would be obvious; a plausible mistake is the point."""
    items = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT]
    base = build_base(_state(), items[0])  # unit 1 owns A1
    assert base["foreign_assignment"] == "A2"


# ---------------------------------------------------------------------------
# Calibration: three strategies per kind
# ---------------------------------------------------------------------------


def test_strict_leaves_the_base_marks_alone():
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "strict")
    assert content["awarded_marks"] == pytest.approx(15.0)
    assert content["similarity_score"] == pytest.approx(0.2)


def test_generous_inflates_marks_and_scales_the_criteria_with_them():
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "generous")
    assert content["awarded_marks"] == pytest.approx(
        round(15.0 * GENEROUS_FACTOR, 2)
    )
    assert round(sum(content["criterion_marks"].values()), 2) == pytest.approx(
        round(15.0 * GENEROUS_FACTOR, 2)
    )


def test_sparse_drops_the_similarity_score_and_one_criterion():
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "sparse")
    assert content["similarity_score"] is None
    assert len(content["criterion_marks"]) == 1


def test_outline_keeps_notes_but_drops_the_linkage():
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "outline")
    assert content["note_count"] == base["note_count"]
    assert content["assignment_ids"] == []
    assert content["submission_ids"] == []


def test_overclaimed_adds_an_assignment_the_unit_does_not_own():
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "overclaimed")
    assert "A2" in content["assignment_ids"]
    assert "A1" in content["assignment_ids"]


@pytest.mark.parametrize("strategy", ["generous", "sparse", "zzz"])
def test_an_unknown_submission_strategy_is_a_loud_failure(strategy):
    """A silently unknown strategy would produce a candidate nobody chose."""
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    if strategy in SUBMISSION_STRATEGIES:
        return
    with pytest.raises(ValueError):
        make_candidate(item, base, strategy)


@pytest.mark.parametrize("strategy", ["outline", "overclaimed", "zzz"])
def test_an_unknown_unit_strategy_is_a_loud_failure(strategy):
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    if strategy in UNIT_STRATEGIES:
        return
    with pytest.raises(ValueError):
        make_candidate(item, base, strategy)


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        make_candidate(WorkItem(kind="essay", item_id="E1"), {}, "strict")


# ---------------------------------------------------------------------------
# Alignment: the merge key, recomputed from the plan
# ---------------------------------------------------------------------------


def test_a_clean_strict_candidate_satisfies_every_obligation():
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "strict")
    alignment = align_submission(content, plan_scope(_state()["plan_locked"]))
    assert alignment.score == 1.0
    assert set(alignment.satisfied) == set(SUBMISSION_OBLIGATIONS)
    assert alignment.violated == ()


def test_generous_still_satisfies_the_plan_when_the_base_is_well_under_max():
    """Generous is the 'nicest' candidate, not necessarily a broken one.

    With the base at 15/20, a ten-percent uplift is still within the rubric, so
    generous ties strict on alignment and loses only on the declared-order
    tie-break. That is exactly the line 707 pitfall: the nicest output is
    rejected without ever breaking a screen.
    """
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "generous")
    alignment = align_submission(content, plan_scope(_state()["plan_locked"]))
    assert alignment.violated == ()
    assert alignment.score == 1.0


def test_generous_breaks_the_rubric_when_the_base_is_near_the_maximum():
    state = _state(
        draft_grades=[_row(awarded_marks=19.0, criterion_marks={"correctness": 10.0, "tests": 9.0})]
    )
    item = build_work_items(state)[0]
    base = build_base(state, item)
    content = make_candidate(item, base, "generous")
    # 19 * 1.10 = 20.9, over the 20 maximum.
    assert content["awarded_marks"] == pytest.approx(20.9)
    alignment = align_submission(content, plan_scope(state["plan_locked"]))
    assert alignment.violated == ("within_rubric_maximum",)
    assert alignment.score == pytest.approx(0.6)


def test_sparse_breaks_similarity_and_criterion_completeness():
    item = build_work_items(_state())[0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "sparse")
    alignment = align_submission(content, plan_scope(_state()["plan_locked"]))
    assert set(alignment.violated) == {"similarity_attached", "criteria_complete"}
    # Three satisfied, two violated, out of five: (3 - 2) / 5.
    assert alignment.score == pytest.approx(0.2)


def test_a_late_mismatch_is_caught_against_the_store():
    content = {
        "awarded_marks": 10.0,
        "max_marks": 20.0,
        "similarity_score": 0.1,
        "late": True,
        "expected_late": False,
        "criterion_marks": {"correctness": 10.0},
        "criterion_ids": ["correctness"],
        "assignment_id": "A1",
    }
    alignment = align_submission(content, plan_scope(_state()["plan_locked"]))
    assert "late_accurate" in alignment.violated


def test_an_assignment_outside_the_plan_lowers_alignment():
    content = {
        "awarded_marks": 10.0,
        "max_marks": 20.0,
        "similarity_score": 0.1,
        "late": False,
        "expected_late": False,
        "criterion_marks": {"correctness": 10.0},
        "criterion_ids": ["correctness"],
        "assignment_id": "A9",
    }
    scope = plan_scope({"units": ["1"], "assignment_ids": ["A1"]})
    alignment = align_submission(content, scope)
    assert "assignment_in_plan" in alignment.violated


def test_an_empty_plan_scope_cannot_fail_the_plan_obligation():
    content = {
        "awarded_marks": 10.0,
        "max_marks": 20.0,
        "similarity_score": 0.1,
        "late": False,
        "expected_late": False,
        "criterion_marks": {"correctness": 10.0},
        "criterion_ids": ["correctness"],
        "assignment_id": "A9",
    }
    alignment = align_submission(content, {"units": (), "assignment_ids": ()})
    assert "assignment_in_plan" in alignment.satisfied


def test_a_complete_unit_candidate_satisfies_every_obligation():
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "complete")
    alignment = align_unit(content, plan_scope(_state()["plan_locked"]))
    assert alignment.score == 1.0
    assert set(alignment.satisfied) == set(UNIT_OBLIGATIONS)


def test_an_outline_unit_candidate_breaks_the_two_linkage_obligations():
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "outline")
    alignment = align_unit(content, plan_scope(_state()["plan_locked"]))
    assert set(alignment.violated) == {
        "assignments_in_unit",
        "submissions_in_unit",
    }
    # Three satisfied, two violated: (3 - 2) / 5.
    assert alignment.score == pytest.approx(0.2)


def test_an_overclaimed_unit_candidate_is_penalised_for_claiming_too_much():
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "overclaimed")
    alignment = align_unit(content, plan_scope(_state()["plan_locked"]))
    # Claiming a foreign assignment breaks both "the linkage is right" and
    # "nothing was claimed that the unit does not own".
    assert set(alignment.violated) == {"assignments_in_unit", "no_overclaim"}
    assert alignment.score == pytest.approx(0.2)


def test_alignment_to_plan_dispatches_on_kind():
    sub = WorkItem(kind=KIND_SUBMISSION, item_id="S1")
    unit = WorkItem(kind=KIND_UNIT, item_id="1", unit="1")
    sub_content = {
        "awarded_marks": 1,
        "max_marks": 2,
        "similarity_score": 0.1,
        "late": False,
        "expected_late": False,
        "criterion_marks": {"correctness": 1},
        "criterion_ids": ["correctness"],
        "assignment_id": "A1",
    }
    assert alignment_to_plan(sub, sub_content, {}).score == 1.0
    with pytest.raises(ValueError):
        alignment_to_plan(WorkItem(kind="essay", item_id="E1"), {}, {})


def test_the_score_never_goes_below_zero():
    """Four violations of five would be negative; the metric clamps."""
    item = [i for i in build_work_items(_state()) if i.kind == KIND_UNIT][0]
    base = build_base(_state(), item)
    content = make_candidate(item, base, "overclaimed")
    content["note_count"] = 0  # also break notes_covered
    content["assignment_ids"] = list(content["assignment_ids"]) + ["A9"]
    alignment = align_unit(content, plan_scope(_state()["plan_locked"]))
    assert alignment.score >= 0.0


# ---------------------------------------------------------------------------
# Merge: select the best-aligned, log the rest
# ---------------------------------------------------------------------------


def _pure_merge(state=None, *, candidates=None, items=None):
    state = state or _state()
    items = items if items is not None else [
        i.to_dict() for i in build_work_items(state)
    ]
    if items and "base" not in items[0]:
        for item, work in zip(items, build_work_items(state)):
            item["base"] = build_base(state, work)
    scope = plan_scope(state["plan_locked"])
    if candidates is None:
        candidates = []
        for item in items:
            work = WorkItem(
                kind=item["kind"],
                item_id=item["item_id"],
                assignment_id=item.get("assignment_id", ""),
                unit=item.get("unit", ""),
                priority=item["priority"],
            )
            for strategy in (
                SUBMISSION_STRATEGIES
                if work.kind == KIND_SUBMISSION
                else UNIT_STRATEGIES
            ):
                content = make_candidate(work, item["base"], strategy)
                candidates.append(
                    {
                        "key": work.key,
                        "kind": work.kind,
                        "item_id": work.item_id,
                        "strategy": strategy,
                        "attempt": 0,
                        "content": content,
                        "self_reported_alignment": 1.0,
                    }
                )
    return merge_results(items, candidates, scope)


def test_merge_selects_exactly_one_candidate_per_item():
    merged = _pure_merge()
    assert len(merged["selected"]) == 4
    assert merged["stats"]["selected"] == merged["stats"]["items"] == 4


def test_merge_logs_every_candidate_including_the_losers():
    merged = _pure_merge()
    assert len(merged["log"]) == 12  # 4 items x 3 strategies
    assert merged["stats"]["candidates"] == 12
    assert merged["stats"]["rejected"] == 8


def test_the_selected_candidate_is_the_highest_alignment_for_its_item():
    merged = _pure_merge()
    log = merged["log"]
    for selection in merged["selected"]:
        scores = [r["alignment"] for r in log if r["key"] == selection["key"]]
        assert selection["alignment"] == max(scores)


def test_each_item_has_exactly_one_marked_winner_in_the_log():
    merged = _pure_merge()
    for key in {r["key"] for r in merged["log"]}:
        winners = [r for r in merged["log"] if r["key"] == key and r["selected"]]
        assert len(winners) == 1


def test_a_lying_self_reported_alignment_cannot_win():
    """The candidate's own score is logged, never trusted.

    Two submission candidates: one clean (strict) but self-reporting 0.0, one
    sparse (no similarity, an incomplete rubric) but self-reporting 1.0. The
    clean one must win, and the liar's recomputed score must show the gap.
    """
    state = _state()
    item = build_work_items(state)[0]
    base = build_base(state, item)
    items = [
        {
            **item.to_dict(),
            "base": base,
        }
    ]
    candidates = [
        {
            "key": item.key,
            "kind": KIND_SUBMISSION,
            "item_id": item.item_id,
            "strategy": "strict",
            "content": make_candidate(item, base, "strict"),
            "self_reported_alignment": 0.0,
        },
        {
            "key": item.key,
            "kind": KIND_SUBMISSION,
            "item_id": item.item_id,
            "strategy": "sparse",
            "content": make_candidate(item, base, "sparse"),
            "self_reported_alignment": 1.0,
        },
    ]
    merged = merge_results(items, candidates, plan_scope(state["plan_locked"]))
    assert merged["selected"][0]["strategy"] == "strict"
    liar = next(r for r in merged["log"] if r["strategy"] == "sparse")
    assert liar["self_reported_alignment"] == 1.0
    assert liar["alignment"] < 1.0


def test_a_tie_is_broken_by_the_declared_strategy_order():
    state = _state()
    item = build_work_items(state)[0]
    base = build_base(state, item)
    items = [{**item.to_dict(), "base": base}]
    good = make_candidate(item, base, "strict")
    candidates = [
        {
            "key": item.key,
            "kind": KIND_SUBMISSION,
            "item_id": item.item_id,
            "strategy": "zzz",
            "content": dict(good),
            "self_reported_alignment": 0.0,
        },
        {
            "key": item.key,
            "kind": KIND_SUBMISSION,
            "item_id": item.item_id,
            "strategy": "strict",
            "content": dict(good),
            "self_reported_alignment": 0.0,
        },
    ]
    merged = merge_results(items, candidates, plan_scope(state["plan_locked"]))
    assert merged["selected"][0]["strategy"] == "strict"


def test_an_item_with_no_candidate_is_logged_as_a_decision_with_no_winner():
    state = _state()
    items = [i.to_dict() for i in build_work_items(state)]
    for item, work in zip(items, build_work_items(state)):
        item["base"] = build_base(state, work)
    merged = merge_results(items, [], plan_scope(state["plan_locked"]))
    assert merged["selected"] == []
    assert len(merged["log"]) == 4
    assert all(r["alignment"] is None for r in merged["log"])
    assert all(not r["selected"] for r in merged["log"])


def test_quality_is_priority_weighted():
    """One perfect submission and one broken unit: (2*1 + 1*0) / 3."""
    scope = {"units": ("1",), "assignment_ids": ("A1",)}
    items = [
        {
            "kind": KIND_SUBMISSION,
            "item_id": "S1",
            "assignment_id": "A1",
            "priority": PRIORITY_SUBMISSION,
            "base": {},
        },
        {
            "kind": KIND_UNIT,
            "item_id": "1",
            "unit": "1",
            "priority": PRIORITY_UNIT,
            "base": {},
        },
    ]
    good = {
        "awarded_marks": 10.0,
        "max_marks": 20.0,
        "similarity_score": 0.1,
        "late": False,
        "expected_late": False,
        "criterion_marks": {"correctness": 10.0},
        "criterion_ids": ["correctness"],
        "assignment_id": "A1",
    }
    bad = {
        "unit": "1",
        "note_count": 0,
        "assignment_ids": [],
        "submission_ids": [],
        "expected_note_count": 1,
        "expected_assignment_ids": ["A1"],
        "expected_submission_ids": ["S1"],
    }
    candidates = [
        {
            "key": "submission:S1",
            "kind": KIND_SUBMISSION,
            "item_id": "S1",
            "strategy": "strict",
            "content": good,
        },
        {
            "key": "unit:1",
            "kind": KIND_UNIT,
            "item_id": "1",
            "strategy": "complete",
            "content": bad,
        },
    ]
    merged = merge_results(items, candidates, scope)
    assert merged["quality"] == pytest.approx(round(2.0 / 3.0, 6))
    assert merged["quality_by_kind"][KIND_SUBMISSION] == 1.0
    assert merged["quality_by_kind"][KIND_UNIT] == 0.0
    assert merged["stats"]["priority_weight"] == 3


def test_the_merge_records_the_candidate_marks_for_the_log():
    merged = _pure_merge()
    submission_records = [
        r for r in merged["log"] if r["kind"] == KIND_SUBMISSION
    ]
    assert submission_records
    for record in submission_records:
        assert record["awarded_marks"] is not None
        assert record["max_marks"] is not None


# ---------------------------------------------------------------------------
# make_merge filters to the current attempt
# ---------------------------------------------------------------------------


def test_make_merge_ignores_candidates_from_an_earlier_attempt(tmp_path):
    """A retried pass appends to the same channel; the merge must not mix them.

    Without the filter the merge would see both attempts' candidates for one item
    and could select from a draft that is no longer current.
    """
    ctx = build_run_context("merge-attempt", output_dir=tmp_path)
    state = _state()
    work = build_work_items(state)[0]
    base = build_base(state, work)
    item = {**work.to_dict(), "base": base}

    def candidate(attempt: int, strategy: str, marks: float):
        content = make_candidate(work, base, strategy)
        content["awarded_marks"] = marks
        return {
            "key": work.key,
            "kind": work.kind,
            "item_id": work.item_id,
            "strategy": strategy,
            "attempt": attempt,
            "content": content,
            "self_reported_alignment": 1.0,
        }

    node_state = {
        "attempts": 1,
        "plan_locked": state["plan_locked"],
        "swarm_mode": "parallel",
        "swarm_t0": 0.0,
        "swarm_items": [item],
        "swarm_intervals": [
            {"attempt": 0, "start": 0.0, "end": 1.0},
            {"attempt": 1, "start": 0.0, "end": 1.0},
        ],
        "swarm_candidates": [
            candidate(0, "strict", 5.0),
            candidate(1, "strict", 15.0),
        ],
    }
    update = make_merge(ctx)(node_state)
    assert update["swarm_stats"]["candidates"] == 1
    assert update["swarm_selected"][0]["content"]["awarded_marks"] == pytest.approx(
        15.0
    )


# ---------------------------------------------------------------------------
# Timing: peak overlap, computed from the intervals
# ---------------------------------------------------------------------------


def test_no_intervals_yields_an_empty_summary():
    summary = timing_summary([], mode="parallel", stage_ms=0.0)
    assert summary["tasks"] == 0
    assert summary["max_overlap"] == 0
    assert summary["span_ms"] == 0.0


def test_non_overlapping_intervals_have_peak_overlap_one():
    intervals = [
        {"start": 0.0, "end": 1.0},
        {"start": 1.0, "end": 2.0},
        {"start": 2.0, "end": 3.0},
    ]
    summary = timing_summary(intervals, mode="sequential", stage_ms=3000.0)
    assert summary["max_overlap"] == 1


def test_three_fully_overlapping_intervals_have_peak_overlap_three():
    intervals = [
        {"start": 0.0, "end": 3.0},
        {"start": 0.0, "end": 3.0},
        {"start": 0.0, "end": 3.0},
    ]
    summary = timing_summary(intervals, mode="parallel", stage_ms=3000.0)
    assert summary["max_overlap"] == 3


def test_the_summary_is_deterministic_and_ascii_shaped():
    intervals = [
        {"start": 0.0, "end": 1.0},
        {"start": 0.5, "end": 1.5},
    ]
    first = timing_summary(intervals, mode="parallel", stage_ms=1500.0)
    second = timing_summary(intervals, mode="parallel", stage_ms=1500.0)
    assert first == second
    assert all(isinstance(v, (int, float, str)) for v in first.values())


# ---------------------------------------------------------------------------
# Topology, read off the compiled graph
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def swarm_ctx(tmp_path_factory):
    return build_run_context(
        "swarm-topology", output_dir=tmp_path_factory.mktemp("swarm-topology")
    )


def _edge_set(graph):
    return {(s, t) for s, t, _ in swarm_edges(graph)}


def test_the_parallel_graph_compiles_the_fan_out_and_merge(swarm_ctx):
    info = inspect_swarm(swarm_ctx, parallel=True)
    assert {"swarm_dispatch", "swarm_worker", "swarm_merge"} <= set(info["nodes"])


def test_the_fan_out_edge_is_conditional_and_targets_the_worker(swarm_ctx):
    info = inspect_swarm(swarm_ctx, parallel=True)
    assert ("swarm_dispatch", "swarm_worker", True) in info["edges"]


def test_the_worker_fans_back_in_at_the_merge(swarm_ctx):
    info = inspect_swarm(swarm_ctx, parallel=True)
    assert ("swarm_worker", "swarm_merge") in _edge_set(
        build_swarm_graph(swarm_ctx, parallel=True)
    )


def test_workers_feeds_the_dispatch_and_the_merge_feeds_validate(swarm_ctx):
    info = inspect_swarm(swarm_ctx, parallel=True)
    edges = {(s, t) for s, t, _ in info["edges"]}
    assert ("workers", "swarm_dispatch") in edges
    assert ("swarm_merge", "validate") in edges


def test_the_sequential_graph_has_no_send_worker(swarm_ctx):
    info = inspect_swarm(swarm_ctx, parallel=False)
    nodes = set(info["nodes"])
    assert "swarm_sequential" in nodes
    assert "swarm_worker" not in nodes
    assert "swarm_dispatch" not in nodes


def test_the_sequential_graph_reaches_the_same_merge(swarm_ctx):
    graph = build_swarm_graph(swarm_ctx, parallel=False)
    edges = {(s, t) for s, t, _ in swarm_edges(graph)}
    assert ("workers", "swarm_sequential") in edges
    assert ("swarm_sequential", "swarm_merge") in edges


def test_lab7s_back_edge_survives_under_the_swarm(swarm_ctx):
    graph = build_swarm_graph(swarm_ctx, parallel=True)
    edges = {(s, t) for s, t, _ in swarm_edges(graph)}
    assert ("validate", "workers") in edges
    assert ("validate", "assemble") in edges
    assert ("validate", "escalate") in edges


def test_publish_is_still_only_reachable_through_the_gate(swarm_ctx):
    graph = build_swarm_graph(swarm_ctx, parallel=True)
    into_publish = {s for s, t, _ in swarm_edges(graph) if t == "publish"}
    assert into_publish == {"gate"}


def test_the_topology_lines_are_deterministic_and_ascii(swarm_ctx):
    first = inspect_swarm(swarm_ctx, parallel=True)["lines"]
    second = inspect_swarm(swarm_ctx, parallel=True)["lines"]
    assert first == second
    assert all(ord(ch) < 128 for line in first for ch in line)


def test_the_topology_lines_render_the_fan_out(swarm_ctx):
    text = "\n".join(inspect_swarm(swarm_ctx, parallel=True)["lines"])
    assert "swarm_dispatch -> swarm_worker" in text
    assert "swarm_worker -> swarm_merge" in text


def test_compiling_without_a_checkpointer_warns(swarm_ctx, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        build_swarm_graph(swarm_ctx, parallel=True, checkpointer=None)
    assert "checkpointer" in caplog.text


# ---------------------------------------------------------------------------
# The fan-out itself
# ---------------------------------------------------------------------------


def _items_state(**overrides):
    """The synthetic state with ``swarm_items`` populated, as ``dispatch`` does."""
    state = _state(**overrides)
    state["swarm_items"] = [
        {**work.to_dict(), "base": build_base(state, work)}
        for work in build_work_items(state)
    ]
    return state


def test_fan_out_emits_one_send_per_item_per_strategy():
    sends = fan_out(_items_state(attempts=0))
    assert len(sends) == 12  # 4 items x 3 strategies


def test_each_send_carries_the_item_the_base_the_strategy_and_the_scope():
    send = fan_out(_items_state(attempts=0))[0]
    assert send.node == "swarm_worker"
    assert send.arg["strategy"] == SUBMISSION_STRATEGIES[0]
    assert "base" in send.arg
    assert send.arg["attempt"] == 0
    assert set(send.arg["plan_scope"]) == {"units", "assignment_ids"}


def test_fan_out_uses_the_submission_strategies_for_a_submission_item():
    sends = [
        s
        for s in fan_out(_items_state(attempts=0))
        if s.arg["item"]["kind"] == KIND_SUBMISSION
    ]
    assert len(sends) == 6  # 2 submissions x 3
    assert {s.arg["strategy"] for s in sends} == set(SUBMISSION_STRATEGIES)


# ---------------------------------------------------------------------------
# State plumbing, restated for the swarm's own nodes
# ---------------------------------------------------------------------------


def test_every_key_a_swarm_node_returns_is_declared_in_swarm_state():
    """LangGraph silently drops state keys the TypedDict does not declare.

    The Lab 7 suite has the same test for ``nodes.py`` and ``specialists.py``.
    The swarm is a second topology, so its nodes get their own copy rather than
    relying on Lab 7's contract.
    """
    package = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "course_organizer"
        / "graph"
    )
    tree = ast.parse((package / "swarm.py").read_text(encoding="utf-8"))
    # A TypedDict flattens the inherited annotations into its own, so this set is
    # Lab 7's keys plus the swarm's own.
    declared = set(SwarmState.__annotations__)
    assert {
        "swarm_items",
        "swarm_candidates",
        "swarm_intervals",
        "swarm_selected",
        "swarm_merge_log",
        "swarm_quality",
        "swarm_timing",
        "unit_coverage",
    } <= declared
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in {
            "swarm_dispatch",
            "swarm_worker",
            "swarm_sequential",
            "swarm_merge",
        }:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and key.value not in declared
                    ):
                        offenders.append(f"{fn.name}:{key.value}")
    assert not offenders, (
        "these keys are returned at the top level of a swarm node but are not "
        f"declared in SwarmState, so LangGraph discards them: {sorted(offenders)}"
    )


def test_the_append_reducer_is_what_keeps_concurrent_writes(tmp_path):
    """Thirty-nine workers write ``swarm_candidates``; LastValue would clobber.

    Checked structurally: the annotation must carry the ``append`` reducer, not be
    a bare ``list``. The functional half of the same claim is
    ``test_thirty_nine_candidates_are_produced_and_the_losers_are_logged``,
    which would see one candidate instead of thirty-nine if the reducer were
    absent.
    """
    hints = get_type_hints(SwarmState, include_extras=True)
    for channel in ("swarm_candidates", "swarm_intervals"):
        metadata = getattr(hints[channel], "__metadata__", ())
        assert metadata, (
            f"{channel} must carry the append reducer or concurrent workers "
            f"will overwrite each other"
        )
        assert metadata[0] is append


# ---------------------------------------------------------------------------
# The evidence record on disk
# ---------------------------------------------------------------------------


def test_write_candidate_artifact_writes_a_json_evidence_record(tmp_path):
    candidate = {
        "key": "submission:S1",
        "strategy": "strict",
        "content": {"awarded_marks": 15.0},
    }
    path = Path(write_candidate_artifact(tmp_path, 0, candidate))
    assert path.exists()
    assert path.name == "a00__submission_S1__strict.json"
    assert json.loads(path.read_text(encoding="utf-8")) == candidate


def test_the_evidence_filename_carries_the_attempt(tmp_path):
    path = Path(
        write_candidate_artifact(
            tmp_path, 2, {"key": "unit:1", "strategy": "complete"}
        )
    )
    assert path.name.startswith("a02__")
    assert path.parent.name == "swarm"


def test_slug_makes_a_key_filename_safe():
    assert slug("submission:DS-A1-S001") == "submission_DS-A1-S001"
    assert slug("") == "item"


def test_the_package_star_import_resolves_every_declared_name():
    """``__all__`` must be reachable, not just declared.

    Lab 8 found a real defect here: ``graph.__all__`` listed ``CHECKPOINTED_TYPES``
    and ``checkpoint_serializer`` but never imported them, so
    ``from course_organizer.graph import *`` raised ``AttributeError``. A public
    API list that names something the module does not have is worse than no list,
    because it fails at the reader's end rather than the author's. Lab 8's own
    exports are covered by the same test, which is why it lives in this file even
    though the defect was in Lab 7's module.
    """
    import course_organizer.graph as package

    missing = [name for name in package.__all__ if not hasattr(package, name)]
    assert not missing, (
        "these names are in graph.__all__ but are not attributes of the "
        f"module, so `from course_organizer.graph import *` fails: {missing}"
    )
    for name in ("SWARM_MAX_CONCURRENCY", "run_swarm", "SwarmState"):
        assert name in package.__all__


# ---------------------------------------------------------------------------
# End to end: the measured acceptance criterion
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parallel_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-parallel")
    return run_swarm(
        run_id="parallel",
        root=root,
        output_dir=root / "parallel",
        parallel=True,
    )


@pytest.fixture(scope="module")
def sequential_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-sequential")
    return run_swarm(
        run_id="sequential",
        root=root,
        output_dir=root / "sequential",
        parallel=False,
    )


def test_the_seed_corpus_fans_out_over_eight_submissions_and_five_units(parallel_run):
    stats = parallel_run.state["swarm_stats"]
    assert stats["submission_items"] == 8
    assert stats["unit_items"] == 5
    assert stats["items"] == 13


def test_every_work_item_is_processed_and_selected(parallel_run):
    stats = parallel_run.state["swarm_stats"]
    assert stats["selected"] == 13
    assert len(parallel_run.state["swarm_selected"]) == 13


def test_thirty_nine_candidates_are_produced_and_the_losers_are_logged(parallel_run):
    stats = parallel_run.state["swarm_stats"]
    assert stats["candidates"] == 39
    assert stats["rejected"] == 26
    assert len(parallel_run.state["swarm_merge_log"]) == 39


def test_the_clean_parallel_run_reaches_the_gate_with_all_four_checks(parallel_run):
    assert parallel_run.awaiting_gate
    assert parallel_run.status is CourseStatus.DRAFT
    validation = parallel_run.state["validation"]
    assert validation.passed
    assert tuple(validation.checks_run) == DOC_CHECKS
    assert validation.failing == []


def test_the_merge_rebuilds_the_draft_grades_and_the_unit_coverage(parallel_run):
    assert len(parallel_run.state["draft_grades"]) == 8
    assert len(parallel_run.state["unit_coverage"]) == 5
    assert {row.submission_id for row in parallel_run.state["draft_grades"]} == {
        f"DS-A1-S00{i}" for i in range(1, 5)
    } | {f"DS-A2-S00{i}" for i in (5, 6)} | {f"DS-A3-S00{i}" for i in (7, 8)}


def test_the_clean_run_selects_the_strict_and_complete_strategies(parallel_run):
    by_kind = {}
    for selection in parallel_run.state["swarm_selected"]:
        by_kind.setdefault(selection["kind"], set()).add(selection["strategy"])
    assert by_kind[KIND_SUBMISSION] == {"strict"}
    assert by_kind[KIND_UNIT] == {"complete"}


def test_the_clean_parallel_quality_is_perfect(parallel_run):
    assert parallel_run.state["swarm_quality"] == 1.0
    by_kind = parallel_run.state["swarm_quality_by_kind"]
    assert by_kind[KIND_SUBMISSION] == 1.0
    assert by_kind[KIND_UNIT] == 1.0


def test_the_parallel_and_sequential_runs_select_identical_candidates(
    parallel_run, sequential_run
):
    def winners(run):
        return {
            s["key"]: s["strategy"] for s in run.state["swarm_selected"]
        }

    assert winners(parallel_run) == winners(sequential_run)


def test_parallel_quality_matches_or_beats_sequential(
    parallel_run, sequential_run
):
    assert parallel_run.state["swarm_quality"] >= (
        sequential_run.state["swarm_quality"]
    )


def test_the_parallel_pass_is_lower_wall_clock_than_the_sequential_pass(
    parallel_run, sequential_run
):
    """The doc's acceptance criterion (line 704), measured not constructed."""
    par = parallel_run.state["swarm_timing"]["stage_ms"]
    seq = sequential_run.state["swarm_timing"]["stage_ms"]
    assert par < seq, (
        f"parallel stage {par}ms was not below sequential stage {seq}ms; the "
        f"acceptance criterion is not met on this machine"
    )


def test_the_parallel_fan_out_really_ran_concurrently(parallel_run, sequential_run):
    """A timing fluke cannot pass the wall-clock check on its own."""
    assert parallel_run.state["swarm_timing"]["max_overlap"] >= 2
    assert sequential_run.state["swarm_timing"]["max_overlap"] == 1


def test_the_fan_out_stayed_within_the_measured_pool_width(parallel_run):
    """The bound is checkable without a stopwatch.

    Peak overlap counts intervals that were in flight at the same instant, so it
    cannot exceed the pool width. That makes the concurrency claim structural:
    the fan-out both overlapped *and* stayed bounded, and an unbounded pool --
    which measured between 0.71x and 1.58x of sequential depending on the
    process -- could not pass this.
    """
    assert parallel_run.state["swarm_timing"]["max_overlap"] <= (
        SWARM_MAX_CONCURRENCY
    )


def test_the_pool_width_is_the_measured_one():
    """Pinned so a later edit to this constant is a visible decision."""
    assert SWARM_MAX_CONCURRENCY == 8
    assert 2 < SWARM_MAX_CONCURRENCY <= 16


def test_the_adapter_pins_max_concurrency_into_the_config():
    """The bound is applied, not merely documented."""
    seen: list[dict] = []

    class Recorder:
        def invoke(self, *args, **kwargs):
            seen.append(args[1] if len(args) > 1 else kwargs.get("config"))
            return {"ok": True}

    adapter = _BoundedFanOut(Recorder(), 5)
    assert adapter.invoke({"seed": 1}, {"configurable": {"thread_id": "t"}}) == {
        "ok": True
    }
    assert adapter.invoke({"seed": 1}, config={"configurable": {}}) == {"ok": True}
    assert [c["max_concurrency"] for c in seen] == [5, 5]
    # The caller's own config entries must survive.
    assert seen[0]["configurable"] == {"thread_id": "t"}


def test_the_adapter_rejects_a_nonsense_width_rather_than_deadlocking():
    assert _BoundedFanOut(object(), 0).width == 1
    assert _BoundedFanOut(object(), -3).width == 1


def test_the_adapter_forwards_everything_else_to_the_graph():
    """``get_graph()`` and ``get_state()`` must still reach the compiled graph."""

    class Fake:
        def get_graph(self):
            return "the compiled graph"

    assert _BoundedFanOut(Fake(), 4).get_graph() == "the compiled graph"


def test_both_modes_ran_the_same_number_of_tasks(parallel_run, sequential_run):
    assert parallel_run.state["swarm_timing"]["tasks"] == 39
    assert sequential_run.state["swarm_timing"]["tasks"] == 39


def test_every_candidate_has_a_durable_evidence_record(parallel_run):
    directory = Path(parallel_run.context.output_dir) / "swarm"
    files = sorted(directory.glob("a00__*.json"))
    assert len(files) == 39
    sample = json.loads(files[0].read_text(encoding="utf-8"))
    assert sample["content"]
    assert "self_reported_alignment" in sample


def test_the_nicest_candidate_is_rejected_in_favour_of_the_aligned_one(parallel_run):
    """Line 707: picking the 'nicest' output quietly breaks coverage.

    For at least one submission the highest-marked candidate is the generous one,
    and it loses to strict because strict is the one that satisfies the plan.
    """
    log = parallel_run.state["swarm_merge_log"]
    found = False
    for key in {r["key"] for r in log if r["kind"] == KIND_SUBMISSION}:
        records = [r for r in log if r["key"] == key]
        richest = max(records, key=lambda r: r["awarded_marks"])
        winner = next(r for r in records if r["selected"])
        if not richest["selected"] and (
            richest["awarded_marks"] > winner["awarded_marks"]
        ):
            found = True
            break
    assert found, "no submission demonstrated the 'nicest loses' selection"


def test_the_run_records_the_work_items_in_priority_order(parallel_run):
    items = parallel_run.state["swarm_items"]
    kinds = [i["kind"] for i in items]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == KIND_SUBMISSION else 1)
    assert [i["priority"] for i in items] == sorted(
        (i["priority"] for i in items), reverse=True
    )


# ---------------------------------------------------------------------------
# The gate and the resume
# ---------------------------------------------------------------------------


def test_the_swarm_stops_before_anything_is_published(parallel_run):
    assert parallel_run.state.get("published_path") in (None, "")
    assert not parallel_run.published


def test_approval_publishes_the_merged_grades(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-approved")
    suspended = run_swarm(
        run_id="approved",
        root=root,
        output_dir=root / "approved",
        parallel=True,
    )
    assert suspended.awaiting_gate
    resumed = resume_swarm("approved", DECISION_APPROVE, root=root)
    assert resumed.status is CourseStatus.PUBLISHED
    assert resumed.published
    path = Path(resumed.state["published_path"])
    text = path.read_text(encoding="utf-8")
    assert "## Approval Recorded" in text
    assert all(row.approved_by for row in resumed.state["draft_grades"])


def test_resuming_does_not_re_run_the_swarm_nodes(tmp_path_factory):
    """``Command(resume=...)`` re-enters at the gate, not from the top."""
    root = tmp_path_factory.mktemp("lab08-resume")
    suspended = run_swarm(
        run_id="resume",
        root=root,
        output_dir=root / "resume",
        parallel=True,
    )
    before = suspended.nodes_ran()
    resumed = resume_swarm("resume", DECISION_APPROVE, root=root)
    after = resumed.nodes_ran()
    newly_ran = after[len(before):]
    assert not {n for n in newly_ran if n.startswith("swarm_")}
    assert "workers" not in newly_ran
    assert newly_ran == ["gate", "publish"]


def test_a_run_may_be_approved_and_then_is_persisted_for_another_process(
    tmp_path_factory,
):
    """``resume_swarm`` reopens the checkpoint from disk, so it is not in-memory."""
    root = tmp_path_factory.mktemp("lab08-crossproc")
    run_swarm(
        run_id="crossproc",
        root=root,
        output_dir=root / "crossproc",
        parallel=True,
    )
    from course_organizer.graph.runner import checkpoint_path

    assert checkpoint_path(root, "crossproc").exists()


# ---------------------------------------------------------------------------
# The bound still stops a non-converging swarm
# ---------------------------------------------------------------------------


def test_the_swarm_retries_and_escalates_when_it_cannot_converge(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-exhaustion")
    run = run_swarm(
        run_id="exhaustion",
        root=root,
        output_dir=root / "exhaustion",
        parallel=True,
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )
    assert run.status is CourseStatus.ESCALATED
    assert run.attempts_made() == MAX_ATTEMPTS
    assert "escalate" in run.nodes_ran()


def test_the_swarm_stage_runs_on_every_retry(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-every-retry")
    run = run_swarm(
        run_id="every-retry",
        root=root,
        output_dir=root / "every-retry",
        parallel=True,
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )
    nodes = run.nodes_ran()
    assert nodes.count("swarm_dispatch") == MAX_ATTEMPTS
    assert nodes.count("swarm_merge") == MAX_ATTEMPTS


def test_the_exhaustion_carries_a_diagnosis_and_the_failing_draft(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab08-diagnosis")
    run = run_swarm(
        run_id="diagnosis",
        root=root,
        output_dir=root / "diagnosis",
        parallel=True,
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )
    escalation = run.state["escalation"]
    assert escalation.reason == "retry_budget_exhausted"
    assert escalation.failing
    assert escalation.pending_draft["draft_grades"]
    assert "rather than retrying forever" in escalation.diagnosis


def test_an_injected_defect_flows_through_the_swarm_rather_than_being_masked(
    tmp_path_factory,
):
    """A pass with no similarity scores must fail validation, not be ignored.

    The scripting injection makes the similarity specialist skip its pass, so
    every draft has ``similarity_score=None``. The swarm then selects the best
    candidate it can -- but no calibration can invent a score, so validation
    still fails. If the swarm masked the defect, the acceptance run would be a
    lie.
    """
    root = tmp_path_factory.mktemp("lab08-injection")
    run = run_swarm(
        run_id="injection",
        root=root,
        output_dir=root / "injection",
        parallel=True,
        fail_until_attempt=MAX_ATTEMPTS + 10,
    )
    assert not run.state["validation"].passed
    assert "similarity_score_attached" in run.state["validation"].failing
    for selection in run.state["swarm_selected"]:
        if selection["kind"] == KIND_SUBMISSION:
            assert selection["content"]["similarity_score"] is None


def test_the_injection_recovers_on_the_retry(tmp_path_factory):
    """``fail_until_attempt=1`` fails the first pass and succeeds on the second."""
    root = tmp_path_factory.mktemp("lab08-recover")
    run = run_swarm(
        run_id="recover",
        root=root,
        output_dir=root / "recover",
        parallel=True,
        fail_until_attempt=1,
    )
    assert run.attempts_made() >= 1
    assert run.status is CourseStatus.DRAFT
    assert run.awaiting_gate
    assert run.state["validation"].passed
