"""Reusable, schema-typed skills. Document 2.10, Lab 3.

The document's deliverable for this lab is precise: "Two callable skills with
schemas, invoked by the agent. Accept: The same skill works for two different
cases without code changes."

That acceptance criterion is what this package is shaped around. Two modules,
one skill each, named after the document's own pseudocode:

    from course_organizer.skills import build_default_skill_registry

    registry = build_default_skill_registry()
    plan = registry.invoke("plan", inputs).unwrap()               # LockedPlan
    document = registry.invoke("format_organised", {...}).unwrap()  # Document

Neither module names a course. The document's pitfall for this lab is "Keep
skills case-agnostic and typed; hard-coding one case breaks reuse", and that is
a property that cannot be checked by reading code convincingly -- it is
checked here by a test that parses this package and fails if a course code or
course name appears in it at all. See
``tests/test_lab03_skill_creation.py::test_no_course_identifier_is_hard_coded``.
"""

from course_organizer.skills.base import (
    SkillCall,
    SkillContext,
    SkillContractError,
    SkillError,
    SkillLedger,
    SkillRegistry,
    SkillResult,
    SkillSpec,
)
from course_organizer.skills.format_skill import (
    BODY_STYLES,
    SECTIONS,
    Document,
    DocumentStatus,
    FormatInputs,
    Section,
    TemplateSpec,
    format_organised,
    register_format_skill,
)
from course_organizer.skills.plan_skill import (
    DEFERRED_KINDS,
    KIND_COVER_UNIT,
    KIND_DISTRIBUTE_ASSIGNMENT,
    KIND_GROUND_FACTS,
    KIND_TRACK_MILESTONES,
    KIND_VERIFY_STEP,
    OBLIGATION_KINDS,
    VOLATILE_FACT_KEYS,
    DriftFinding,
    DriftReport,
    LabStep,
    LockedPlan,
    MISSING,
    Obligation,
    PlanDraft,
    PlanInputs,
    WorkspaceResult,
    expand,
    obligation_id,
    plan_skill,
    register_plan_skill,
    resolve_fact,
    stable_facts,
)

__all__ = [
    # base
    "SkillCall",
    "SkillContext",
    "SkillContractError",
    "SkillError",
    "SkillLedger",
    "SkillRegistry",
    "SkillResult",
    "SkillSpec",
    # plan skill
    "DEFERRED_KINDS",
    "DriftFinding",
    "DriftReport",
    "KIND_COVER_UNIT",
    "KIND_DISTRIBUTE_ASSIGNMENT",
    "KIND_GROUND_FACTS",
    "KIND_TRACK_MILESTONES",
    "KIND_VERIFY_STEP",
    "LabStep",
    "LockedPlan",
    "MISSING",
    "OBLIGATION_KINDS",
    "VOLATILE_FACT_KEYS",
    "Obligation",
    "PlanDraft",
    "PlanInputs",
    "WorkspaceResult",
    "expand",
    "obligation_id",
    "plan_skill",
    "register_plan_skill",
    "resolve_fact",
    "stable_facts",
    # format skill
    "BODY_STYLES",
    "Document",
    "DocumentStatus",
    "FormatInputs",
    "SECTIONS",
    "Section",
    "TemplateSpec",
    "format_organised",
    "register_format_skill",
    # construction
    "build_default_skill_registry",
    "SKILL_NAMES",
]

#: The two skills document 2.10 Lab 3 asks for, in the order the pipeline
#: uses them. Named here so the "exactly two" claim is one assertion.
SKILL_NAMES: tuple[str, ...] = ("plan", "format_organised")


def build_default_skill_registry(run_id: str | None = None) -> SkillRegistry:
    """A registry with both Lab 3 skills registered. Nothing else.

    Lab 6 binds skills, tools and memory together behind a runtime, so it needs
    a single construction path rather than assembling a registry per caller.
    Mirrors Lab 2's `build_default_registry`.

    Args:
        run_id: Run id for the ledger. Generated if omitted.

    Returns:
        A registry holding exactly the skills in `SKILL_NAMES`.
    """
    registry = SkillRegistry(run_id=run_id)
    register_plan_skill(registry)
    register_format_skill(registry)
    return registry
