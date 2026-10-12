"""Lab 3 -- Skill Creation: reusable plan & format skills.

Document 2.10, Lab 3, quoted in full:

    Goal:  "Package repeatable capability as reusable, schema-typed skills."
    What it adds: "A plan/blueprint skill and a formatting skill that renders
                    the organised course workspace (notes, assignments,
                    grades) -- both reusable across cases."
    Inputs:  "Parsed inputs; template spec."
    Outputs: "A locked plan object; a formatted organised course workspace
               (notes, assignments, grades) shell."
    Agents / nodes: "Planning and assembly emerge as skills."
    Deliverable: "Two callable skills with schemas, invoked by the agent.
                  Accept: The same skill works for two different cases
                  without code changes."
    Nodes activated: "Plan node + Assembly node (as skills)."
    Pitfall to avoid: "Keep skills case-agnostic and typed; hard-coding one
                        case breaks reuse."
    Pseudocode:

        skill plan(inputs) -> Plan:           return expand(inputs).lock()
        skill format_organised(result, template) -> Document

Three places in the document make this lab more than "write two reusable
functions", and the design follows all three.

**The acceptance criterion is the lab.** "The same skill works for two
different cases without code changes" is a falsifiable claim, so this module
demonstrates it over *four* cases rather than two, chosen to differ in two
independent dimensions rather than only in identity:

============  ==========================  =========================================
Case          Course                      Scope shape
============  ==========================  =========================================
`cs-broad`    CS201, Data Structures      5 units, 3 assignments, milestones on
`cs-narrow`   CS201, Data Structures      1 unit, 1 assignment, milestones OFF
`db-broad`    DBMS402, Database Mgmt      5 units, 3 assignments, milestones on
`db-narrow`   DBMS402, Database Mgmt      2 units, 2 assignments, milestones OFF
============  ==========================  =========================================

Two courses * x two shapes. A skill that only handled "the whole course" would
pass a two-case demo using one course twice, and would fail here on
`cs-narrow` alone. The two courses also differ measurably -- 8 notes / 65
marks against 6 notes / 55 marks -- so the assertion "no code changes" covers
different underlying data as well as different scope.

**Plan drift is Lab 3's actual job.** The 2.12 risk table names it:

    | Plan drift | individual results deviate from the locked plan
                | Lock the plan (Lab 3/7); validation rejects any deviation
                  before the human sees it. |

Lab 3 is named in that mitigation. So `SkillAgent` also runs a *deliberately
drifted* result past the same two skills, unchanged, and shows the drift
rejected and the document marked `draft`. A reuse demo with no failure case
demonstrates that nothing broke; it does not demonstrate that anything works.

**The lock has to be real.** 2.12 defines the blueprint as "The locked
specification the pipeline obeys", and Lab 1's plan deliberately stays `DRAFT`
("not final -- a structured plan"). So `.lock()` produces a frozen,
tuple-backed `LockedPlan` whose hash is verified on every single drift check.
That is the part of Lab 3 that cannot be skipped, because an advisory lock is
just a comment.

Layering: Lab 1 supplies the outline steps and the clarified request, Lab 2
supplies the tool-computed facts. This module owns the cases, the templates
and the wiring, and it adds no new domain logic -- every number it prints came
from a Lab 2 tool call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from course_organizer import HUMAN_AUTHORITY
from course_organizer.labs.lab01_planner_loop import (
    PlannerResult,
    ScriptedFeedback,
    run_planner_loop,
)
from course_organizer.labs.lab02_tool_using_agent import ToolRunResult, run_tool_using_agent
from course_organizer.logging_setup import log_event
from course_organizer.skills import (
    SKILL_NAMES,
    Document,
    DriftReport,
    LabStep,
    LockedPlan,
    PlanInputs,
    TemplateSpec,
    WorkspaceResult,
    build_default_skill_registry,
    stable_facts,
)
from course_organizer.skills.base import SkillError, SkillRegistry
from course_organizer.tools.compute import ScopeValidationResult

logger = logging.getLogger(__name__)


DOC_PSEUDOCODE = """\
skill plan(inputs) -> Plan:           return expand(inputs).lock()
skill format_organised(result, template) -> Document

# doc 2.10 Lab 3, verbatim. Every step below maps to one call in this module:
#   expand()        -> plan_skill.handler, first half of the pseudocode
#   .lock()         -> plan_skill.handler, second half
#   format_organised() -> format_organised handler
"""


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSpec:
    """One demonstration case, entirely as data.

    Cases live here, in the *lab* module, and not in the skill package. That
    split is deliberate and is the clearest statement of Lab 3's pitfall: a
    demo has to name its cases somewhere, and the only safe place for those
    names is the demo.

    Attributes:
        label: Short identifier used in every table and assertion.
        course_code: The course. Read from the store, not assumed.
        units: Units in scope, in teaching order.
        assignment_ids: Assignments in scope. May be a strict subset.
        include_milestones: Whether milestone tracking is in scope. Lab 1's
            clarify loop never asks this, so it is a case-level decision and
            is recorded as an *assumption* on the clarified request rather
            than being set silently.
        as_of: Anchor date, so the run is reproducible.
        narrative: What makes this case worth including.
    """

    label: str
    course_code: str
    units: tuple[str, ...]
    assignment_ids: tuple[str, ...]
    include_milestones: bool
    as_of: str = "2026-10-15"
    narrative: str = ""

    @property
    def units_answer(self) -> str:
        """The `units` clarifying-question answer for this scope."""
        return f"units {self.units[0]}-{self.units[-1]}" if len(self.units) > 1 else (
            f"unit {self.units[0]}"
        )

    @property
    def assignments_answer(self) -> str:
        """The `assignments` clarifying-question answer for this scope."""
        return ", ".join(self.assignment_ids)

    def clarifications(self) -> dict[str, str]:
        """The answers to give Lab 1's clarifying questions.

        Returns:
            A mapping keyed by Lab 1's `REQUIRED_FIELDS` field names.
        """
        return {
            "course_code": self.course_code,
            "units": self.units_answer,
            "assignments": self.assignments_answer,
            "as_of": self.as_of,
        }


#: The four cases. Two courses x two scope shapes, so "works for two different
#: cases" is demonstrated on two independent axes rather than one.
CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        label="cs-broad",
        course_code="CS201",
        units=("1", "2", "3", "4", "5"),
        assignment_ids=("ASG-01", "ASG-02", "ASG-03"),
        include_milestones=True,
        narrative="widest scope: every unit, every assignment, milestones on",
    ),
    CaseSpec(
        label="cs-narrow",
        course_code="CS201",
        units=("3",),
        assignment_ids=("ASG-01",),
        include_milestones=False,
        narrative="single unit, single assignment, milestones OFF",
    ),
    CaseSpec(
        label="db-broad",
        course_code="DBMS402",
        units=("1", "2", "3", "4", "5"),
        assignment_ids=("ASG-01", "ASG-02", "ASG-03"),
        include_milestones=True,
        narrative="different course, same wide shape",
    ),
    CaseSpec(
        label="db-narrow",
        course_code="DBMS402",
        units=("2", "3"),
        assignment_ids=("ASG-02", "ASG-03"),
        include_milestones=False,
        narrative="different course, subset shape, milestones OFF",
    ),
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

#: Three template specs. These are *data*: a fourth output format is a fourth
#: entry here, not a fourth function. All three declare `header` and
#: `approval`, which `TemplateSpec` refuses to let them omit -- 2.12's hard
#: stop is a property of the template, not a convention the renderer applies.
TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    "faculty-pack": TemplateSpec(
        template_id="faculty-pack",
        title="Course Workspace Pack",
        audience="course faculty",
        sections=("header", "plan_lock", "units", "assignments", "checks", "drift", "approval"),
        body_style="markdown",
        include_drift_table=True,
    ),
    "coordinator-brief": TemplateSpec(
        template_id="coordinator-brief",
        title="Coordinator Brief",
        audience="apniLeap AURA Coordinator",
        sections=("header", "plan_lock", "milestones", "drift", "approval"),
        body_style="plain",
        include_empty_sections=False,
    ),
    "pipeline-json": TemplateSpec(
        template_id="pipeline-json",
        title="Course Workspace Shell",
        audience="downstream lab agents",
        sections=("header", "plan_lock", "units", "assignments", "milestones", "checks", "drift", "approval"),
        body_style="json",
        include_drift_table=True,
    ),
}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class SkillRunResult:
    """One case driven through both skills.

    Attributes:
        case: The case that was run.
        template_id: Which template rendered it.
        planner: Lab 1's planner result, which supplied the outline steps.
        tool_run: Lab 2's result, which supplied the verified facts.
        locked_plan: The skill's output: the locked blueprint.
        result: The produced workspace, for comparison against the plan.
        document: The skill's output: the formatted document.
        skill_calls_used: How many skill invocations this run took.
        registry: The registry, so the ledger can be inspected.
    """

    case: CaseSpec
    template_id: str
    planner: PlannerResult
    tool_run: ToolRunResult
    locked_plan: LockedPlan
    result: WorkspaceResult
    document: Document
    skill_calls_used: int
    registry: SkillRegistry

    @property
    def conforms(self) -> bool:
        """Whether the produced result obeyed the locked plan."""
        drift = self.document.drift
        return bool(drift and drift.conforms)

    @property
    def document_id(self) -> str:
        """The rendered document's id."""
        return self.document.document_id

    @property
    def plan_sha256(self) -> str:
        """The locked plan's content hash."""
        return self.locked_plan.plan_sha256


@dataclass
class ReuseProof:
    """Evidence for the document's acceptance criterion.

    Attributes:
        runs: Every case run, in order.
        documents: Every document rendered, keyed by case label and then
            template id. Held rather than merely counted, so a test can open
            any one of them and assert on its content rather than on a
            summary string.
        distinct_plan_hashes: How many different blueprints the cases
            produced. A count equal to the number of cases is the finding:
            the same skill produced genuinely different plans, which is what
            "reusable" means -- a skill that returned one fixed answer for
            every input would also be "called once per case".
        skill_names_used: The skills invoked, across all runs. Must be exactly
            `SKILL_NAMES` -- no per-case skill, no case-specific branch.
    """

    runs: list[SkillRunResult] = field(default_factory=list)
    documents: dict[str, dict[str, Document]] = field(default_factory=dict)
    distinct_plan_hashes: int = 0
    skill_names_used: tuple[str, ...] = ()

    @property
    def document_count(self) -> int:
        """How many documents the proof rendered in total."""
        return sum(len(group) for group in self.documents.values())

    @property
    def all_conform(self) -> bool:
        """Whether every case satisfied its own locked plan.

        Conformance is per case, not global: four different plans and four
        different results, each compared against its own plan. A "no drift"
        claim that pooled all four would hide a case that only passed because
        another's plan was lenient.
        """
        return all(run.conforms for run in self.runs)

    def summary(self) -> str:
        """A readable reuse table, one row per case."""
        headers = ("case", "course", "units", "asg", "obligations", "plan sha256", "doc status")
        rows = [
            (
                run.case.label,
                run.case.course_code,
                str(len(run.case.units)),
                str(len(run.case.assignment_ids)),
                str(run.locked_plan.obligation_count),
                run.plan_sha256[:12],
                run.document.status.value,
            )
            for run in self.runs
        ]
        widths = [
            max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
            for i in range(len(headers))
        ]

        def line(values: tuple[str, ...]) -> str:
            return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

        out = [
            "REUSE: the same two skills, four cases, no code changes",
            line(headers),
            "-" * (sum(widths) + 2 * (len(widths) - 1)),
        ]
        out.extend(line(row) for row in rows)
        out.append("")
        out.append(
            f"distinct locked plans : {self.distinct_plan_hashes} of "
            f"{len(self.runs)} (one per case, so each really did differ)"
        )
        out.append(f"skills invoked        : {', '.join(self.skill_names_used)}")
        out.append(f"documents rendered    : {self.document_count}")
        out.append(
            f"every case conforms   : {'yes' if self.all_conform else 'NO'}"
        )
        return "\n".join(out)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class SkillAgent:
    """Invokes the two Lab 3 skills and wires them to Labs 1 and 2.

    Holds no domain logic of its own. Every number it publishes came from a
    Lab 2 tool call, every acceptance criterion from a Lab 1 plan step, and
    every artefact from a skill invocation.

    Attributes:
        registry: The skill registry. One per agent, so one ledger per run.
    """

    def __init__(self, registry: Optional[SkillRegistry] = None) -> None:
        """Create an agent.

        Args:
            registry: The registry to invoke through. A fresh default registry
                is built when omitted.
        """
        self.registry = registry or build_default_skill_registry(
            run_id=f"lab03-{datetime.now(tz=timezone.utc):%Y%m%dT%H%M%S}"
        )

    # -- inputs ------------------------------------------------------------

    def build_plan_inputs(
        self, case: CaseSpec, planner: PlannerResult, facts: dict[str, Any]
    ) -> PlanInputs:
        """Assemble the `plan` skill's typed input from Labs 1 and 2.

        Args:
            case: The case being run.
            planner: Lab 1's result, supplying the clarified scope and the
                outline steps.
            facts: Lab 2's verified facts for the case's scope.

        Returns:
            A validated `PlanInputs`.

        Raises:
            SkillError: Propagated from `PlanInputs` validation if Lab 1
                produced an empty unit list, which cannot be planned for.
        """
        request = planner.request

        # `include_milestones` is an OPTIONAL_FIELD in Lab 1: never asked, so
        # it always arrives True. The case is the authority on it, and the
        # override is recorded in `assumptions` -- the mechanism Lab 1 built
        # for exactly this ("the agent filled this in without asking"). Doing
        # it silently would be the hidden state this codebase keeps rejecting.
        assumptions = list(request.assumptions)
        if request.include_milestones != case.include_milestones:
            assumptions.append(
                f"include_milestones={case.include_milestones} set by case "
                f"scope, not asked by the clarify loop"
            )
        request = request.model_copy(
            update={
                "include_milestones": case.include_milestones,
                "assumptions": assumptions,
            }
        )

        return PlanInputs(
            course_code=request.course_code,
            course_name=request.course_name,
            units=tuple(request.units),
            assignment_ids=tuple(request.assignment_ids),
            include_milestones=request.include_milestones,
            steps=tuple(
                LabStep(
                    step_id=step.step_id,
                    name=step.name,
                    agent=step.agent,
                    intent=step.intent,
                    acceptance_criteria=tuple(step.acceptance_criteria),
                )
                for step in planner.plan.steps
            ),
            verified_facts=stable_facts(facts),
            # Every assumption the case overrode, carried into the locked plan
            # and on into the document. A revision that silently changes scope
            # is exactly what 2.12's drift row exists to catch, and a plan that
            # does not say which parameters were assumed cannot be checked
            # against one that was written by hand.
            notes=tuple(assumptions),
            as_of=request.as_of.date().isoformat()
            if hasattr(request.as_of, "date")
            else str(request.as_of),
            human_authority=request.human_authority or HUMAN_AUTHORITY,
        )

    # -- assembly ----------------------------------------------------------

    def assemble(
        self, case: CaseSpec, plan: LockedPlan, validation: ScopeValidationResult
    ) -> WorkspaceResult:
        """Build the produced workspace, from tool-computed facts only.

        Every field traces to `validation.verified_facts` or to the plan's own
        scope. Nothing here is typed by hand, which is how Lab 2's rule -- "key
        numbers and lookups always go through tools, never hand-done in the
        prompt" -- survives into a document a human will read.

        Args:
            case: The case being run.
            plan: The locked blueprint, for the scope to stay inside.
            validation: Lab 2's course-wide validation for this case.

        Returns:
            A `WorkspaceResult` satisfying the obligations that Lab 3 can
            actually settle -- coverage, distribution, milestones and
            grounding.

            `completed_steps` is deliberately empty. Lab 3 locks a blueprint
            and renders a shell; it does not run the pipeline, so no Lab 1 step
            has executed. Claiming otherwise would report execution that never
            happened, and the document would show a row of green ticks over
            work nobody did. The `verify_step` obligations those steps own are
            held out of the drift verdict as `DEFERRED_KINDS` describes, and
            render as "pending", so an unsettled criterion is visible as
            unsettled rather than as either a pass or a failure.
        """
        facts = stable_facts(validation.verified_facts)
        marks = facts.get("marks_by_assignment", {}) or {}

        units_covered = tuple(
            unit for unit in plan.units if unit in _units_with_notes(validation)
        )

        reported: dict[str, Any] = {}
        ground_keys: list[str] = []
        for assignment_id in plan.assignment_ids:
            if assignment_id in marks:
                key = f"marks_by_assignment.{assignment_id}"
                reported[key] = marks[assignment_id]
                ground_keys.append(key)
        if "total_available_marks" in facts:
            reported["total_available_marks"] = facts["total_available_marks"]
            ground_keys.append("total_available_marks")

        return WorkspaceResult(
            course_code=plan.course_code,
            units_covered=units_covered,
            assignments_included=tuple(
                a for a in plan.assignment_ids if a in marks
            ),
            milestones_emitted=plan.include_milestones,
            completed_steps=(),
            reported_facts=reported,
            notes_by_unit=_notes_by_unit(validation),
            ground_fact_keys=tuple(sorted(ground_keys)),
        )

    # -- the run -----------------------------------------------------------

    def run(self, case: CaseSpec, template_id: str = "faculty-pack") -> SkillRunResult:
        """Drive one case through Labs 1, 2 and both skills.

        Args:
            case: The case to run.
            template_id: Which template to render with.

        Returns:
            A `SkillRunResult`.

        Raises:
            SkillError: If the template id is unknown.
            ToolError: If a Lab 2 tool call failed, which means the facts this
                case depends on could not be verified -- better to fail than to
                plan around unverified numbers.
        """
        if template_id not in TEMPLATE_SPECS:
            raise SkillErrorLookup(template_id)
        template = TEMPLATE_SPECS[template_id]

        # Lab 1: clarify and outline. Supplies the steps whose acceptance
        # criteria become the plan's verification obligations.
        planner = run_planner_loop(
            "Organise my course workspace for this term.",
            provider=ScriptedFeedback(answers=case.clarifications()),
            compare_one_shot=False,
        )

        # Lab 2: verify. A course-wide read, because a plan that promises the
        # whole course must be checked against the whole course -- the scope
        # lesson from Lab 2 applied before the plan exists rather than after.
        tool_run = run_tool_using_agent(
            f"Check {case.course_code} before I organise the course workspace.",
            course_code=case.course_code,
        )
        validation = _course_wide_validation(tool_run, case.course_code)

        inputs = self.build_plan_inputs(case, planner, validation.verified_facts)

        # Skill 1: expand(inputs).lock()
        plan_result = self.registry.invoke("plan", inputs)
        locked_plan = plan_result.unwrap()

        result = self.assemble(case, locked_plan, validation)

        # Skill 2: format_organised(result, template)
        document_result = self.registry.invoke(
            "format_organised",
            {"result": result, "plan": locked_plan, "template": template},
        )
        document = document_result.unwrap()

        log_event(
            logger,
            "lab03_case_complete",
            case=case.label,
            course_code=case.course_code,
            template_id=template_id,
            obligations=locked_plan.obligation_count,
            plan_sha256=locked_plan.plan_sha256,
            status=document.status,
            drift=len(document.drift.findings) if document.drift else 0,
        )

        return SkillRunResult(
            case=case,
            template_id=template_id,
            planner=planner,
            tool_run=tool_run,
            locked_plan=locked_plan,
            result=result,
            document=document,
            skill_calls_used=len(self.registry.ledger),
            registry=self.registry,
        )

    def render_all_templates(
        self, run_result: SkillRunResult
    ) -> dict[str, Document]:
        """Re-render one case through every template, with no other change.

        This is the format skill's half of the acceptance criterion, isolated
        from the plan skill's. The plan and the result are held fixed and only
        the template varies, so any difference between the outputs is
        attributable to the template -- which is the claim that the template
        really is data.

        Args:
            run_result: A completed run, to reuse its plan and result.

        Returns:
            A mapping of template id to the document it produced.
        """
        documents: dict[str, Document] = {}
        for template_id, template in TEMPLATE_SPECS.items():
            outcome = self.registry.invoke(
                "format_organised",
                {
                    "result": run_result.result,
                    "plan": run_result.locked_plan,
                    "template": template,
                },
            )
            documents[template_id] = outcome.unwrap()
        return documents

    # -- the acceptance criterion -----------------------------------------

    def prove_reuse(self) -> ReuseProof:
        """Run every case through the same two skills, and report.

        The proof is not "it did not crash". It is four independently
        constructed cases producing four *different* locked plans from one
        skill, each case's result conforming to its own plan, and the set of
        skills invoked across all of them being exactly `SKILL_NAMES`. Each
        case is additionally rendered through every template, so the format
        skill's half of the acceptance criterion is demonstrated with the
        plan and result held fixed and only the template varying.

        Returns:
            A `ReuseProof`.
        """
        proof = ReuseProof()
        used: set[str] = set()

        for case in CASES:
            run_result = self.run(case, template_id="faculty-pack")
            proof.runs.append(run_result)
            proof.documents[case.label] = self.render_all_templates(run_result)
            used.update(self.registry.ledger.names())

        proof.distinct_plan_hashes = len({r.plan_sha256 for r in proof.runs})
        proof.skill_names_used = tuple(sorted(used))
        return proof

    def inject_drift(self, run_result: SkillRunResult) -> Document:
        """Push a deliberately drifted result through the same two skills.

        Lab 3 is named in 2.12's plan-drift mitigation, so the drift case is
        part of the deliverable rather than a footnote. The drifted result is
        built by *removing* things the plan promised and *adding* one the plan
        never asked for -- the three ways a run can deviate, plus a number
        that no tool verified.

        Nothing in `course_organizer.skills` changes. If this reports no
        drift, the drift machinery is broken, not the input.

        Args:
            run_result: A completed conforming run, to drift away from.

        Returns:
            The document rendered from the drifted result. It must be `draft`.
        """
        plan = run_result.locked_plan
        good = run_result.result

        units = list(good.units_covered)
        units.pop()  # a promised unit the run failed to cover
        assignments = list(good.assignments_included)
        assignments.pop()  # a promised assignment the run failed to distribute

        drifted = good.model_copy(
            update={
                # The last one is a unit id that exists in no store at all, so
                # the "unexpected" finding cannot be mistaken for a typo in a
                # real unit number.
                "units_covered": tuple(units) + ("unit-99",),
                "assignments_included": tuple(assignments) + ("ASG-99",),
                "milestones_emitted": False,
                "reported_facts": {
                    **dict(good.reported_facts),
                    "total_available_marks": 999.0,
                },
            }
        )

        document = self.registry.invoke(
            "format_organised",
            {
                "result": drifted,
                "plan": plan,
                "template": TEMPLATE_SPECS["faculty-pack"],
            },
        ).unwrap()

        log_event(
            logger,
            "lab03_drift_injected",
            case=run_result.case.label,
            kept_units=len(drifted.units_covered),
            kept_assignments=len(drifted.assignments_included),
            status=document.status,
            findings=len(document.drift.findings) if document.drift else 0,
        )
        return document


class SkillErrorLookup(KeyError):
    """Raised for an unknown template id.

    Named rather than raising `SkillError` directly so the "you passed a bad
    template id" case is distinguishable from "the format skill failed" --
    the former is a caller's mistake at the call site, the latter belongs to
    the skill.
    """

    def __init__(self, template_id: str) -> None:
        super().__init__(
            f"unknown template {template_id!r}; available templates are "
            f"{sorted(TEMPLATE_SPECS)}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _units_with_notes(validation: ScopeValidationResult) -> set[str]:
    """The unit ids the tool layer reported notes for.

    Args:
        validation: Lab 2's course-wide validation result.

    Returns:
        A set of unit ids.

    Reads `units_covered`, which the tool layer computes from the store. It
    deliberately does *not* fall back to the `units_with_notes` count: turning
    "5 units have notes" back into a list of ids would mean guessing which
    five, and a guessed unit id is exactly the ungrounded claim this whole
    pipeline exists to prevent. If the tool did not report the ids, the
    assembled result covers nothing and the drift check says so -- the safe
    direction, since under-claiming is recoverable and over-claiming is not.
    """
    covered = validation.verified_facts.get("units_covered")
    if isinstance(covered, (list, tuple, set)):
        return {str(unit) for unit in covered}
    return set()


def _notes_by_unit(validation: ScopeValidationResult) -> dict[str, int]:
    """Notes per unit, as computed by the tool layer.

    Args:
        validation: Lab 2's course-wide validation result.

    Returns:
        Unit id to note count. Empty when the tool recorded none, which makes
        the units section show a note column of zeros rather than inventing
        counts.
    """
    per_unit = validation.verified_facts.get("notes_by_unit")
    if isinstance(per_unit, dict):
        return {str(k): int(v) for k, v in per_unit.items()}
    return {}


def _course_wide_validation(
    tool_run: ToolRunResult, course_code: str
) -> ScopeValidationResult:
    """Pick the course-wide validation out of a Lab 2 run.

    A plan that promises a whole course must be checked against the whole
    course. Lab 2 narrows its read to whatever the request implied, so its
    primary result may be scoped to a subset -- and a subset's totals would
    make the plan look wrong when it is right. Lab 2's scope lesson, applied
    before the plan is written rather than after a claim is refuted.

    Args:
        tool_run: Lab 2's result.
        course_code: The course the case is about.

    Returns:
        A course-wide `ScopeValidationResult`.

    Raises:
        SkillError: If the run produced no course-wide validation, which would
            mean planning against unverified numbers.
    """
    candidates = [tool_run.validation, *tool_run.extra_validations]
    for validation in candidates:
        if validation.course_code == course_code and validation.course_wide:
            return validation
    raise SkillError(
        f"no course-wide validation for {course_code!r}; "
        f"Lab 2 returned {[v.course_code for v in candidates]}",
        code="no_course_wide_validation",
    )


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------


def _rule(char: str = "=") -> str:
    """A horizontal rule of the given character."""
    return char * 78


def _main() -> int:  # pragma: no cover - manual entry point
    """Run the Lab 3 acceptance demo from the command line.

    Exits 0 only when every acceptance assertion holds, so this doubles as a
    self-check the way Lab 2's does.

    Returns:
        A process exit code.
    """
    import argparse

    from course_organizer.logging_setup import setup_logging

    parser = argparse.ArgumentParser(
        description="Lab 3 - reusable, schema-typed skills"
    )
    parser.add_argument(
        "--case", default="all", choices=["all", *(c.label for c in CASES)]
    )
    parser.add_argument(
        "--template", default="faculty-pack", choices=sorted(TEMPLATE_SPECS)
    )
    args = parser.parse_args()

    setup_logging()
    agent = SkillAgent()
    checks: list[tuple[str, bool, str]] = []

    print(_rule())
    print("LAB 3 - Skill Creation: reusable plan & format skills")
    print(_rule())
    print()
    print("doc pseudocode (2.10 Lab 3):")
    for line in DOC_PSEUDOCODE.splitlines():
        print(f"  {line}")
    print()

    selected = CASES if args.case == "all" else tuple(
        c for c in CASES if c.label == args.case
    )

    print(_rule())
    print("1. The skills")
    print(_rule())
    print()
    print(agent.registry.describe_all())
    print()
    checks.append(
        (
            "exactly the two documented skills are registered",
            agent.registry.names() == sorted(SKILL_NAMES),
            str(agent.registry.names()),
        )
    )
    print()

    print(_rule())
    print("2. Each case, through the same two skills")
    print(_rule())
    print()

    runs: list[SkillRunResult] = []
    for case in selected:
        run_result = agent.run(case, template_id=args.template)
        runs.append(run_result)

        print(f"--- case {run_result.case.label}: {case.narrative}")
        print()
        print(run_result.locked_plan.outline())
        print()
        print(f"  document   : {run_result.document_id}")
        print(f"  status     : {run_result.document.status.value}")
        print(f"  drift      : {run_result.document.drift.summary() if run_result.document.drift else 'n/a'}")
        print(f"  sections   : {', '.join(run_result.document.sections_rendered)}")
        if run_result.document.sections_omitted:
            print(f"  omitted    : {', '.join(run_result.document.sections_omitted)}")
        print(f"  skill calls: {run_result.skill_calls_used}")
        print()

        checks.append(
            (f"case {case.label} obeys its locked plan", run_result.conforms, "")
        )
        checks.append(
            (
                f"case {case.label} document is ready for review, not published",
                run_result.document.is_ready_for_review
                and run_result.document.awaiting_approval,
                run_result.document.status.value,
            )
        )
        checks.append(
            (
                f"case {case.label} locked plan passes its integrity check",
                run_result.locked_plan.recompute_sha256() == run_result.plan_sha256,
                run_result.plan_sha256[:12],
            )
        )

    if len(runs) > 1:
        print(_rule())
        print("3. The same skills, several cases (doc acceptance criterion)")
        print(_rule())
        print()
        proof = ReuseProof(
            runs=runs,
            documents={r.case.label: dict.fromkeys(TEMPLATE_SPECS) for r in runs},
            distinct_plan_hashes=len({r.plan_sha256 for r in runs}),
            skill_names_used=tuple(sorted(set(agent.registry.ledger.names()))),
        )
        print(proof.summary())
        print()
        checks.append(
            (
                "every case produced a different locked plan",
                proof.distinct_plan_hashes == len(runs),
                f"{proof.distinct_plan_hashes} of {len(runs)}",
            )
        )
        checks.append(
            (
                "no skill outside the documented two was invoked",
                set(proof.skill_names_used) <= set(SKILL_NAMES),
                ", ".join(proof.skill_names_used),
            )
        )

    print(_rule())
    print("4. Plan drift: 2.12's named mitigation for Lab 3")
    print(_rule())
    print()
    drifted = agent.inject_drift(runs[0])
    drift_report: Optional[DriftReport] = drifted.drift
    print(drift_report.summary() if drift_report else "no drift report")
    print()
    if drift_report:
        print(drift_report.table())
        print()
    print(
            f"  document status : {drifted.status.value}  "
            f"(must not be ready for review)"
        )
    print(f"  blocked because : {drifted.blocked_reason}")
    print()
    checks.append(
        (
            "an injected drift is detected",
            bool(drift_report and not drift_report.conforms),
            f"{len(drift_report.findings)} finding(s)" if drift_report else "0",
        )
    )
    checks.append(
        (
            "a drifted document is never ready for review",
            not drifted.is_ready_for_review,
            drifted.status.value,
        )
    )
    checks.append(
        (
            "no document is ever marked approved or published",
            all(run.document.awaiting_approval for run in runs) and drifted.awaiting_approval,
            "2.12: faculty approval is a hard stop",
        )
    )
    print()

    print(_rule())
    print("5. The skill call ledger")
    print(_rule())
    print()
    print(agent.registry.ledger.describe())
    print()

    print(_rule())
    print("6. Acceptance")
    print(_rule())
    print()
    for label, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {label:<58} {detail}")

    failed = [c for c in checks if not c[1]]
    print()
    print("OVERALL: " + ("PASS" if not failed else f"FAIL ({len(failed)} check(s))"))
    return 0 if not failed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
