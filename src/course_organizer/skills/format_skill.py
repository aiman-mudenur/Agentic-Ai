"""The `format_organised` skill: render a result against a template spec.

Document 2.10, Lab 3 pseudocode, verbatim:

    skill format_organised(result, template) -> Document

and the Output column reads: "A locked plan object; a formatted organised
course workspace (notes, assignments, grades) shell."

Two decisions make this skill reusable rather than a second hard-coded case.

**The template is data, not code.** A template is a `TemplateSpec`: an
ordered tuple of section names, a body style, and a few flags. Which sections
appear, in what order, and how they are rendered is decided by that value
alone. So a new output format is a new `TemplateSpec`, not a new function --
which is what the document means by "template spec" as an *input*.

**One set of builders, several renderers.** Every section builder returns
`(payload, lines)`: structured data plus pre-laid-out text. The `body_style`
then picks a renderer for those same parts. `markdown`, `plain` and `json`
are presentation, not behaviour -- the same sections, the same order, the
same data, three renderings. Adding a style is one entry in `_RENDERERS`,
and a test asserts every entry is reachable, so a style cannot be added
untested.

The drift check lives here rather than in the plan skill because of *when* it
has to happen. Document 2.12 requires that "validation rejects any deviation
before the human sees it", and the formatted document is the last artefact
before the human. A drift check somewhere earlier can be bypassed by the step
that runs afterwards; one that annotates the document itself cannot, because
the finding is part of what gets read. A document with unresolved drift is
therefore marked `draft`, never `final` -- and it still renders, because hiding
the deviation would defeat the purpose of reporting it.

Nothing here knows what a course is either. The document is a function of the
result, the template, and the plan's own scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from course_organizer.logging_setup import log_event
from course_organizer.skills.base import (
    SkillContext,
    SkillError,
    SkillRegistry,
    SkillSpec,
)
from course_organizer.skills.plan_skill import (
    KIND_VERIFY_STEP,
    DriftReport,
    LockedPlan,
    WorkspaceResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The template spec
# ---------------------------------------------------------------------------

#: The closed section vocabulary. A renderer that only understands these names
#: can be total: every valid template is renderable, because a template cannot
#: name a section that does not exist. An open vocabulary would force every
#: builder to decide what to do with a section it does not understand.
SECTIONS: tuple[str, ...] = (
    "header",        # title, course, scope, approver
    "plan_lock",     # the locked plan's hash and obligation count
    "units",         # units covered, with note counts
    "assignments",   # assignments distributed, with marks
    "milestones",    # milestone tracking status
    "checks",        # acceptance criteria carried from the outline plan
    "drift",         # deviations from the locked plan
    "approval",      # the human gate, per 2.12
)

STYLE_MARKDOWN = "markdown"
STYLE_PLAIN = "plain"
STYLE_JSON = "json"
BODY_STYLES: tuple[str, ...] = (STYLE_MARKDOWN, STYLE_PLAIN, STYLE_JSON)

DEFAULT_HEADINGS: Mapping[str, str] = {
    "header": "Course Workspace",
    "plan_lock": "Locked Blueprint",
    "units": "Units Covered",
    "assignments": "Assignments Distributed",
    "milestones": "Project Milestones",
    "checks": "Acceptance Checks",
    "drift": "Deviations From The Locked Plan",
    "approval": "Approval Required",
}

#: Sections a template MUST declare. Enforced by `TemplateSpec` validation,
#: not checked after the fact at render time, because a template that omits
#: `approval` is a template that would produce a document nobody is obliged to
#: sign off -- 2.12's hard stop. It is far cheaper to refuse the template than
#: to notice the hole in a finished artefact.
REQUIRED_TEMPLATE_SECTIONS: tuple[str, ...] = ("header", "approval")


class Section(BaseModel):
    """One section of the output document, as a template declares it.

    Attributes:
        name: A key from `SECTIONS`.
        heading: The text to show. Data, so two templates could render the
            same section under different names.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    heading: str = Field(min_length=1)


def _markdown_heading(heading: str, level: int) -> str:
    """A markdown heading at the given level.

    Args:
        heading: The heading text.
        level: Number of leading `#` characters, 1-6.

    Returns:
        The heading line.

    Raises:
        ValueError: If `level` is outside 1-6. Markdown renderers treat a
            seventh `#` as literal text, so the mistake would surface as
            broken output rather than as an error here.
    """
    if not 1 <= level <= 6:
        raise ValueError(f"markdown heading level must be 1-6, got {level}")
    return f"{'#' * level} {heading}"


class TemplateSpec(BaseModel):
    """Document Input: "template spec".

    Everything about the output's shape lives here. Two `TemplateSpec` values
    that are equal must produce byte-identical documents -- and a test asserts
    exactly that, because a template that quietly leaks behaviour from
    somewhere else is how "reusable across cases" silently becomes false.

    Attributes:
        template_id: Stable id, for the document header and the ledger.
        title: Document title.
        audience: Who the document is for. Affects nothing but the header;
            recorded because a document's audience is part of its spec, and
            omitting it invites a later lab adding it as hidden state.
        sections: Ordered section names. Order is the template's choice, so
            two templates with the same set in a different order are
            legitimately different documents.
        body_style: One of `BODY_STYLES`.
        include_empty_sections: Whether an optional section with no content
            still appears as a placeholder, or is omitted entirely.
        include_drift_table: Whether a *clean* drift section is shown. Drift
            findings are always shown when they exist; this flag only governs
            the "None." case.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    sections: tuple[str, ...] = Field(min_length=1)
    body_style: str = Field(default=STYLE_MARKDOWN)
    include_empty_sections: bool = False
    include_drift_table: bool = True

    @model_validator(mode="after")
    def _sections_are_known_and_required_present(self) -> "TemplateSpec":
        """Reject a template naming a section or style that does not exist.

        Runs at construction, so an invalid template never reaches a builder
        and no builder needs a fallback branch for something it cannot build.

        Returns:
            The validated spec.

        Raises:
            ValueError: If a section name or `body_style` is unknown, or if
                `header` or `approval` is absent.
        """
        unknown = [name for name in self.sections if name not in SECTIONS]
        if unknown:
            raise ValueError(
                f"template {self.template_id!r} names unknown section(s) "
                f"{unknown}; valid sections are {list(SECTIONS)}"
            )

        if self.body_style not in BODY_STYLES:
            raise ValueError(
                f"template {self.template_id!r} asks for body_style "
                f"{self.body_style!r}; available styles are {list(BODY_STYLES)}"
            )

        missing = [
            name for name in REQUIRED_TEMPLATE_SECTIONS if name not in self.sections
        ]
        if missing:
            raise ValueError(
                f"template {self.template_id!r} must declare {missing}; "
                f"document 2.12 requires every artefact to name its approver "
                f"and to be marked a draft"
            )
        return self

    def sections_resolved(self) -> tuple[Section, ...]:
        """The section descriptors, in template order.

        Returns:
            A `Section` per declared section, using `DEFAULT_HEADINGS`.

        Raises:
            SkillError: Retained as a defensive guard. Unreachable while
                construction validation holds; if it ever became reachable
                the bug would be in the validator, and naming that is better
                than a silent skip.
        """
        resolved: list[Section] = []
        for name in self.sections:
            if name not in SECTIONS:
                raise SkillError(
                    f"template {self.template_id!r} names unknown section "
                    f"{name!r}; valid sections are {list(SECTIONS)}",
                    code="unknown_section",
                )
            resolved.append(Section(name=name, heading=DEFAULT_HEADINGS[name]))
        return tuple(resolved)


# ---------------------------------------------------------------------------
# The output document
# ---------------------------------------------------------------------------


class DocumentStatus(str, Enum):
    """The lifecycle a rendered document can be in.

    Two states, and deliberately no third.

    ``DRAFT``
        Something is unresolved -- almost always drift from the locked plan.
        The document renders anyway, because hiding the deviation would defeat
        the purpose of reporting it.

    ``READY_FOR_REVIEW``
        No drift, and the plan's integrity check passed. The document is
        complete and internally consistent. It is still *not* approved and
        *not* published.

    There is no ``FINAL`` or ``PUBLISHED`` state, and that absence is the
    point. Document 2.12: "the blueprint/plan is locked before work begins;
    validation is a hard gate; the human approves before publish." Passing
    validation is what earns a document the right to be *seen* by the faculty
    -- never the right to have been accepted by them. Lab 3 has no human gate,
    so a state called "final" would be a state this lab cannot honestly
    produce, and a type that permits it is an invitation to invent one later.
    Lab 7 implements the gate and will earn any stronger state.
    """

    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"


class Document(BaseModel):
    """The formatted output. Document 2.10 Lab 3 Output.

    Holds both the rendered body *and* the structured data behind it. Two
    reasons: a caller may want the text (a human reads it) or the structure
    (a test asserts on it), and re-parsing rendered markdown to assert on it
    would make every test hostage to the renderer's escaping.

    Attributes:
        document_id: Stable id derived from the plan, the result and the
            template -- content-addressed, so the same three always produce
            the same id and a repeated run is recognisable as the same
            document rather than a new one.
        template_id: Which template produced it.
        body: The rendered text, in the template's `body_style`.
        data: Section contents as structured data.
        status: A `DocumentStatus`. Never anything stronger than
            `READY_FOR_REVIEW`.
        drift: The drift verdict, retained so the document cannot be read
            without its drift state.
        sections_rendered: Which sections actually appeared.
        sections_omitted: Which were declared but had nothing to say.
        blocked_reason: Why the document is not `READY_FOR_REVIEW`, or empty.
        produced_at: UTC timestamp.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    body: str
    data: Mapping[str, Any] = Field(default_factory=dict)
    status: DocumentStatus = DocumentStatus.DRAFT
    drift: Optional[DriftReport] = None
    sections_rendered: tuple[str, ...] = ()
    sections_omitted: tuple[str, ...] = ()
    blocked_reason: str = ""
    produced_at: str = ""

    @property
    def is_ready_for_review(self) -> bool:
        """Whether this document may be shown to the approving human.

        True when nothing about it is unresolved. It says nothing about
        whether the human approved it -- they have not, and Lab 3 gives nobody
        the means to say so.
        """
        return self.status == DocumentStatus.READY_FOR_REVIEW

    @property
    def awaiting_approval(self) -> bool:
        """Always True at this lab, by construction.

        Document 2.12's guardrail is absolute: "no grade, feedback or
        published artefact advances without faculty approval." Lab 3 has no
        approval step, so every document it produces is awaiting approval. The
        property exists so that no caller has to know that -- and so a later
        lab that *does* implement the gate has one obvious place to change.
        """
        return True

    def line_count(self) -> int:
        """Body length in lines, so the CLI can show the size it produced."""
        return len(self.body.splitlines())


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
#
# Uniform signature: every builder takes the result, the plan, the template
# and the drift report, and returns `(payload, lines)`. The uniform shape is
# what lets `_BUILDERS` be a plain dispatch table, and passing `drift` in --
# rather than having each builder recompute it -- is what guarantees the
# rendered drift section and the document's status cannot disagree.

Builder = Callable[
    [WorkspaceResult, LockedPlan, TemplateSpec, DriftReport],
    "tuple[Mapping[str, Any], list[str]]",
]


def _build_header(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """Title, scope and approver. Never empty, so always rendered."""
    payload = {
        "title": template.title,
        "audience": template.audience,
        "course_code": plan.course_code,
        "course_name": plan.course_name,
        "units_in_scope": list(plan.units),
        "assignments_in_scope": list(plan.assignment_ids),
        "as_of": plan.as_of,
        # Rendered in full, not summarised. A reader who cannot see which
        # parameters were assumed rather than asked has no way to tell a
        # deliberate narrowing from a mistake, and that is exactly the question
        # 2.12's drift row is about.
        "assumptions": list(plan.notes),
    }
    lines = [
        template.title,
        "",
        f"Course      : {plan.course_code} ({plan.course_name})",
        f"Units       : {', '.join(plan.units)}",
        f"Assignments : {', '.join(plan.assignment_ids) or 'none'}",
        f"Audience    : {template.audience}",
    ]
    if plan.as_of:
        lines.append(f"As of       : {plan.as_of}")
    for note in plan.notes:
        lines.append(f"Assumed     : {note}")
    return payload, lines


def _build_plan_lock(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """The locked blueprint's identity, so the document names what it obeys."""
    payload = {
        "plan_sha256": plan.plan_sha256,
        "obligation_count": plan.obligation_count,
        "obligation_kinds": sorted({o.kind for o in plan.obligations}),
        "integrity": "verified",
    }
    lines = [
        f"Plan sha256      : {plan.plan_sha256}",
        f"Obligations      : {plan.obligation_count}",
        f"Obligation kinds : {', '.join(payload['obligation_kinds'])}",
        f"Locked at        : {plan.locked_at or '(not stamped)'}",
    ]
    return payload, lines


def _build_units(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """Units covered, with their note counts.

    Emptiness here is meaningful rather than cosmetic: an empty unit list is
    exactly what a drifted result looks like, so the section is omitted
    instead of rendered as a bare heading.
    """
    if not result.units_covered:
        return {}, []

    rows = [
        {"unit": unit, "notes": result.notes_by_unit.get(unit, 0)}
        for unit in result.units_covered
    ]
    payload = {"units": rows, "covered": len(rows)}
    width = max(len(row["unit"]) for row in rows)
    lines = [f"{row['unit'].ljust(width)}  {row['notes']:>3} note(s)" for row in rows]
    return payload, lines


def _build_assignments(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """Assignments distributed, with their computed marks.

    The mark column comes from `reported_facts`, which the drift check has
    already compared against the plan's verified facts. So a mark appearing
    here was verified by a tool *and* confirmed to match the locked plan --
    Lab 2's "never hand-done in the prompt" rule surviving two more layers.
    """
    if not result.assignments_included:
        return {}, []

    rows = [
        {
            "assignment_id": assignment_id,
            "max_marks": result.reported_facts.get(
                f"marks_by_assignment.{assignment_id}"
            ),
        }
        for assignment_id in result.assignments_included
    ]
    payload = {"assignments": rows, "included": len(rows)}
    width = max(len(row["assignment_id"]) for row in rows)
    lines = [
        f"{row['assignment_id'].ljust(width)}  "
        f"{'-' if row['max_marks'] is None else row['max_marks']} marks"
        for row in rows
    ]
    total = result.reported_facts.get("total_available_marks")
    if total is not None:
        lines.extend(["", f"Total available marks: {total}"])
    return payload, lines


def _build_milestones(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """Milestone tracking status. Always rendered -- it has a real answer."""
    if plan.include_milestones:
        payload = {
            "in_scope": True,
            "emitted": result.milestones_emitted,
            "satisfied": result.milestones_emitted,
        }
        lines = [
            "In scope : yes",
            f"Emitted  : {'yes' if result.milestones_emitted else 'NO'}",
        ]
    else:
        payload = {"in_scope": False, "emitted": result.milestones_emitted,
                   "satisfied": True}
        lines = ["In scope : no", "Emitted  : n/a"]
    return payload, lines


def _build_checks(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """The Lab 1 acceptance criteria, with whether their step has run.

    These are the `verify_step` obligations, shown by criterion rather than by
    step: one step can carry several criteria, and the count of criteria is
    the honest denominator for how much of the outline survived into the
    blueprint.

    A criterion whose step has not run is shown as ``pending``, not ``failed``.
    Lab 3 locks a blueprint and renders a shell; it does not execute the
    pipeline, so those criteria are settled by Lab 7 when the node graph runs.
    Rendering them as failures would put a red mark on work that has not been
    attempted, which is the same fabrication as marking them passed.
    """
    checks = plan.obligations_of(KIND_VERIFY_STEP)
    if not checks:
        return {}, []

    rows = [
        {
            "step_id": obligation.subject,
            "criterion": obligation.statement,
            "step_ran": obligation.subject in result.completed_steps,
        }
        for obligation in checks
    ]
    for row in rows:
        row["state"] = "ran" if row["step_ran"] else "pending"

    passed = sum(1 for row in rows if row["step_ran"])
    payload = {
        "checks": rows,
        "total": len(rows),
        "ran": passed,
        "pending": len(rows) - passed,
        "settled_by": "Lab 7 (the node graph runs the steps)",
    }
    lines = [
        f"[{'x' if row['step_ran'] else ' '}] {row['step_id']}: {row['criterion']}"
        for row in rows
    ]
    lines.extend(
        [
            "",
            f"{passed} ran, {len(rows) - passed} pending - "
            f"the locked blueprint carries them forward to the lab that "
            f"executes the steps",
        ]
    )
    return payload, lines


def _build_drift(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """Deviations from the locked plan. The section 2.12 is really about.

    Receives the report rather than recomputing it. The caller needs it to
    decide `status`, and computing it twice would leave the rendered body and
    the document status free to disagree -- a document reading "no drift"
    while carrying `status=draft` is worse than either state alone.

    A *clean* drift section is omitted when the template says so. Findings
    are never omitted, and the verdict is on the `Document` regardless, so
    hiding the section cannot launder a drifted document.
    """
    if drift.conforms and not template.include_drift_table:
        return {}, []

    payload = {
        "conforms": drift.conforms,
        "summary": drift.summary(),
        "findings": [
            {
                "kind": finding.kind,
                "obligation_id": finding.obligation_id,
                "subject": finding.subject,
                "detail": finding.detail,
            }
            for finding in drift.findings
        ],
        "count": len(drift.findings),
    }

    if drift.conforms:
        return payload, ["None. Every obligation in the locked plan was met."]

    lines = [drift.summary(), ""]
    width = max(len(finding.obligation_id) for finding in drift.findings)
    lines.extend(
        f"  [{finding.kind}] {finding.obligation_id.ljust(width)}  "
        f"{finding.detail}"
        for finding in drift.findings
    )
    return payload, lines


def _build_approval(
    result: WorkspaceResult,
    plan: LockedPlan,
    template: TemplateSpec,
    drift: DriftReport,
) -> tuple[Mapping[str, Any], list[str]]:
    """The human gate. 2.12's hard stop, rendered so it cannot be missed.

    The status line states the *live* status rather than a fixed one, so a
    document that reads "READY FOR REVIEW" is visibly still awaiting a person,
    and a drifted one visibly is not ready at all. A static "DRAFT" string here
    would have been a lie in one direction the moment a clean document was
    produced, which is exactly the kind of fixed reassurance a reader cannot
    check.
    """
    status = (
        DocumentStatus.DRAFT
        if drift.blocking
        else DocumentStatus.READY_FOR_REVIEW
    )
    payload = {
        "authority": plan.human_authority,
        "required": True,
        "approved": False,
        "published": False,
        "status": status.value,
        "rule": (
            "Document 2.12: no grade, feedback or published artefact "
            "advances without faculty approval."
        ),
    }
    lines = [
        f"Approver : {plan.human_authority}",
        f"Status   : {status.value.replace('_', ' ').upper()}",
        "Approved : no - this lab implements no approval step",
        "Published: no",
        f"Rule     : {payload['rule']}",
    ]
    return payload, lines


_BUILDERS: Mapping[str, Builder] = {
    "header": _build_header,
    "plan_lock": _build_plan_lock,
    "units": _build_units,
    "assignments": _build_assignments,
    "milestones": _build_milestones,
    "checks": _build_checks,
    "drift": _build_drift,
    "approval": _build_approval,
}


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

Renderer = Callable[
    [Sequence[tuple[Section, Mapping[str, Any], Sequence[str]]], TemplateSpec],
    str,
]


def _render_markdown(
    sections: Sequence[tuple[Section, Mapping[str, Any], Sequence[str]]],
    template: TemplateSpec,
) -> str:
    """Markdown headings with the structured payload in an HTML comment.

    Keeping the payload in the text means a rendered `.md` file is still
    machine-checkable, so the document does not have to be re-rendered to be
    asserted on.
    """
    parts: list[str] = [f"<!-- template: {template.template_id} -->", ""]
    for index, (section, payload, lines) in enumerate(sections):
        parts.append(_markdown_heading(section.heading, level=2))
        parts.append("")
        parts.extend(lines)
        parts.append("")
        parts.append(
            f"<!-- data: {json.dumps(payload, sort_keys=True, default=str)} -->"
        )
        if index < len(sections) - 1:
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _render_plain(
    sections: Sequence[tuple[Section, Mapping[str, Any], Sequence[str]]],
    template: TemplateSpec,
) -> str:
    """Uppercase headings and no markup at all.

    This is the "has to render in a terminal with no markdown renderer"
    deployment constraint, which is a real one and worth a style for.
    """
    rule = "=" * 72
    parts: list[str] = [rule, f"TEMPLATE: {template.template_id}", rule, ""]
    for section, _payload, lines in sections:
        parts.append(section.heading.upper())
        parts.append("-" * len(section.heading))
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _render_json(
    sections: Sequence[tuple[Section, Mapping[str, Any], Sequence[str]]],
    template: TemplateSpec,
) -> str:
    """Machine-readable. Used by the later labs to consume documents.

    The `lines` are dropped: they are a rendering of `payload`, and keeping
    both would put two representations of the same fact in one JSON file where
    they could disagree. A consumer takes the structured form and formats it
    itself.
    """
    return (
        json.dumps(
            {
                "template_id": template.template_id,
                "body_style": template.body_style,
                "sections": {
                    section.name: dict(payload) for section, payload, _ in sections
                },
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


_RENDERERS: Mapping[str, Renderer] = {
    STYLE_MARKDOWN: _render_markdown,
    STYLE_PLAIN: _render_plain,
    STYLE_JSON: _render_json,
}


# ---------------------------------------------------------------------------
# The skill
# ---------------------------------------------------------------------------


class FormatInputs(BaseModel):
    """The format skill's input: the result, its plan, and the template.

    Attributes:
        result: What the run produced.
        plan: The locked blueprint the result must obey. Required, not
            optional: a document rendered without its plan could not report
            drift, and drift is why this skill exists rather than a plain
            `str.format` call.
        template: The template spec.
    """

    model_config = ConfigDict(extra="forbid")

    result: WorkspaceResult
    plan: LockedPlan
    template: TemplateSpec


def format_organised(inputs: FormatInputs, context: SkillContext) -> Document:
    """Document: `skill format_organised(result, template) -> Document`.

    Args:
        inputs: The result, the locked plan and the template spec.
        context: Run context; used for the ledger, not otherwise.

    Returns:
        The rendered `Document`. Its `status` is `final` only when drift is
        empty.

    Raises:
        SkillError: If the template names an unknown section or an unknown
            `body_style`. Both are template bugs, and construction-time
            validation normally catches them first, so reaching here means the
            validator and the renderer disagree -- which this names rather
            than papering over.
    """
    template = inputs.template
    result = inputs.result
    plan = inputs.plan

    if template.body_style not in _RENDERERS:
        raise SkillError(
            f"template {template.template_id!r} asks for body_style "
            f"{template.body_style!r}; available styles are "
            f"{sorted(_RENDERERS)}",
            code="unknown_style",
        )

    resolved = template.sections_resolved()

    # Drift is checked ONCE, before any section is built, and that single
    # report is both rendered into the drift section and attached to the
    # document. Two computations would be two chances to disagree.
    drift = result.check_against(plan)

    rendered: list[tuple[Section, Mapping[str, Any], list[str]]] = []
    omitted: list[str] = []
    for section in resolved:
        payload, lines = _BUILDERS[section.name](result, plan, template, drift)
        if not lines and not template.include_empty_sections:
            omitted.append(section.name)
            continue
        rendered.append((section, payload, lines))

    body = _RENDERERS[template.body_style](rendered, template)

    # Drift is the only blocker: a template omitting `approval` was rejected at
    # construction, and an optional section with nothing to say is legitimately
    # absent rather than a defect. Note what a clean result does NOT buy --
    # `READY_FOR_REVIEW`, never "final". See `DocumentStatus`.
    blocked: list[str] = []
    if drift.blocking:
        blocked.append(f"{len(drift.findings)} deviation(s) from the locked plan")

    document = Document(
        document_id=_document_id(result, template, plan),
        template_id=template.template_id,
        body=body,
        data={section.name: dict(payload) for section, payload, _ in rendered},
        status=DocumentStatus.DRAFT if blocked else DocumentStatus.READY_FOR_REVIEW,
        drift=drift,
        sections_rendered=tuple(section.name for section, _, _ in rendered),
        sections_omitted=tuple(omitted),
        blocked_reason="; ".join(blocked),
        produced_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )

    log_event(
        logger,
        "skill_format_organised",
        document_id=document.document_id,
        template_id=template.template_id,
        body_style=template.body_style,
        status=document.status,
        sections=len(rendered),
        drift=len(drift.findings),
    )
    return document


def _document_id(
    result: WorkspaceResult, template: TemplateSpec, plan: LockedPlan
) -> str:
    """Content-derived document id.

    Args:
        result: The rendered result.
        template: The template spec.
        plan: The locked plan.

    Returns:
        An id like ``DOC-CSxxx-faculty-pack-3f2a91c0d4``.

    Derived from the plan hash, the result and the template id, so the same
    three always produce the same id. A timestamp would make every run look
    like a new document and defeat the reproducibility the `scripted` backend
    exists to provide.
    """
    encoded = json.dumps(
        {
            "plan_sha256": plan.plan_sha256,
            "course_code": result.course_code,
            "template_id": template.template_id,
            "body_style": template.body_style,
            "sections": list(template.sections),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"DOC-{result.course_code}-{template.template_id}-{digest[:10]}"


FORMAT_SKILL_SPEC = SkillSpec(
    name="format_organised",
    description=(
        "render a produced result against a template spec into a Document, "
        "annotating any deviation from the locked plan (doc 2.10 Lab 3)"
    ),
    input_model=FormatInputs,
    output_model=Document,
    handler=format_organised,
    case_agnostic=True,
    tags=("assembly", "rendering", "phase-a"),
)


def register_format_skill(registry: SkillRegistry) -> SkillRegistry:
    """Register the `format_organised` skill on a registry.

    Args:
        registry: The registry to add to.

    Returns:
        The same registry, so calls chain.
    """
    registry.register(FORMAT_SKILL_SPEC)
    return registry
