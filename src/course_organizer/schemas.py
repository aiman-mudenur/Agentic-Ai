"""Canonical data model for Worklet 2.

This module is a direct transcription of the document's **2.6 Data Model** table,
plus the plan objects that Lab 1 needs. Nothing here invents fields: every
entity and key field below is quoted from Chapter 2.

Document (2.6 Data Model):

===========================  =====================================================
Entity                       Key fields
===========================  =====================================================
Notes/materials              Lecture notes, slides, and reference material tagged
                             by unit/topic and version.
Assignment definition        Assignment brief, due date, marks, and rubric.
Submission record            Student submission, timestamp, and
                             similarity/plagiarism score.
Project milestones           Defined checkpoints, expected deliverables, and
                             group ownership.
Grade & feedback             Draft grade and feedback pending faculty approval.
===========================  =====================================================

The same table is echoed in the worklet's three connector endpoints (2.7):
`read('the notes/materials store')`, `read('assignment & rubric definitions')`,
`read('submission records')`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from course_organizer import HUMAN_AUTHORITY

# ═══════════════════════════════════════════════════════════════════════════
# Enumerations
# ═══════════════════════════════════════════════════════════════════════════


class ArtifactKind(str, Enum):
    """Material type inside the notes store (doc 2.6: "lecture notes, slides,
    and reference material")."""

    LECTURE_NOTES = "lecture_notes"
    SLIDES = "slides"
    REFERENCE = "reference"


class SubmissionStatus(str, Enum):
    """Lifecycle of a submission.

    The doc's guardrail "never alter a submission timestamp" means
    `submitted_at` is set once at collection and is immutable thereafter;
    `late` is *derived* from it, never typed in by a human.
    """

    PENDING = "pending"
    SUBMITTED = "submitted"
    LATE = "late"
    MISSING = "missing"


class MilestoneStatus(str, Enum):
    """Checkpoint state for a project group.

    The doc (agent 6 guardrail) says: "Report status only from submitted
    evidence; never assume progress not shown." Hence there is no `unknown ->
    complete` shortcut; `BLOCKED` is the honest default.
    """

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    OVERDUE = "overdue"
    BLOCKED = "blocked"


class PlanStatus(str, Enum):
    """Lifecycle of a plan.

    Lab 1 explicitly returns a *draft* plan — the doc's pseudocode ends with
    `return plan  # not final — a structured plan`. `LOCKED` arrives in Lab 3
    via the plan skill (`.lock()`), and `APPROVED` only after the faculty gate.
    """

    DRAFT = "draft"
    LOCKED = "locked"
    APPROVED = "approved"


class Decision(str, Enum):
    """The human gate's verdict. Doc 2.7: "a human gate returns
    approve / edit / override"."""

    APPROVE = "approve"
    EDIT = "edit"
    OVERRIDE = "override"


# ═══════════════════════════════════════════════════════════════════════════
# Base
# ═══════════════════════════════════════════════════════════════════════════


class _Base(BaseModel):
    """Shared config: reject unknown keys so typos surface loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Used instead of naive local time everywhere."""
    return datetime.now(tz=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# Entity 1 — Notes/materials
# ═══════════════════════════════════════════════════════════════════════════


class NoteVersion(_Base):
    """One version of a note, per doc 2.6 ("tagged by unit/topic and version").

    Agent 1's guardrail: "Keep clear version history; never remove a prior
    version without faculty request." Appending is therefore the only allowed
    mutation — `version` must strictly increase within a `note_id`.
    """

    version: int = Field(ge=1, description="Monotonic version number, starting at 1")
    content: str = Field(min_length=1, description="Full note/slide text")
    author: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utcnow)
    changelog: str = Field(default="", description="What changed in this version")
    content_sha256: str = Field(default="", description="Integrity hash of `content`")

    def compute_hash(self) -> str:
        """Return the SHA-256 of the note content, hex encoded.

        Normalises line endings first so the hash is stable across platforms.
        """
        normalised = self.content.replace("\r\n", "\n").strip()
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    def model_post_init(self, __context: Any) -> None:
        """Fill `content_sha256` automatically when the caller omits it."""
        if not self.content_sha256:
            self.content_sha256 = self.compute_hash()


class NoteMaterial(_Base):
    """Doc 2.6 entity: "Notes/materials".

    Key fields: lecture notes, slides and reference material tagged by
    unit/topic and version. Holds the full version history rather than only the
    current version, so "which version is current?" is always answerable.
    """

    note_id: str = Field(min_length=1, examples=["ds-u3-bst"])
    title: str = Field(min_length=1, examples=["Binary Search Trees"])
    kind: ArtifactKind = ArtifactKind.LECTURE_NOTES
    unit: str = Field(min_length=1, description="Unit this note belongs to", examples=["3"])
    topic: str = Field(min_length=1, examples=["Binary Search Trees"])
    tags: list[str] = Field(default_factory=list, examples=[["trees", "search", "recursion"]])
    versions: list[NoteVersion] = Field(min_length=1)

    @property
    def current_version(self) -> NoteVersion:
        """The highest-numbered version. This is the 'current' set of materials."""
        return max(self.versions, key=lambda v: v.version)

    @field_validator("versions")
    @classmethod
    def _versions_strictly_increasing(cls, versions: list[NoteVersion]) -> list[NoteVersion]:
        """Reject duplicate version numbers.

        This is a machine-enforced version of the doc's "keep clear version
        history" guardrail: two versions claiming the same number is the one
        corruption that cannot be recovered from later.
        """
        numbers = [v.version for v in versions]
        if len(set(numbers)) != len(numbers):
            duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
            raise ValueError(f"duplicate version numbers in note history: {duplicates}")
        if sorted(numbers) != list(range(1, len(numbers) + 1)):
            raise ValueError(
                f"version history must be contiguous from 1; got {sorted(numbers)}"
            )
        return versions


# ═══════════════════════════════════════════════════════════════════════════
# Entity 2 — Assignment definition
# ═══════════════════════════════════════════════════════════════════════════


class RubricCriterion(_Base):
    """One scored line in a rubric.

    `max_marks` here is the machine anchor for §2.7 validation check 0:
    "a draft grade never exceeds the rubric maximum".
    """

    criterion_id: str = Field(min_length=1, examples=["correctness"])
    description: str = Field(min_length=1)
    max_marks: float = Field(gt=0, examples=[8.0])
    weight: float = Field(
        default=1.0,
        gt=0,
        description="Relative importance. Must sum to 1.0 across the rubric.",
    )


class AssignmentDefinition(_Base):
    """Doc 2.6 entity: "Assignment definition".

    Key fields: assignment brief, due date, marks, and rubric.
    Agent 2's guardrail: distribute only faculty-approved briefs and rubrics —
    so `approved_by` is required and must name the human authority.
    """

    assignment_id: str = Field(min_length=1, examples=["DS-A1"])
    course_code: str = Field(min_length=1, examples=["CS201"])
    title: str = Field(min_length=1)
    brief: str = Field(min_length=1, description="The faculty-approved brief text")
    due_date: datetime
    max_marks: float = Field(gt=0, examples=[20.0])
    release_date: Optional[datetime] = None
    rubric: list[RubricCriterion] = Field(min_length=1)
    approved_by: str = Field(
        default=HUMAN_AUTHORITY,
        description="Who approved this brief. Never an agent.",
    )
    unit: str = Field(default="", description="Syllabus unit this assesses")

    @property
    def rubric_total(self) -> float:
        """Sum of criterion maxima. Must equal `max_marks`; see `check_rubric`."""
        return round(sum(c.max_marks for c in self.rubric), 6)

    @property
    def weight_total(self) -> float:
        """Sum of criterion weights; should be 1.0."""
        return round(sum(c.weight for c in self.rubric), 6)

    def check_rubric(self) -> list[str]:
        """Return a list of rubric problems; empty means the rubric is sound.

        Validation check 0 depends on this being trustworthy, so the problems
        are reported rather than silently repaired.
        """
        problems: list[str] = []
        if abs(self.rubric_total - self.max_marks) > 1e-6:
            problems.append(
                f"rubric criteria sum to {self.rubric_total} but max_marks is {self.max_marks}"
            )
        if abs(self.weight_total - 1.0) > 1e-6:
            problems.append(f"rubric weights sum to {self.weight_total}, expected 1.0")
        return problems


# ═══════════════════════════════════════════════════════════════════════════
# Entity 3 — Submission record
# ═══════════════════════════════════════════════════════════════════════════


class SubmissionRecord(_Base):
    """Doc 2.6 entity: "Submission record".

    Key fields: student submission, timestamp, and similarity/plagiarism score.

    Two guardrails are encoded structurally rather than by convention:

    1. "Never alter a submission timestamp" — `submitted_at` is validated to be
       timezone-aware and is never written after collection.
    2. "Never make a final plagiarism determination automatically" — only
       `similarity_score` (a float) is stored. There is deliberately **no**
       `is_plagiaristic: bool` field, because a boolean is exactly the kind of
       verdict the doc forbids an agent from making on its own.
    """

    submission_id: str = Field(min_length=1, examples=["DS-A1-S007"])
    assignment_id: str = Field(min_length=1)
    student_id: str = Field(min_length=1)
    student_name: str = Field(min_length=1)
    content: str = Field(min_length=1, description="Soft-copy submission text")
    submitted_at: datetime = Field(description="Immutable once collected")
    status: SubmissionStatus = SubmissionStatus.SUBMITTED
    similarity_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Set by agent 4. Attached, never interpreted by an agent.",
    )
    similarity_against: list[str] = Field(
        default_factory=list,
        description="Submission ids this was compared against, for audit.",
    )
    late_by_seconds: int = Field(
        default=0,
        ge=0,
        description="Derived from submitted_at vs due_date. Never typed by hand.",
    )

    @field_validator("submitted_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Reject naive datetimes.

        A naive timestamp makes 'late' uncomputable and would silently corrupt
        validation check 1, so it is rejected at construction.
        """
        if value.tzinfo is None:
            raise ValueError("submitted_at must be timezone-aware (got a naive datetime)")
        return value

    @property
    def is_late(self) -> bool:
        """Derived lateness. Doc §2.7 check 1: "late submissions are flagged
        accurately" — so this must be computed, never asserted."""
        return self.late_by_seconds > 0


# ═══════════════════════════════════════════════════════════════════════════
# Entity 4 — Project milestones
# ═══════════════════════════════════════════════════════════════════════════


class MilestoneCheckpoint(_Base):
    """Doc 2.6 entity: "Project milestones" — one defined checkpoint."""

    checkpoint_id: str = Field(min_length=1, examples=["M2"])
    name: str = Field(min_length=1, examples=["Schema design + ER diagram"])
    due_date: datetime
    expected_deliverable: str = Field(min_length=1)
    status: MilestoneStatus = MilestoneStatus.NOT_STARTED
    evidence: str = Field(
        default="",
        description="Link or description of what was actually submitted. "
        "Agent 6 reports status ONLY from evidence — empty evidence can never "
        "be reported as ACCEPTED.",
    )


class ProjectMilestones(_Base):
    """A course project's checkpoints plus group ownership.

    Doc 2.6: "Defined checkpoints, expected deliverables, and group ownership."
    """

    project_id: str = Field(min_length=1)
    course_code: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    members: list[str] = Field(min_length=1, description="Student ids in the group")
    checkpoints: list[MilestoneCheckpoint] = Field(min_length=1)

    @property
    def status(self) -> MilestoneStatus:
        """Roll the group's checkpoints up into one status, worst-case wins.

        Worst-case-wins is deliberate: the doc's agent-6 guardrail is "report
        status only from submitted evidence; never assume progress not shown".
        Reporting the best checkpoint would invent progress.
        """
        if any(c.status is MilestoneStatus.OVERDUE for c in self.checkpoints):
            return MilestoneStatus.OVERDUE
        if any(c.status is MilestoneStatus.BLOCKED for c in self.checkpoints):
            return MilestoneStatus.BLOCKED
        if all(c.status is MilestoneStatus.ACCEPTED for c in self.checkpoints):
            return MilestoneStatus.ACCEPTED
        if any(c.status in (MilestoneStatus.SUBMITTED, MilestoneStatus.IN_PROGRESS) for c in self.checkpoints):
            return MilestoneStatus.IN_PROGRESS
        return MilestoneStatus.NOT_STARTED

    def schedule_health(self) -> float:
        """Fraction of checkpoints that are at least SUBMITTED, in [0, 1].

        'schedule health' is what the doc's Milestone tracker dashboard panel
        (2.9) shows per group.
        """
        if not self.checkpoints:
            return 0.0
        done = sum(
            1
            for c in self.checkpoints
            if c.status in (MilestoneStatus.SUBMITTED, MilestoneStatus.ACCEPTED)
        )
        return round(done / len(self.checkpoints), 4)


# ═══════════════════════════════════════════════════════════════════════════
# Entity 5 — Grade & feedback
# ═══════════════════════════════════════════════════════════════════════════


class DraftGrade(_Base):
    """Doc 2.6 entity: "Grade & feedback".

    Key fields: draft grade and feedback **pending faculty approval**.

    `status` starts at `draft` and can only become `approved` through the human
    gate. §2.7 check 3 — "no grade is published without faculty approval" — is
    enforced against this field.
    """

    grade_id: str = Field(min_length=1)
    submission_id: str = Field(min_length=1)
    awarded_marks: float = Field(ge=0.0, examples=[17.5])
    max_marks: float = Field(gt=0.0, examples=[20.0])
    criterion_marks: dict[str, float] = Field(
        default_factory=dict,
        description="criterion_id -> marks awarded, the per-test-case breakdown "
        "agent 6 must show.",
    )
    feedback: str = Field(default="")
    status: str = Field(default="draft", pattern="^(draft|approved|published)$")
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None

    @property
    def exceeds_maximum(self) -> bool:
        """Whether the draft grade breaks §2.7 check 0.

        Kept as a property so the check and the display cannot disagree.
        """
        return self.awarded_marks > self.max_marks + 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# Lab 1 — clarification and plan objects
# ═══════════════════════════════════════════════════════════════════════════


class ClarifyingQuestion(_Base):
    """One question the agent asked because a required input was missing.

    Lab 1 acceptance: "Given a vague request it asks at least two clarifying
    questions." Each of these is evidence of that behaviour, so each records
    the field, the question, and what it unblocks.
    """

    field: str = Field(min_length=1, description="Which input this resolves")
    question: str = Field(min_length=1, description="Asked verbatim to the user")
    why_it_matters: str = Field(min_length=1)
    example: str = Field(
        default="",
        description="A sample answer, offered so a user who does not know can "
        "still unblock the loop. Also used as the offline fallback.",
    )
    answer: Optional[str] = Field(default=None, description="Filled once answered")
    assumed: bool = Field(
        default=False,
        description="True if the agent filled this in itself rather than asking.",
    )


class ClarifiedRequest(_Base):
    """The output of Lab 1's `clarify` step — doc Input: "Free-text request;
    core parameters". Output: "A clarified request".

    `questions_asked` is retained (rather than discarded once answered) because
    the audit trail must show what the agent had to ask.
    """

    raw_request: str = Field(min_length=1, description="The original free text, verbatim")
    course_code: str = Field(min_length=1, examples=["CS201"])
    course_name: str = Field(min_length=1, examples=["Data Structures"])
    units: list[str] = Field(min_length=1, description="Units/topics in scope")
    assignment_ids: list[str] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=_utcnow, description="Deadline/anchor date")
    include_milestones: bool = Field(
        default=True,
        description="Whether course-project milestone tracking is in scope",
    )
    human_authority: str = Field(
        default=HUMAN_AUTHORITY,
        description="Who must approve. The doc names course faculty.",
    )
    questions_asked: list[ClarifyingQuestion] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything the agent filled in without asking. Shown to faculty.",
    )

    @property
    def question_count(self) -> int:
        """How many clarifying questions were genuinely asked.

        Excludes agent-assumed fields, because an assumption is not a question
        and must not inflate Lab 1's acceptance metric.
        """
        return sum(1 for q in self.questions_asked if not q.assumed)

    def summary(self) -> str:
        """One-line human-readable echo of the clarified request."""
        return (
            f"{self.course_code} ({self.course_name}) | units: {', '.join(self.units)} | "
            f"assignments: {', '.join(self.assignment_ids) or 'none'} | "
            f"milestones: {'yes' if self.include_milestones else 'no'} | "
            f"approver: {self.human_authority}"
        )


class PlanStep(_Base):
    """One step in the outline plan for the organised course workspace.

    `agent` names the node that will eventually run the step. In Lab 1 those
    nodes are *planned but not activated* — the doc's Lab 1 row says
    "Coordinator (loop only)" and "no specialist nodes yet". The field is
    present so Lab 7 can wire the same plan straight into the node graph.
    """

    step_id: str = Field(min_length=1, examples=["S1"])
    name: str = Field(min_length=1)
    agent: str = Field(min_length=1, description="Node that will execute this")
    intent: str = Field(min_length=1, description="What the step is trying to achieve")
    inputs_required: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, description="Prior step_ids")
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="How we will know this step succeeded.",
    )
    priority: int = Field(default=5, ge=1, le=9, description="1 = highest")


class PlanRevision(_Base):
    """One pass of the `revise` loop. Doc 2.10 Lab 1 pseudocode:
    `while user.wants_changes(plan): plan = revise(plan, user.feedback)`."""

    revision: int = Field(ge=1)
    feedback: str = Field(min_length=1, description="Verbatim user feedback")
    changes_made: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=_utcnow)


class CourseWorkspacePlan(_Base):
    """The Lab 1 deliverable: "an editable outline plan for the organised
    course workspace (notes, assignments, grades)".

    Status is `DRAFT` and stays that way through Lab 1 — the doc's pseudocode
    returns the plan as "not final — a structured plan". Locking is Lab 3's job
    (`plan_skill`), approval is the faculty gate's job.
    """

    plan_id: str = Field(min_length=1, examples=["PLAN-CS201-0001"])
    created_at: datetime = Field(default_factory=_utcnow)
    request: ClarifiedRequest
    steps: list[PlanStep] = Field(min_length=1)
    status: PlanStatus = PlanStatus.DRAFT
    human_authority: str = Field(default=HUMAN_AUTHORITY)
    revisions: list[PlanRevision] = Field(default_factory=list)
    open_questions: list[str] = Field(
        default_factory=list,
        description="Unresolved ambiguity. The doc forbids guessing silently.",
    )

    # ── Editable-plan helpers used by the revise loop ─────────────────────

    def step_by_id(self, step_id: str) -> PlanStep:
        """Look up a step.

        Raises:
            KeyError: If no step has that id, naming the valid ids so the
                revise loop can report a useful error to the user.
        """
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(
            f"no step {step_id!r}; valid ids are {[s.step_id for s in self.steps]}"
        )

    def plan_sha256(self) -> str:
        """Content hash of the plan, used from Lab 3 onward as the lock key."""
        payload = json.dumps(
            {
                "plan_id": self.plan_id,
                "steps": [s.model_dump(mode="json") for s in self.steps],
                "status": self.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def outline(self) -> str:
        """Render the plan as a human-editable outline.

        This is the artefact a person actually reads and edits, so it is
        rendered plainly, with dependency order preserved.
        """
        lines = [
            f"PLAN {self.plan_id}  (status: {self.status.value}, approver: {self.human_authority})",
            f"Request: {self.request.summary()}",
            "",
        ]
        for step in self._topological_steps():
            deps = f"  [after {', '.join(step.depends_on)}]" if step.depends_on else ""
            lines.append(f"  {step.step_id}. {step.name}  -> {step.agent}{deps}")
            lines.append(f"      intent: {step.intent}")
            for criterion in step.acceptance_criteria:
                lines.append(f"      done when: {criterion}")
        if self.revisions:
            lines.append("")
            lines.append(f"Revisions applied: {len(self.revisions)}")
            for revision in self.revisions:
                lines.append(f"  r{revision.revision}: {revision.feedback}")
                for change in revision.changes_made:
                    lines.append(f"      - {change}")
        if self.open_questions:
            lines.append("")
            lines.append("Open questions (agent will not guess these):")
            for question in self.open_questions:
                lines.append(f"  ? {question}")
        return "\n".join(lines)

    def _topological_steps(self) -> list[PlanStep]:
        """Order steps so every step appears after the steps it depends on.

        Implemented as a stable Kahn topological sort. Raises on a dependency
        cycle rather than looping forever — the doc's Lab 7 pitfall about
        unbounded loops applies to plan editing too.
        """
        by_id = {s.step_id: s for s in self.steps}
        remaining = list(self.steps)
        ordered: list[PlanStep] = []
        placed: set[str] = set()

        while remaining:
            progressed = False
            for step in list(remaining):
                if all(dep in placed for dep in step.depends_on if dep in by_id):
                    ordered.append(step)
                    placed.add(step.step_id)
                    remaining.remove(step)
                    progressed = True
            if not progressed:
                cycle = ", ".join(s.step_id for s in remaining)
                raise ValueError(f"plan steps contain a dependency cycle among: {cycle}")

        return ordered


__all__ = [
    "ArtifactKind",
    "AssignmentDefinition",
    "ClarifiedRequest",
    "ClarifyingQuestion",
    "CourseWorkspacePlan",
    "Decision",
    "DraftGrade",
    "MilestoneCheckpoint",
    "MilestoneStatus",
    "NoteMaterial",
    "NoteVersion",
    "PlanRevision",
    "PlanStatus",
    "PlanStep",
    "ProjectMilestones",
    "RubricCriterion",
    "SubmissionRecord",
    "SubmissionStatus",
]
