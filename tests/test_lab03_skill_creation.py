"""Tests for Lab 3 -- skill creation.

Grouped to mirror the lab's claims, because the document's acceptance
criterion for Lab 3 ("the same skill works for two different cases without code
changes") is a claim about *reuse*, and reuse is not testable by calling a
function once and checking it did not raise.

Groups:

1. structural      -- the package is shaped as the document describes
2. the lock        -- immutability, integrity, reproducibility
3. drift           -- 2.12's named mitigation for this lab
4. the format skill -- templates are data; three renderers; no hidden state
5. the reuse proof -- four cases, one registry, no course constant in code
6. honesty         -- no fabricated execution; nothing ever "final"

The test that matters most is
``test_no_course_identifier_is_hard_coded``. It parses the skills package with
`ast` and fails on any course code or course name appearing in it. That is the
document's pitfall -- "keep skills case-agnostic; hard-coding one case breaks
reuse" -- enforced structurally, so it cannot be lost in a later edit and
reintroduced as a plausible-looking special case.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from course_organizer import HUMAN_AUTHORITY
from course_organizer.labs.lab03_skill_creation import (
    CASES,
    DOC_PSEUDOCODE,
    TEMPLATE_SPECS,
    CaseSpec,
    ReuseProof,
    SkillAgent,
    SkillErrorLookup,
    SkillRunResult,
    _course_wide_validation,
)
from course_organizer.skills import (
    BODY_STYLES,
    DEFERRED_KINDS,
    KIND_COVER_UNIT,
    KIND_DISTRIBUTE_ASSIGNMENT,
    KIND_GROUND_FACTS,
    KIND_TRACK_MILESTONES,
    KIND_VERIFY_STEP,
    MISSING,
    OBLIGATION_KINDS,
    SECTIONS,
    SKILL_NAMES,
    VOLATILE_FACT_KEYS,
    Document,
    DocumentStatus,
    LabStep,
    LockedPlan,
    Obligation,
    PlanDraft,
    PlanInputs,
    TemplateSpec,
    WorkspaceResult,
    build_default_skill_registry,
    expand,
    obligation_id,
    resolve_fact,
    stable_facts,
)
from course_organizer.skills.base import SkillError, SkillSpec
from course_organizer.skills.plan_skill import _sort_key

SKILLS_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "course_organizer" / "skills"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry():
    """A skill registry holding exactly the two documented skills."""
    return build_default_skill_registry(run_id="test-lab03")


@pytest.fixture
def inputs() -> PlanInputs:
    """A small, fully-specified plan input. No course constant in the skills."""
    return PlanInputs(
        course_code="TEST1",
        course_name="Test One",
        units=("1", "2"),
        assignment_ids=("A-1",),
        include_milestones=True,
        steps=(
            LabStep(
                step_id="S1",
                name="read",
                agent="notes",
                intent="read the store",
                acceptance_criteria=("every unit covered", "no version removed"),
            ),
        ),
        verified_facts={
            "course_code": "TEST1",
            "unit_count": 2,
            "total_available_marks": 40.0,
            "marks_by_assignment": {"A-1": 40.0},
            "computed_at": "2026-01-01T00:00:00+00:00",
        },
        as_of="2026-10-15",
    )


@pytest.fixture
def locked(inputs: PlanInputs) -> LockedPlan:
    """A locked plan built from `inputs`."""
    return expand(inputs).lock()


@pytest.fixture
def conforming(locked: LockedPlan) -> WorkspaceResult:
    """A result that satisfies every settleable obligation in `locked`."""
    return WorkspaceResult(
        course_code=locked.course_code,
        units_covered=locked.units,
        assignments_included=locked.assignment_ids,
        milestones_emitted=locked.include_milestones,
        completed_steps=(),
        reported_facts={
            "total_available_marks": 40.0,
            "marks_by_assignment.A-1": 40.0,
        },
        notes_by_unit={"1": 2, "2": 1},
        ground_fact_keys=("total_available_marks", "marks_by_assignment.A-1"),
    )


@pytest.fixture
def template() -> TemplateSpec:
    """A minimal valid template."""
    return TemplateSpec(
        template_id="t1",
        title="Test",
        audience="tester",
        sections=("header", "drift", "approval"),
        body_style="markdown",
    )


# ---------------------------------------------------------------------------
# 1. Structural: the shape the document asks for
# ---------------------------------------------------------------------------


class TestStructural:
    def test_registry_holds_exactly_the_two_documented_skills(self, registry):
        assert registry.names() == sorted(SKILL_NAMES)
        assert set(SKILL_NAMES) == {"plan", "format_organised"}

    def test_each_skill_declares_a_distinct_input_and_output_model(self, registry):
        """They must be genuinely different skills, not one skill renamed.

        Two specs sharing both models would satisfy "two callable skills" on a
        technicality while providing one capability.
        """
        specs = [registry.get(name) for name in SKILL_NAMES]
        inputs_models = {spec.input_model for spec in specs}
        output_models = {spec.output_model for spec in specs}
        assert len(inputs_models) == 2
        assert len(output_models) == 2

    def test_both_skills_declare_themselves_reusable(self, registry):
        """The declaration is verified by tests below, not just asserted."""
        for name in SKILL_NAMES:
            assert registry.get(name).case_agnostic is True

    def test_doc_pseudocode_is_recorded_verbatim(self):
        """The doc's own pseudocode, so the code can be read against 2.10."""
        assert "skill plan(inputs) -> Plan" in DOC_PSEUDOCODE
        assert "expand(inputs).lock()" in DOC_PSEUDOCODE
        assert "skill format_organised(result, template) -> Document" in (
            DOC_PSEUDOCODE
        )

    def test_obligations_cover_the_documented_outputs(self):
        """The doc's Output column: a locked plan, plus a rendered shell.

        Each obligation kind maps to one part of that output, so the closed
        vocabulary is traceable to the document rather than to taste.
        """
        assert set(OBLIGATION_KINDS) == {
            KIND_COVER_UNIT,
            KIND_DISTRIBUTE_ASSIGNMENT,
            KIND_TRACK_MILESTONES,
            KIND_VERIFY_STEP,
            KIND_GROUND_FACTS,
        }

    def test_a_second_registry_can_be_built_independently(self):
        """No module-level singleton registry, so tests cannot leak state."""
        first = build_default_skill_registry(run_id="a")
        second = build_default_skill_registry(run_id="b")
        assert first is not second
        assert first.ledger is not second.ledger
        assert len(first.ledger) == 0

    def test_registering_the_same_skill_twice_is_refused(self, registry):
        """Silent replacement would be indistinguishable from working code."""
        spec = registry.get("plan")
        with pytest.raises(SkillError) as exc:
            registry.register(spec)
        assert exc.value.code == "duplicate_skill"

    def test_unknown_skill_names_list_the_real_ones(self, registry):
        with pytest.raises(SkillError) as exc:
            registry.invoke("no_such_skill", {})
        assert exc.value.code == "unknown_skill"
        for name in SKILL_NAMES:
            assert name in str(exc.value)

    def test_invalid_input_is_rejected_before_the_handler_runs(self, registry):
        """A bad payload must not reach the handler.

        If validation ran after, a malformed input could already have
        half-expanded a draft before being rejected.
        """
        before = len(registry.ledger)
        with pytest.raises(SkillError) as exc:
            registry.invoke("plan", {"course_code": "X"})  # no units
        assert exc.value.code == "invalid_input"
        assert len(registry.ledger) == before

    def test_every_ledger_entry_records_its_declared_output_type(self, registry, inputs):
        registry.invoke("plan", inputs)
        for call in registry.ledger:
            assert call.output_type in {"LockedPlan", "Document"}
            assert call.ok is True


# ---------------------------------------------------------------------------
# 2. The lock
# ---------------------------------------------------------------------------


class TestTheLock:
    def test_a_locked_plan_cannot_be_edited(self, locked):
        """The document's whole premise: the blueprint is not negotiable."""
        with pytest.raises(ValidationError):
            locked.course_code = "HACKED"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            locked.obligation_count = 0  # type: ignore[misc]

    def test_a_locked_plan_cannot_be_edited_through_its_lists(self, locked):
        """`frozen=True` alone would NOT stop this.

        Pydantic blocks attribute rebinding but leaves list contents mutable,
        so a "locked" plan built on list fields is still editable via append --
        and the failure is completely silent. Every collection on LockedPlan is
        a tuple, which has no mutating methods at all.
        """
        with pytest.raises(AttributeError):
            locked.obligations.append("x")  # type: ignore[attr]
        with pytest.raises(AttributeError):
            locked.units.append("x")  # type: ignore[attr]
        with pytest.raises(AttributeError):
            locked.assignment_ids.append("x")  # type: ignore[arg]

    def test_editing_the_draft_after_locking_does_not_change_the_lock(
        self, inputs, locked
    ):
        """The aliasing trap: a lock that shares objects can be rewritten.

        If `lock()` held references rather than serialising, every edit below
        would appear inside `locked` -- history rewritten through the lock.
        """
        draft = expand(inputs)
        plan = draft.lock()

        draft.add_obligation(
            Obligation(
                obligation_id="OBL-cover_unit/99",
                kind=KIND_COVER_UNIT,
                subject="99",
                statement="added after locking",
            )
        )
        draft.units = draft.units + ("99",)
        draft.course_code = "HACKED"

        assert len(plan.obligations) == locked.obligation_count
        assert "99" not in plan.units
        assert plan.course_code == locked.course_code == "TEST1"
        assert plan.plan_sha256 == locked.plan_sha256
        assert plan.recompute_sha256() == plan.plan_sha256

    def test_locking_twice_from_one_draft_gives_the_same_hash(self, inputs):
        """`lock()` must not consume or mutate the draft."""
        draft = expand(inputs)
        assert draft.lock().plan_sha256 == draft.lock().plan_sha256

    def test_the_integrity_check_passes_on_an_untouched_plan(self, locked):
        assert locked.recompute_sha256() == locked.plan_sha256

    def test_a_tampered_plan_fails_its_own_integrity_check(self, locked):
        """Detectable even by a caller who reached past the frozen model.

        `model_construct` bypasses validation, so `frozen` can be sidestepped.
        The stored hash is what makes that sidestep *visible* rather than
        merely discouraged.
        """
        tampered = LockedPlan.model_construct(
            **{**locked.model_dump(), "units": ("1", "2", "3")}
        )
        with pytest.raises(SkillError) as exc:
            tampered.recompute_sha256()
        assert exc.value.code == "plan_tampered"

    def test_two_lockings_of_the_same_input_hash_identically(self, inputs):
        """Reproducibility: the scripted backend exists to make this possible."""
        assert expand(inputs).lock().plan_sha256 == expand(inputs).lock().plan_sha256

    def test_a_per_run_timestamp_cannot_change_the_hash(self, inputs):
        """The reason `VOLATILE_FACT_KEYS` exists.

        Lab 2 records `computed_at` in `verified_facts`. Feeding a clock
        reading into the plan's identity would make every identical run hash
        differently, and drift would be indistinguishable from time passing.
        """
        stamped = inputs.model_copy(
            update={
                "verified_facts": {
                    **inputs.verified_facts,
                    "computed_at": "2099-12-31T23:59:59+00:00",
                }
            }
        )
        assert "computed_at" in inputs.verified_facts
        assert expand(stamped).lock().plan_sha256 == expand(inputs).lock().plan_sha256

    def test_volatile_keys_are_named_and_effective(self):
        assert "computed_at" in VOLATILE_FACT_KEYS
        assert stable_facts({"b": 1, "computed_at": "now", "a": 2}) == {"a": 2, "b": 1}

    def test_the_lock_time_is_recorded_but_excluded_from_the_hash(self, inputs):
        """A lock time is a fact about the locking, not the plan's content."""
        first = expand(inputs).lock()
        second = expand(inputs).lock()
        assert first.locked_at
        assert first.plan_sha256 == second.plan_sha256

    def test_the_lock_is_reported_as_reproducible(self, locked):
        """`locked_at` differing while the hash matches is the whole point."""
        assert locked.obligation_count == len(locked.obligations)

    def test_obligations_are_ordered_naturally(self, inputs):
        """S10 after S2, unit 10 after unit 9 -- as a syllabus numbers them."""
        plan = expand(inputs).lock()
        assert _sort_key("S2") < _sort_key("S10")
        assert _sort_key("ASG-2") < _sort_key("ASG-10")
        assert _sort_key("2") < _sort_key("10")

    def test_reordering_obligations_does_not_change_the_plan(self, inputs):
        """Obligations are a set; their order is for display only.

        If their order fed the hash, two runs building the same plan from the
        same scope could hash differently and the drift check could never tell a
        reordered plan from a changed one.
        """
        draft = expand(inputs)
        forward = draft.lock()
        shuffled = PlanDraft(
            course_code=draft.course_code,
            course_name=draft.course_name,
            units=draft.units,
            assignment_ids=draft.assignment_ids,
            include_milestones=draft.include_milestones,
            obligations=list(reversed(draft.obligations)),
            verified_facts=dict(draft.verified_facts),
        ).lock()
        assert forward.plan_sha256 == shuffled.plan_sha256

    def test_reordering_scope_does_change_the_plan(self, inputs):
        """Scope order is teaching order, and teaching order is content."""
        forward = expand(inputs).lock()
        reversed_scope = inputs.model_copy(
            update={"units": tuple(reversed(inputs.units))}
        )
        assert expand(reversed_scope).lock().plan_sha256 != forward.plan_sha256

    def test_a_duplicate_obligation_id_is_refused(self, inputs):
        """Two obligations under one id make the drift set difference ambiguous."""
        draft = expand(inputs)
        duplicate = draft.obligations[0]
        with pytest.raises(SkillError) as exc:
            draft.add_obligation(duplicate)
        assert exc.value.code == "duplicate_obligation"

    def test_a_duplicate_unit_is_refused_at_expand_time(self, inputs):
        """One obligation standing for two units would weaken the check."""
        with pytest.raises(SkillError) as exc:
            expand(inputs.model_copy(update={"units": ("1", "1", "2")}))
        assert exc.value.code == "duplicate_unit"

    def test_a_dropped_obligation_is_reported(self, inputs):
        draft = expand(inputs)
        removed = draft.drop_obligation(obligation_id(KIND_COVER_UNIT, "1"))
        assert removed.subject == "1"
        with pytest.raises(KeyError):
            draft.drop_obligation("OBL-nope")

    def test_milestones_out_of_scope_creates_no_obligation(self, inputs):
        """An out-of-scope item must not become an unmeetable commitment."""
        without = expand(inputs.model_copy(update={"include_milestones": False})).lock()
        assert without.obligations_of(KIND_TRACK_MILESTONES) == ()

        with_milestones = expand(inputs).lock().obligations_of(KIND_TRACK_MILESTONES)
        assert len(with_milestones) == 1

    def test_the_plan_outline_renders_its_hash_and_obligations(self, locked):
        text = locked.outline()
        assert locked.plan_sha256 in text
        assert "OBL-cover_unit/1" in text
        assert HUMAN_AUTHORITY in text

    def test_an_empty_plan_renders_without_crashing(self, inputs):
        draft = PlanDraft(
            course_code="X", course_name="Y", units=("1",)
        ).lock()
        assert "no obligations" in draft.outline()

    def test_the_human_authority_comes_from_the_plan(self, inputs):
        assert expand(inputs).lock().human_authority == HUMAN_AUTHORITY

    def test_a_different_authority_is_carried_through(self, inputs):
        plan = expand(
            inputs.model_copy(update={"human_authority": "examiner_board"})
        ).lock()
        assert plan.human_authority == "examiner_board"
        assert plan.recompute_sha256() == plan.plan_sha256

    def test_obligations_are_addressable_by_kind_and_subject(self, locked):
        assert locked.subjects_of(KIND_COVER_UNIT) == ("1", "2")
        assert locked.subjects_of(KIND_DISTRIBUTE_ASSIGNMENT) == ("A-1",)
        assert locked.obligations_of("no_such_kind") == ()
        assert len(locked.obligation_ids()) == len(locked.obligations)


# ---------------------------------------------------------------------------
# 3. Drift -- 2.12's named mitigation for Lab 3
# ---------------------------------------------------------------------------


class TestDrift:
    def test_a_conforming_result_has_no_drift(self, locked, conforming):
        report = conforming.check_against(locked)
        assert report.conforms
        assert report.findings == ()
        assert report.blocking is False

    def test_deferred_obligations_are_counted_not_passed(self, locked, conforming):
        """A clean verdict must not be able to mean "nothing was checked"."""
        report = conforming.check_against(locked)
        assert report.obligations_deferred == len(
            locked.obligations_of(KIND_VERIFY_STEP)
        )
        assert report.obligations_checked + report.obligations_deferred == len(
            locked.obligations
        )
        assert report.coverage_is_complete is False

    def test_deferred_kinds_are_named_and_cover_verify_step_only(self):
        assert set(DEFERRED_KINDS) == {KIND_VERIFY_STEP}

    def test_a_dropped_unit_is_reported_as_missing(self, locked, conforming):
        drifted = conforming.model_copy(update={"units_covered": ("1",)})
        report = drifted.check_against(locked)
        assert not report.conforms
        assert any(
            f.obligation_id == obligation_id(KIND_COVER_UNIT, "2")
            for f in report.findings
        )

    def test_an_out_of_scope_unit_is_reported_as_unexpected(self, locked, conforming):
        drifted = conforming.model_copy(update={"units_covered": ("1", "2", "99")})
        report = drifted.check_against(locked)
        assert any(
            f.kind == "unexpected" and f.subject == "99" for f in report.findings
        )

    def test_an_unverified_number_is_reported(self, locked, conforming):
        """The grounding rule, as a mechanical check rather than a convention."""
        drifted = conforming.model_copy(
            update={
                "reported_facts": {**conforming.reported_facts, "invented_total": 7.0}
            }
        )
        report = drifted.check_against(locked)
        assert any(
            f.kind == "value_mismatch" and f.subject == "invented_total"
            for f in report.findings
        )

    def test_a_wrong_number_is_reported_against_the_tool_value(self, locked, conforming):
        drifted = conforming.model_copy(
            update={
                "reported_facts": {
                    **conforming.reported_facts,
                    "total_available_marks": 999.0,
                }
            }
        )
        report = drifted.check_against(locked)
        assert any(
            f.kind == "value_mismatch"
            and f.subject == "total_available_marks"
            and "40.0" in f.detail
            for f in report.findings
        )

    def test_a_dotted_fact_path_resolves_into_nested_verified_facts(
        self, locked, conforming
    ):
        """Lab 2 names per-assignment marks with a dotted key over a nested dict.

        Without path resolution the grounding check would reject a correct
        number it simply could not follow a level down.
        """
        assert resolve_fact(locked.verified_facts, "marks_by_assignment.A-1") == 40.0
        assert conforming.check_against(locked).conforms

    def test_resolve_fact_distinguishes_missing_from_a_none_value(self):
        """`None` is a legitimate fact value, so absence needs its own marker."""
        assert resolve_fact({"a": None}, "a") is None
        assert resolve_fact({"a": None}, "b") is MISSING
        assert resolve_fact({}, "a.b.c") is MISSING
        assert resolve_fact({"a": {"b": 1}}, "a.b") == 1

    def test_missing_milestones_are_reported_when_in_scope(self, locked, conforming):
        drifted = conforming.model_copy(update={"milestones_emitted": False})
        report = drifted.check_against(locked)
        assert any(f.kind == "missing" for f in report.findings)

    def test_an_unknown_obligation_kind_cannot_be_satisfied(self, locked, conforming):
        """An obligation nobody can evaluate is an obligation nobody enforces.

        Failing it is the safe direction: an open vocabulary cannot be checked
        for completeness, and silently passing an unknown kind would let a new
        kind be added and never enforced.
        """
        from course_organizer.skills.plan_skill import _content_payload, _sha256

        odd = Obligation(
            obligation_id="OBL-future_kind/x",
            kind="future_kind",
            subject="x",
            statement="a kind nothing knows how to check",
        )
        forged = LockedPlan.model_construct(
            **{
                **locked.model_dump(),
                "obligations": (*locked.obligations, odd),
                "obligation_count": locked.obligation_count + 1,
                "plan_sha256": _sha256(
                    _content_payload(
                        course_code=locked.course_code,
                        units=locked.units,
                        assignment_ids=locked.assignment_ids,
                        include_milestones=locked.include_milestones,
                        obligations=(*locked.obligations, odd),
                        verified_facts=locked.verified_facts,
                        notes=locked.notes,
                        human_authority=locked.human_authority,
                    )
                ),
            }
        )
        report = conforming.check_against(forged)
        assert any("unknown kind" in f.detail for f in report.findings)

    def test_the_drift_summary_names_the_plan_it_checked(self, locked, conforming):
        report = conforming.check_against(locked)
        assert locked.plan_sha256[:12] in report.summary()
        assert "deferred" in report.summary()

    def test_the_drift_table_renders_when_there_are_findings(self, locked, conforming):
        drifted = conforming.model_copy(update={"units_covered": ()})
        assert "missing" in drifted.check_against(locked).table()

    def test_the_drift_table_says_so_when_there_are_none(self, locked, conforming):
        assert "no drift" in conforming.check_against(locked).table()

    def test_a_drift_check_rejects_a_tampered_plan(self, conforming, locked):
        tampered = LockedPlan.model_construct(
            **{**locked.model_dump(), "units": ("1", "2", "3")}
        )
        with pytest.raises(SkillError):
            conforming.check_against(tampered)


# ---------------------------------------------------------------------------
# 4. The format skill
# ---------------------------------------------------------------------------


class TestFormatSkill:
    def test_a_template_must_declare_header_and_approval(self):
        """2.12's hard stop is a property of the template, not a convention.

        Refusing the template is far cheaper than noticing the hole in a
        finished artefact nobody is obliged to sign.
        """
        with pytest.raises(ValidationError) as exc:
            TemplateSpec(
                template_id="bad",
                title="T",
                audience="a",
                sections=("header", "drift"),
                body_style="markdown",
            )
        assert "approval" in str(exc.value)

    def test_an_unknown_section_is_refused_at_construction(self):
        with pytest.raises(ValidationError) as exc:
            TemplateSpec(
                template_id="bad",
                title="T",
                audience="a",
                sections=("header", "no_such_section", "approval"),
            )
        assert "no_such_section" in str(exc.value)

    def test_an_unknown_body_style_is_refused_at_construction(self):
        with pytest.raises(ValidationError):
            TemplateSpec(
                template_id="bad",
                title="T",
                audience="a",
                sections=("header", "approval"),
                body_style="pdf",
            )

    def test_every_declared_section_has_a_builder_and_a_heading(self):
        """A closed vocabulary, so no renderer needs a fallback branch."""
        from course_organizer.skills.format_skill import _BUILDERS, DEFAULT_HEADINGS

        assert set(SECTIONS) == set(_BUILDERS)
        assert set(SECTIONS) == set(DEFAULT_HEADINGS)

    def test_every_body_style_has_a_renderer(self):
        from course_organizer.skills.format_skill import _RENDERERS

        assert set(BODY_STYLES) == set(_RENDERERS)

    @pytest.mark.parametrize("style", BODY_STYLES)
    def test_all_three_styles_render_the_same_case(self, locked, conforming, style):
        """Style is presentation, not behaviour: same data, three renderings."""
        template = TemplateSpec(
            template_id=f"t-{style}",
            title="Test",
            audience="tester",
            sections=("header", "units", "drift", "approval"),
            body_style=style,
        )
        document = _render(locked, conforming, template)
        if style == "json":
            parsed = json.loads(document.body)
            assert parsed["sections"]["header"]["course_code"] == locked.course_code
        else:
            assert locked.course_code in document.body

    def test_the_same_template_twice_gives_a_byte_identical_document(
        self, locked, conforming, template
    ):
        """A template leaking behaviour from elsewhere breaks 'reusable'."""
        first = _render(locked, conforming, template)
        second = _render(locked, conforming, template)
        assert first.body == second.body
        assert first.document_id == second.document_id

    def test_a_different_template_gives_a_different_document(
        self, locked, conforming, template
    ):
        """Otherwise the template would not actually be doing anything."""
        other = template.model_copy(
            update={"template_id": "t2", "sections": ("header", "approval")}
        )
        assert _render(locked, conforming, other).body != _render(
            locked, conforming, template
        ).body

    def test_section_order_follows_the_template_not_the_vocabulary(self):
        """Order is the template's choice; two orders are two documents."""
        from course_organizer.skills.format_skill import _BUILDERS

        template = TemplateSpec(
            template_id="t",
            title="T",
            audience="a",
            sections=("approval", "drift", "header"),
        )
        resolved = template.sections_resolved()
        assert [s.name for s in resolved] == ["approval", "drift", "header"]
        assert {s.name for s in resolved} <= set(_BUILDERS)

    def test_an_empty_optional_section_is_omitted(self, locked, template):
        """A bare heading is worse than nothing.

        'units' returns no lines for an empty result, so rendering it would
        produce a heading with an empty table -- which is also exactly what a
        drifted result looks like.
        """
        empty = WorkspaceResult(course_code=locked.course_code)
        document = _render(locked, empty, template.model_copy(update={
            "sections": ("header", "units", "drift", "approval")
        }))
        assert "units" in document.sections_omitted
        assert "units" not in document.sections_rendered

    def test_an_empty_optional_section_can_be_kept_as_a_placeholder(
        self, locked, template
    ):
        document = _render(
            locked,
            WorkspaceResult(course_code=locked.course_code),
            template.model_copy(
                update={
                    "sections": ("header", "units", "drift", "approval"),
                    "include_empty_sections": True,
                }
            ),
        )
        assert "units" in document.sections_rendered

    def test_a_clean_drift_section_can_be_hidden(self, locked, conforming):
        """Hiding the section cannot launder a drifted document."""
        template = TemplateSpec(
            template_id="t",
            title="T",
            audience="a",
            sections=("header", "drift", "approval"),
            include_drift_table=False,
        )
        clean = _render(locked, conforming, template)
        assert "drift" in clean.sections_omitted
        assert clean.drift.conforms
        assert clean.is_ready_for_review

    def test_findings_are_never_hidden_by_include_drift_table(
        self, locked, conforming
    ):
        drifted = conforming.model_copy(update={"units_covered": ()})
        template = TemplateSpec(
            template_id="t",
            title="T",
            audience="a",
            sections=("header", "drift", "approval"),
            include_drift_table=False,
        )
        document = _render(locked, drifted, template)
        assert "drift" in document.sections_rendered
        assert not document.is_ready_for_review

    def test_the_document_carries_both_text_and_structure(self, locked, conforming):
        """Re-parsing rendered markdown to assert on it would make every test
        hostage to the renderer's escaping."""
        document = _render(locked, conforming, TEMPLATE_SPECS["faculty-pack"])
        assert isinstance(document.data, dict)
        assert document.data["header"]["course_code"] == locked.course_code
        assert document.line_count() > 0

    def test_the_document_id_is_content_derived_and_reproducible(
        self, locked, conforming
    ):
        template = TEMPLATE_SPECS["faculty-pack"]
        first = _render(locked, conforming, template)
        second = _render(locked, conforming, template)
        assert first.document_id == second.document_id
        assert first.document_id.startswith("DOC-")

    def test_a_different_plan_gives_a_different_document_id(
        self, locked, conforming, inputs
    ):
        template = TEMPLATE_SPECS["faculty-pack"]
        other = expand(inputs.model_copy(update={"units": ("1",)})).lock()
        assert (
            _render(other, conforming, template).document_id
            != _render(locked, conforming, template).document_id
        )

    def test_the_document_cannot_be_edited_after_rendering(
        self, locked, conforming, template
    ):
        document = _render(locked, conforming, template)
        with pytest.raises(ValidationError):
            document.status = DocumentStatus.DRAFT  # type: ignore[misc]

    def test_every_template_in_the_demo_is_valid_and_renders(self, locked, conforming):
        for template_id, template in TEMPLATE_SPECS.items():
            document = _render(locked, conforming, template)
            assert document.template_id == template_id
            assert document.body.strip()

    def test_the_markdown_style_keeps_the_payload_in_the_text(
        self, locked, conforming
    ):
        """So a rendered .md file is still machine-checkable."""
        document = _render(locked, conforming, TEMPLATE_SPECS["faculty-pack"])
        assert "<!-- data:" in document.body

    def test_the_plain_style_has_no_markup(self, locked, conforming):
        document = _render(locked, conforming, TEMPLATE_SPECS["coordinator-brief"])
        assert "##" not in document.body
        assert "<!--" not in document.body

    def test_the_json_style_does_not_duplicate_the_data(self, locked, conforming):
        """Two representations of one fact could disagree."""
        document = _render(locked, conforming, TEMPLATE_SPECS["pipeline-json"])
        parsed = json.loads(document.body)
        assert parsed["body_style"] == "json"
        assert all(isinstance(section, dict) for section in parsed["sections"].values())

    def test_a_handler_returning_the_wrong_type_is_a_contract_error(
        self, registry, inputs
    ):
        """A skill bug must not look like ordinary runtime trouble."""
        spec = registry.get("plan")
        rogue = _with_handler(
            build_default_skill_registry(run_id="rogue"),
            spec,
            lambda payload, ctx: "not a LockedPlan",  # type: ignore[return-value]
        )
        with pytest.raises(SkillError) as exc:
            rogue.invoke("plan", inputs)
        assert exc.value.code == "contract_violation"

    def test_a_handler_that_raises_is_recorded_not_crashed(self, inputs):
        """A capability that failed at runtime is an auditable fact.

        Raises `RuntimeError` from the handler, so a caller downstream never
        sees a traceback for a condition the ledger already records. The point
        is that `ok` is False *and* a call is still appended -- an audit trail
        that skips failures is an audit trail of successes.
        """
        spec = build_default_skill_registry().get("plan")

        def boom(payload, ctx):
            raise RuntimeError("planned failure")

        rogue = _with_handler(build_default_skill_registry(run_id="rogue"), spec, boom)
        result = rogue.invoke("plan", inputs)

        assert result.ok is False
        assert "planned failure" in (result.call.error or "")
        assert len(rogue.ledger) == 1
        assert rogue.ledger.entries[0].ok is False

        with pytest.raises(SkillError) as exc:
            result.unwrap()
        assert exc.value.code == "handler_failure"

    def test_a_failed_call_fabricates_no_value(self, inputs):
        """`LockedPlan()` is not a degraded plan; it is not a plan.

        An earlier version asserted every output model was default-constructible
        and called the constructor on failure. It was not -- `LockedPlan` and
        `Document` both have required fields -- and that assertion was the only
        thing standing between a handler exception and a `ValidationError`
        about a missing `course_code`, raised far from the cause. `None` is the
        honest answer, and `unwrap()` is the only supported way to read a
        failed result.
        """
        spec = build_default_skill_registry().get("plan")
        assert spec.failure_value() is None

        def boom(payload, ctx):
            raise RuntimeError("planned failure")

        rogue = _with_handler(build_default_skill_registry(run_id="none"), spec, boom)
        assert rogue.invoke("plan", inputs).value is None

    def test_a_failed_format_call_fabricates_no_document(self, locked, conforming):
        """Same rule, the other skill: no id means no document.

        A `Document` with an empty id would not be a degraded document, it would
        be an unrenderable one, and putting it in the audit trail would suggest
        a rendering happened.
        """
        spec = build_default_skill_registry().get("format_organised")
        assert spec.failure_value() is None

        def boom(payload, ctx):
            raise RuntimeError("planned failure")

        rogue = _with_handler(build_default_skill_registry(run_id="doc"), spec, boom)
        result = rogue.invoke(
            "format_organised",
            {
                "result": conforming,
                "plan": locked,
                "template": TEMPLATE_SPECS["faculty-pack"],
            },
        )
        assert result.ok is False
        assert result.value is None
        with pytest.raises(SkillError):
            result.unwrap()

    def test_a_default_is_kept_when_it_means_something(self):
        """The rule is "is the empty value meaningful", not "always None".

        A model whose every field defaults has a real degraded state, and
        returning a typed instance is more useful to a caller than None. Proved
        with a synthetic spec because neither skill in this lab qualifies --
        which is itself the finding: both of them are artefacts that either
        exist completely or not at all.
        """

        class Degradable(BaseModel):
            """A model that can honestly be empty."""

            note: str = ""
            count: int = 0

        spec = SkillSpec(
            name="degradable",
            description="a skill with a meaningful empty state",
            input_model=PlanInputs,
            output_model=Degradable,
            handler=lambda payload, ctx: Degradable(),
        )
        value = spec.failure_value()
        assert isinstance(value, Degradable)
        assert value.note == ""


def _with_handler(registry, spec, handler):
    """Install a replacement handler on a fresh registry.

    Args:
        registry: A registry built by `build_default_skill_registry`.
        spec: The original `SkillSpec`, for its declared models.
        handler: The replacement callable.

    Returns:
        The same registry, with `spec.name` pointing at a broken skill.

    Bypasses `register` on purpose: registering a second skill under a name
    that already exists is correctly refused, and these tests are specifically
    about what happens when a skill is broken anyway.
    """
    registry._skills[spec.name] = SkillSpec(
        name=spec.name,
        description=spec.description,
        input_model=spec.input_model,
        output_model=spec.output_model,
        handler=handler,
    )
    return registry


def _render(
    plan: LockedPlan, result: WorkspaceResult, template: TemplateSpec
) -> Document:
    """Render one document through the registry, the way the agent does.

    Args:
        plan: The locked blueprint.
        result: The produced workspace.
        template: The template spec.

    Returns:
        The rendered document, unwrapped so a failure raises.
    """
    registry = build_default_skill_registry(run_id="render-test")
    return registry.invoke(
        "format_organised", {"result": result, "plan": plan, "template": template}
    ).unwrap()


# ---------------------------------------------------------------------------
# 5. The reuse proof
# ---------------------------------------------------------------------------


class TestReuse:
    def test_the_four_cases_differ_on_two_independent_axes(self):
        """Two courses x two scope shapes.

        A skill that only handled "the whole course" would pass a two-case demo
        using one course twice, and fails here on `cs-narrow` alone.
        """
        assert len(CASES) == 4
        assert {case.course_code for case in CASES} == {"CS201", "DBMS402"}
        assert {case.include_milestones for case in CASES} == {True, False}
        assert len({len(case.units) for case in CASES}) > 1
        assert len({len(case.assignment_ids) for case in CASES}) > 1

    def test_every_case_labels_why_it_exists(self):
        assert all(case.narrative for case in CASES)

    def test_the_same_two_skills_serve_every_case(self, registry):
        """The document's acceptance criterion, asserted at the registry."""
        agent = SkillAgent(registry)
        proof = agent.prove_reuse()
        assert set(proof.skill_names_used) <= set(SKILL_NAMES)
        assert len(proof.runs) == len(CASES)

    def test_each_case_produces_a_genuinely_different_locked_plan(self, registry):
        """Four inputs, four plans -- so the skill is really parameterised.

        A skill returning one fixed answer for every input would also be
        'called once per case'.
        """
        proof = SkillAgent(registry).prove_reuse()
        assert proof.distinct_plan_hashes == len(proof.runs)
        assert proof.runs[0].plan_sha256 != proof.runs[1].plan_sha256

    def test_every_case_conforms_to_its_own_plan(self, registry):
        """Conformance is per case, not pooled.

        A global 'no drift' claim would hide a case that passed only because
        another's plan was lenient.
        """
        assert SkillAgent(registry).prove_reuse().all_conform

    def test_every_case_renders_through_every_template(self, registry):
        proof = SkillAgent(registry).prove_reuse()
        for case in CASES:
            assert set(proof.documents[case.label]) == set(TEMPLATE_SPECS)
        assert proof.document_count == len(CASES) * len(TEMPLATE_SPECS)

    def test_the_same_registry_serves_every_case_without_re_registration(
        self, registry
    ):
        """If a case needed its own skill, registering twice would fail."""
        before = len(registry.ledger)
        SkillAgent(registry).prove_reuse()
        assert len(registry.ledger) > before

    def test_a_narrow_case_drops_the_obligations_it_does_not_need(self, registry):
        proof = SkillAgent(registry).prove_reuse()
        by_label = {run.case.label: run for run in proof.runs}
        broad = by_label["cs-broad"].locked_plan
        narrow = by_label["cs-narrow"].locked_plan
        assert len(narrow.obligations) < len(broad.obligations)
        assert narrow.obligations_of(KIND_TRACK_MILESTONES) == ()
        assert broad.obligations_of(KIND_TRACK_MILESTONES)

    def test_both_courses_report_their_own_tool_computed_facts(self, registry):
        """Numbers come from the store, not from the case definition."""
        proof = SkillAgent(registry).prove_reuse()
        by_label = {run.case.label: run for run in proof.runs}
        assert by_label["cs-broad"].result.reported_facts["total_available_marks"] == 65.0
        assert by_label["db-broad"].result.reported_facts["total_available_marks"] == 55.0

    def test_every_number_in_a_document_comes_from_a_verified_fact(self, registry):
        """Lab 2's rule, surviving two more layers."""
        for run in SkillAgent(registry).prove_reuse().runs:
            for key, value in run.result.reported_facts.items():
                assert key in run.result.ground_fact_keys
                assert resolve_fact(run.locked_plan.verified_facts, key) == value

    def test_an_unknown_template_id_is_refused(self, registry):
        with pytest.raises(SkillErrorLookup) as exc:
            SkillAgent(registry).run(CASES[0], template_id="no-such-template")
        assert "no-such-template" in str(exc.value)

    def test_injected_drift_is_detected_and_blocks_readiness(self, registry):
        """Lab 3 is named in 2.12's plan-drift mitigation, so this is the job."""
        agent = SkillAgent(registry)
        good = agent.run(CASES[0])
        assert good.document.is_ready_for_review

        drifted = agent.inject_drift(good)
        assert drifted.drift is not None
        assert not drifted.drift.conforms
        assert drifted.status is DocumentStatus.DRAFT
        assert not drifted.is_ready_for_review

    def test_injected_drift_covers_every_deviation_kind(self, registry):
        agent = SkillAgent(registry)
        report = agent.inject_drift(agent.run(CASES[0])).drift
        assert report is not None
        kinds = {finding.kind for finding in report.findings}
        assert kinds == {"missing", "unexpected", "value_mismatch"}

    def test_a_course_wide_validation_is_required_for_planning(self, registry):
        """Lab 2's scope lesson, applied before the plan is written.

        A subset's totals would make a whole-course plan look wrong when it is
        right, so planning against them is worse than not planning.
        """
        agent = SkillAgent(registry)
        tool_run = agent.run(CASES[0]).tool_run
        assert _course_wide_validation(tool_run, CASES[0].course_code).course_wide

    def test_a_case_override_is_recorded_as_an_assumption(self, registry):
        """Lab 1 never asks about milestones, so the case sets them.

        Recorded in the locked plan's `notes` and rendered into the document --
        the mechanism Lab 1 built for exactly this ("the agent filled this in
        without asking"). A revision that silently narrows scope is precisely
        what 2.12's drift row exists to catch, and it cannot be caught if the
        plan does not say which parameter moved.
        """
        run = SkillAgent(registry).run(CASES[1])  # milestones OFF
        assert run.case.include_milestones is False
        assert run.locked_plan.include_milestones is False

        notes = " ".join(run.locked_plan.notes)
        assert "include_milestones=False" in notes

        # And it must be visible to a human reading the document, or hashing
        # it buys nothing.
        assert "Assumed" in run.document.body
        assert "include_milestones=False" in run.document.body
        assert run.document.data["header"]["assumptions"] == list(
            run.locked_plan.notes
        )

    def test_a_broad_case_records_only_what_lab_1_assumed(self, registry):
        """The broad case overrides nothing, so only Lab 1's own note appears.

        Lab 1 assumes `include_milestones: true` because the doc lists milestone
        tracking as a standard work stream. That note is true for this case and
        is kept; what must not happen is the *override* note appearing when
        there was no override.
        """
        run = SkillAgent(registry).run(CASES[0])  # milestones ON, as asked
        assert run.case.include_milestones is True
        joined = " ".join(run.locked_plan.notes)
        assert "include_milestones=False" not in joined
        assert "include_milestones" in joined  # Lab 1's own assumption survives

    def test_the_notes_are_covered_by_the_lock_hash(self, registry):
        """Stripping an assumption after locking must be detectable."""
        run = SkillAgent(registry).run(CASES[1])
        stripped = LockedPlan.model_construct(
            **{**run.locked_plan.model_dump(), "notes": ()}
        )
        with pytest.raises(SkillError) as exc:
            stripped.recompute_sha256()
        assert exc.value.code == "plan_tampered"

    def test_case_answers_address_lab_1_fields_by_name(self):
        """If Lab 1 renames a field, this fails rather than silently defaulting."""
        assert set(CASES[0].clarifications()) == {
            "course_code",
            "units",
            "assignments",
            "as_of",
        }

    def test_a_single_unit_case_answers_in_the_singular(self):
        narrow = next(case for case in CASES if len(case.units) == 1)
        assert narrow.units_answer.startswith("unit ")

    def test_the_ledger_records_every_skill_call_in_order(self, registry):
        agent = SkillAgent(registry)
        proof = agent.prove_reuse()
        names = registry.ledger.names()
        assert names[0] == "plan"
        assert "format_organised" in names
        assert len(names) == len(registry.ledger)
        assert len(proof.runs) * 2 <= len(names)

    def test_the_ledger_is_append_only(self, registry, inputs):
        registry.invoke("plan", inputs)
        snapshot = registry.ledger.entries
        registry.invoke("plan", inputs)
        assert registry.ledger.entries[: len(snapshot)] == snapshot

    def test_the_ledger_renders_as_an_audit_table(self, inputs):
        """Uses its own registry: the shared fixture accumulates across tests."""
        registry = build_default_skill_registry(run_id="ledger-test")
        registry.invoke("plan", inputs)
        text = registry.ledger.describe()
        assert "SKILL CALL LEDGER" in text
        assert "plan" in text
        assert "1 call(s)" in text

    def test_an_empty_ledger_says_so(self):
        assert "no skill calls" in build_default_skill_registry().ledger.describe()

    def test_a_reuse_proof_can_be_built_empty(self):
        """It must render without a single run, rather than raising on max()."""
        assert "0 of 0" in ReuseProof().summary()

    def test_a_run_result_exposes_its_plan_hash(self, registry):
        run = SkillAgent(registry).run(CASES[0])
        assert isinstance(run, SkillRunResult)
        assert run.plan_sha256 == run.locked_plan.plan_sha256
        assert run.document_id.startswith("DOC-")

    def test_a_case_is_frozen(self):
        """The demo's inputs must not be mutated by a run."""
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            CASES[0].course_code = "HACKED"  # type: ignore[misc]

    def test_a_case_can_be_built_from_scratch(self):
        """The cases are data, so a fifth case needs no code change."""
        extra = CaseSpec(
            label="custom",
            course_code="ZZ999",
            units=("7",),
            assignment_ids=(),
            include_milestones=False,
        )
        assert extra.clarifications()["course_code"] == "ZZ999"
        assert extra.assignments_answer == ""

    def test_the_injected_drift_uses_ids_from_no_store(self, registry):
        """So an 'unexpected' finding cannot be a mistyped real id."""
        agent = SkillAgent(registry)
        drifted = agent.inject_drift(agent.run(CASES[0]))
        assert "unit-99" in drifted.body
        assert "ASG-99" in drifted.body


# ---------------------------------------------------------------------------
# 6. Honesty -- the properties that keep this lab from lying
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_no_lab3_step_is_claimed_to_have_run(self, registry):
        """Lab 3 locks a blueprint; it does not execute the pipeline.

        Claiming otherwise would put a row of green ticks in the document over
        work nobody did.
        """
        for run in SkillAgent(registry).prove_reuse().runs:
            assert run.result.completed_steps == ()

    def test_unrun_criteria_render_as_pending_not_passed_or_failed(self, registry):
        """Lab 7 runs the steps. Neither pass nor fail is honest yet."""
        run = SkillAgent(registry).run(CASES[0])
        assert "[ ] S1:" in run.document.body
        assert "0 ran, " in run.document.body
        checks = run.document.data["checks"]
        assert checks["ran"] == 0
        assert checks["pending"] == checks["total"] > 0
        assert {row["state"] for row in checks["checks"]} == {"pending"}

    def test_the_checks_section_names_who_settles_them(self, registry):
        run = SkillAgent(registry).run(CASES[0])
        assert "Lab 7" in run.document.data["checks"]["settled_by"]

    def test_nothing_is_ever_marked_approved_or_published(self, registry):
        for run in SkillAgent(registry).prove_reuse().runs:
            assert run.document.awaiting_approval is True
            approval = run.document.data["approval"]
            assert approval["approved"] is False
            assert approval["published"] is False
            assert approval["required"] is True

    def test_there_is_no_final_status_to_reach(self):
        """Lab 3 has no human gate, so the type must not offer a stronger state.

        Passing validation earns a document the right to be *seen* by the
        faculty -- never the right to have been accepted by them.
        """
        assert {s.value for s in DocumentStatus} == {"draft", "ready_for_review"}
        assert not hasattr(DocumentStatus, "FINAL")
        assert not hasattr(DocumentStatus, "PUBLISHED")

    def test_a_clean_document_is_ready_for_review_not_published(self, registry):
        run = SkillAgent(registry).run(CASES[0])
        assert run.document.is_ready_for_review
        assert run.document.status is DocumentStatus.READY_FOR_REVIEW
        assert "Published: no" in run.document.body

    def test_the_approval_section_reports_the_live_status(self, registry):
        """A fixed 'DRAFT' string would be a lie once a clean document existed."""
        clean = SkillAgent(registry).run(CASES[0]).document
        assert "READY FOR REVIEW" in clean.body
        drifted = SkillAgent(registry).inject_drift(
            SkillAgent(build_default_skill_registry()).run(CASES[0])
        )
        assert "DRAFT" in drifted.body

    def test_the_drift_verdict_is_computed_once_and_shared(self, locked, conforming):
        """Two computations would let the body and the status disagree.

        A document reading 'no drift' while carrying status=draft is worse than
        either state alone.
        """
        document = _render(locked, conforming, TEMPLATE_SPECS["faculty-pack"])
        rendered = document.data["drift"]
        assert rendered["conforms"] == document.drift.conforms
        assert rendered["count"] == len(document.drift.findings)
        assert document.is_ready_for_review == document.drift.conforms


#: Modules a skill may not import. A skill transforms; data access belongs to
#: the tool layer, which has a connector boundary and a ledger entry behind it.
#: Matching is on the module name's first dotted segment, so both
#: `import os` and `from os.path import join` are caught.
FORBIDDEN_IMPORTS = ("open", "io", "pathlib", "os")


# ---------------------------------------------------------------------------
# 7. The pitfall, enforced structurally
# ---------------------------------------------------------------------------


#: Course codes and names from the seed corpus, plus the two obvious near
#: misses a hard-coding shortcut would reach for. Matching is done on the
#: *source text*, so it also catches a course buried in a docstring or a
#: comment -- which is where a "temporary" constant tends to survive.
FORBIDDEN_LITERALS = (
    "CS201",
    "DBMS402",
    "Data Structures",
    "Database Management",
    "ASG-01",
    "ASG-02",
    "ASG-03",
)


class TestNoCourseIsHardCoded:
    def test_the_skills_package_contains_no_course_identifier(self):
        """The document's pitfall, made impossible to reintroduce.

        "Keep skills case-agnostic and typed; hard-coding one case breaks
        reuse." Reviewer diligence does not survive a deadline; this does.
        """
        offenders: list[str] = []
        for path in sorted(SKILLS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            ast.parse(source)  # must be parseable for the scan to mean anything
            for literal in FORBIDDEN_LITERALS:
                if literal in source:
                    offenders.append(f"{path.name}: {literal}")
        assert offenders == [], (
            "the skills package must not name any course, template case or "
            f"assignment id. Found: {offenders}"
        )

    def test_the_agent_module_keeps_cases_out_of_the_skill_layer(self):
        """Cases live in the lab module, never in the skill package.

        A demo has to name its cases somewhere, and the only safe place for
        those names is the demo.
        """
        agent_source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "course_organizer"
            / "labs"
            / "lab03_skill_creation.py"
        ).read_text(encoding="utf-8")
        assert "CS201" in agent_source  # the cases are declared here, deliberately

        skills_sources = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(SKILLS_DIR.glob("*.py"))
        )
        for literal in FORBIDDEN_LITERALS:
            assert literal not in skills_sources

    def test_the_skill_package_does_not_import_the_lab_modules(self):
        """A skill that imported a case would not be reusable by construction."""
        for path in sorted(SKILLS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "labs.lab0" not in source, f"{path.name} imports a lab module"
            assert "TEMPLATE_SPECS" not in source, f"{path.name} references demo cases"

    def test_the_skill_package_never_touches_the_filesystem(self):
        """Skills transform. Data access belongs to the tool layer.

        A skill that opened a file would bypass the connector boundary Lab 2
        exists to enforce, and there would be no ledger entry for it.
        """
        for path in sorted(SKILLS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    for name in names:
                        assert not name.startswith(FORBIDDEN_IMPORTS), (
                            f"{path.name} imports {name!r}; skills must not do IO"
                        )

    def test_a_fresh_case_needs_no_change_to_the_skills(self):
        """The acceptance criterion as an executable demonstration.

        A course code absent from every forbidden literal above, a shape
        present in no existing case, driven through the unmodified skills.
        """
        template = TemplateSpec(
            template_id="reuse",
            title="Reuse",
            audience="reviewer",
            sections=("header", "units", "drift", "approval"),
        )
        plan = expand(
            PlanInputs(
                course_code="ZZ999",
                course_name="Never Before Seen",
                units=("42",),
                assignment_ids=(),
                include_milestones=False,
                steps=(),
                verified_facts={"course_code": "ZZ999"},
            )
        ).lock()
        result = WorkspaceResult(
            course_code="ZZ999",
            units_covered=("42",),
            milestones_emitted=False,
            ground_fact_keys=(),
        )
        document = _render(plan, result, template)
        assert document.is_ready_for_review
        assert "ZZ999" in document.body

    def test_case_agnosticism_is_declared_and_enforced_together(self, registry):
        """The flag on the spec and the AST scan are two halves of one claim.

        Neither alone is sufficient: the flag could be a lie, and the scan
        cannot see behaviour.
        """
        assert all(registry.get(name).case_agnostic for name in SKILL_NAMES)
        assert len(FORBIDDEN_LITERALS) >= 6


# ---------------------------------------------------------------------------
# 8. Documented limitations
# ---------------------------------------------------------------------------


class TestLimitations:
    def test_a_deferred_obligation_is_not_counted_as_checked(self, locked, conforming):
        """Lab 3's honest limitation, asserted so it cannot quietly change."""
        report = conforming.check_against(locked)
        assert report.obligations_deferred > 0
        assert report.coverage_is_complete is False

    def test_the_locked_plan_is_not_yet_obeyed_by_a_real_pipeline(self, registry):
        """Lab 7 runs the steps. Until then the blueprint is a specification.

        Recorded as a test so that the day Lab 7 lands, this one fails and
        points at the work.
        """
        run = SkillAgent(registry).run(CASES[0])
        assert run.locked_plan.obligation_count > run.document.drift.obligations_checked

    def test_the_document_is_a_shell_not_a_workspace(self, registry):
        """2.10 Lab 3's Output says 'shell'; Lab 4-8 fill it in."""
        run = SkillAgent(registry).run(CASES[0])
        assert "ready_for_review" in run.document.status.value
        assert run.document.awaiting_approval