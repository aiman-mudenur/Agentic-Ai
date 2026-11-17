"""The graph's shared state: one typed contract every node reads and writes.

Doc 2.5 (line 530) describes what flows between agents as *\"state / partial
result\"*. That phrase is the whole design constraint of this module: a node does
not receive what the previous node returned, it receives **the accumulated
state**. LangGraph's state channel is what makes the distinction concrete -- each
node returns a *partial update* that is merged into one shared dict, and the
merge rule for each key is the reducer declared here.

Two reducers, and they are not interchangeable:

``LastValue``
    The default. Whatever a node returns replaces what was there. Correct for
    ``draft``, which is wholly rebuilt each pass.

``append``
    Accumulates. Required for ``attempt_log`` and ``audit_trail``, where losing an
    earlier entry would erase the evidence that the back-edge ran at all. If this
    were LastValue, a retried run would look exactly like a first attempt, and
    the doc's *"Bound the retry edge"* requirement could not be *demonstrated*,
    only asserted.

Doc 2.6's Data Model (lines 543-548) names five entities. They are carried here
as the doc names them, not as whatever the tools happen to return:

===========================  ============================================
Entity                       Where it lands in this state
===========================  ============================================
Notes/materials              ``notes_by_unit`` + ``version_total``
Assignment definition        ``assignments``
Submission record            ``submissions`` (+ derived ``late_flags``)
Project milestones           ``milestones``
Grade & feedback             ``draft_grades`` (all ``draft`` until approved)
===========================  ============================================

The one field this module refuses to guess is ``status``. It is a
:class:`CourseStatus` -- an enum, not a string -- because the doc's central
promise (line 455) is *"agents draft and flag; a named human approves"*, and a
bare ``status="published"`` in a state dict is exactly the kind of thing that
gets set without anybody approving anything.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, Callable, Optional, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The append reducer. Named because "annotated with a function" reads as magic
#: until you know which function and why.
append: Callable[[list[Any], list[Any]], list[Any]] = operator.add


class CourseStatus(str, Enum):
    """Where the run got to. Doc 2.10 Lab 7's outcome vocabulary.

    ``draft`` is the doc's word (line 690: *\"...draft end to end\"*) and is the
    status a run has the moment validation passes -- before any human has seen
    it. ``awaiting_faculty`` means the graph suspended at the gate.
    ``published`` requires a faculty decision that was recorded, because doc
    2.12 (line 830) forbids publishing a grade without approval.
    """

    DRAFT = "draft"
    AWAITING_FACULTY = "awaiting_faculty"
    PUBLISHED = "published"
    REFUSED = "refused"
    ESCALATED = "escalated"


class ValidationOutcome(BaseModel):
    """Doc 2.7's ValidationAgent report (line 558), in full.

    The doc computes ``passed = not failing`` and attaches
    ``diagnosis = explain(failing)``. Both are here, and ``explain`` is not a
    placeholder: :func:`course_organizer.graph.validation.explain_failure` has to
    name *which* check failed and why, because Lab 7's whole deliverable is that
    a failure produces a branch and a diagnosis rather than a retry that never
    ends.
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    failing: list[str] = Field(default_factory=list)
    diagnosis: str = ""
    checks_run: list[str] = Field(default_factory=list)
    #: Attempt number this report came from, so a diagnosis can say "third try".
    attempt: int = 0


class DraftGradeRow(BaseModel):
    """One draft grade. Doc 2.6's *"Grade & feedback ... pending faculty approval"*.

    ``status`` starts at ``draft`` and is the only thing the gate changes. A
    published grade carries ``approved_by``; a draft that somehow reached publish
    without one is the bug doc 2.12's first guardrail exists to prevent, so it is
    checkable rather than merely intended.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str
    student_name: str = ""
    assignment_id: str = ""
    awarded_marks: float = 0.0
    max_marks: float = 0.0
    criterion_marks: dict[str, float] = Field(default_factory=dict)
    feedback: str = ""
    similarity_score: Optional[float] = None
    #: The submission this one most overlaps, when a counterparty exists. What
    #: makes a similarity flag actionable: the reviewer is told who to compare
    #: against rather than only being told a number was high.
    similarity_against: str = ""
    similarity_flagged: bool = False
    late: bool = False
    status: str = "draft"
    approved_by: str = ""
    approved_at: str = ""


#: Doc 2.5 line 538 spells the gate's return as ``approve | edit | override``.
#: Module-level rather than class attributes because pydantic treats an
#: unannotated class attribute as a field, and a *field* called ``APPROVE`` on a
#: decision object is a value someone could set by accident.
DECISION_APPROVE = "approve"
DECISION_EDIT = "edit"
DECISION_OVERRIDE = "override"
DECISION_REFUSE = "refuse"

#: Every outcome that authorises publication. ``refuse`` is the only one that
#: does not, and it is the only one that routes to escalation.
APPROVING_DECISIONS: tuple[str, ...] = (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
)


class FacultyDecision(BaseModel):
    """Doc 2.5 (line 538): the gate returns ``approve | edit | override``.

    Three outcomes, not two, because a human who wants to change a grade is not
    refusing the run and a system that treats *\"approve with edits\"* as a
    rejection will train people to click the wrong button.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = DECISION_APPROVE
    decided_by: str = "course_faculty"
    reason: str = ""
    #: For ``edit``: replacement awarded marks, keyed by submission id.
    edits: dict[str, float] = Field(default_factory=dict)
    #: For ``override``: marks the faculty supplied, used verbatim.
    override_marks: dict[str, float] = Field(default_factory=dict)

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        """Reject any answer the doc does not define.

        Without this, a typo like ``"Approve"`` or ``"approved"`` would make
        ``approved`` False and the run would escalate as a *refusal* -- the
        reviewer asked to publish and the system recorded that they declined.
        Failing loudly is the better of the two behaviours: a caller has to fix
        its own vocabulary rather than discover months later that half the
        approvals never happened.
        """
        normalised = str(value).strip().lower()
        allowed = (*APPROVING_DECISIONS, DECISION_REFUSE)
        if normalised not in allowed:
            raise ValueError(
                f"unknown faculty decision {value!r}; the gate answers with one "
                f"of: {', '.join(allowed)}"
            )
        return normalised

    @property
    def approved(self) -> bool:
        """Did faculty authorise publication?

        Every outcome except a refusal. ``edit`` and ``override`` both mean *yes,
        with these marks* -- collapsing them into ``refused`` is how a review
        queue ends up with reviewers who stop reviewing carefully.
        """
        return self.decision in (DECISION_APPROVE, DECISION_EDIT, DECISION_OVERRIDE)


class Escalation(BaseModel):
    """Doc 2.12 line 825: *\"on exhaustion, escalate to course faculty with a diagnosis.\"*

    Lab 7 reaches this node for two distinct reasons -- a retry budget spent, and
    a faculty refusal -- and both are modelled by one type because both mean the
    same thing to whoever picks the run up: **a person has to decide, and here is
    why the system stopped.** Collapsing them would force a caller to re-derive
    the reason from a flag.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    diagnosis: str
    raised_by: str
    attempts: int = 0
    max_attempts: int = 0
    failing: list[str] = Field(default_factory=list)
    escalated_to: str = "course_faculty"
    #: The draft as it stood when the run stopped, so the human sees the work
    #: rather than having to re-run to reproduce it.
    pending_draft: dict[str, Any] = Field(default_factory=dict)


class GateRequest(BaseModel):
    """What the gate actually asks. Built *before* the interrupt.

    Doc 2.11's dashboard (line 594) says faculty review shows *"Rubric-aligned
    draft grades and feedback awaiting faculty approval"*, so this carries the
    grades, the validation report and the similarity flags -- the three things a
    reviewer needs and cannot get without re-running the pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    course_code: str
    course_name: str = ""
    draft_grades: list[DraftGradeRow] = Field(default_factory=list)
    validation: ValidationOutcome = Field(default_factory=ValidationOutcome)
    similarity_flags: list[dict[str, Any]] = Field(default_factory=list)
    units_covered: list[str] = Field(default_factory=list)
    assignments_included: list[str] = Field(default_factory=list)
    attempt: int = 0
    #: Lab 14: which domain specialist produced the workspace, and whether it
    #: succeeded. Carried to the reviewer because approving a run whose
    #: specialist failed is a decision only a named human may make -- but only
    #: if they are told, rather than shown a clean-looking document.
    domain: str = "theory"
    specialist_ok: bool = True
    domain_error: bool = False
    summary: str = ""


class CourseState(TypedDict, total=False):
    """The one object that travels through the graph.

    Every key is optional because LangGraph seeds a fresh run with whatever the
    caller passes; ``total=False`` is what lets ``graph.invoke({})`` start.

    The reducer on a key decides whether a node's return *replaces* or
    *accumulates*. Getting that wrong on ``attempt_log`` would erase the evidence
    that the retry edge ever fired, which is the property Lab 7 exists to prove.
    """

    # -- identity ---------------------------------------------------------
    course_code: str
    course_name: str
    run_id: str
    human_authority: str

    # -- doc 2.6 entities ------------------------------------------------
    notes_by_unit: dict[str, Any]
    version_total: int
    assignments: list[dict[str, Any]]
    submissions: list[dict[str, Any]]
    milestones: list[dict[str, Any]]
    draft_grades: list[DraftGradeRow]

    # -- Lab 9/10: grounding and repair evidence --------------------------
    #: Submission id -> evidence-table dump, built by ``workers`` from the
    #: records the run actually read. Declared because LangGraph **silently
    #: discards** any key a node returns that this TypedDict does not declare:
    #: an undeclared table would leave every grounding check failing closed on
    #: the next pass, which looks exactly like ungrounded work.
    evidence_tables: dict[str, Any]
    #: What the last validation pass found, as ``{"check": name}`` entries, so
    #: the repair step and the audit trail read one list instead of
    #: re-deriving the same facts from the diagnosis prose.
    repair_findings: list[dict[str, Any]]

    # -- the plan (doc line 455: locked before work begins) -------------
    #: The ``LockedPlan`` from the ``plan`` skill, as a plain dict so it
    #: checkpoints cleanly.
    plan_locked: dict[str, Any]
    #: Plan integrity, copied out of the lock so a later node can assert it
    #: rather than trust that nobody changed it.
    plan_sha256: str
    obligation_count: int
    #: What ``plan`` was asked to do, so a test can assert the node received
    #: the state it was given rather than reaching for globals.
    plan_inputs: dict[str, Any]

    # -- the assembled document -----------------------------------------
    #: The ``Document`` from ``format_organised``, as a dict.
    document: dict[str, Any]
    document_id: str
    document_status: str
    #: The rendered markdown. Kept separately because it is what gets written
    #: at publish and what the reviewer reads at the gate.
    document_body: str
    #: Deviations from the locked plan the formatter detected. Doc 2.11's
    #: dashboard shows drift to faculty, so these are surfaced, not hidden.
    drift_findings: list[dict[str, Any]]
    published_path: str

    # -- Lab 14: the domain specialist ------------------------------------
    #: Which domain specialist assembled this workspace ("theory", "lab",
    #: "project"). Declared so the value the caller seeded survives the run and
    #: ``assemble`` does not silently fall back to the default.
    domain: str
    #: Whether that specialist succeeded. ``False`` blocks publication even
    #: with a faculty approval: the reviewer would otherwise be approving a
    #: workspace the domain expertise never actually shaped.
    specialist_ok: bool
    #: True when the domain was unknown or the specialist raised, as opposed to
    #: ran and reported a problem. The two are different failures and the
    #: reviewer is told which.
    domain_error: bool
    specialist_failed_reason: str
    specialist_error: str
    #: One entry per specialist run, appended so a retried run keeps the
    #: earlier attempt's outcome rather than overwriting the evidence.
    specialist_audit: Annotated[list[dict[str, Any]], append]

    # -- derived ----------------------------------------------------------
    late_flags: list[dict[str, Any]]
    similarity_flags: list[dict[str, Any]]
    units_covered: list[str]
    units_requested: list[str]
    #: Score per submission id, from the similarity specialist.
    similarity_scores: dict[str, float]
    #: Which submission each score is against. Declared separately from the
    #: flag because it is the answer to "compare with whom" and is needed even
    #: for a pair that scored below the threshold.
    similarity_against: dict[str, str]
    #: How many submissions the store says are late. Carried because the gate
    #: request shows a late count and it has to come from the store rather than
    #: be recounted from the flags.
    reported_late_count: int
    #: Which of the scope tool's checks ``ingest`` ran and passed. Recorded
    #: rather than assumed, so "the run started from a verified scope" is a
    #: fact in the state and not a claim in a docstring.
    scope_checks_run: list[str]
    #: The scope tool's ``verified_facts``, carried from ``ingest`` into
    #: ``plan``. It has to be a declared channel: LangGraph **silently discards**
    #: any key a node returns that the state TypedDict does not declare, which
    #: would leave the plan locked against an empty scope and make every
    #: distributed assignment look out of scope to the drift check.
    scope_verified_facts: dict[str, Any]
    #: The assignment ids the scope tool found. Same reason as above.
    scope_assignment_ids: list[str]
    #: An already-verified locked plan dump the run must execute under, when
    #: the caller supplied one. Declared so ``plan`` receives the seed instead
    #: of having LangGraph discard it and lock a different scope.
    authorized_plan: dict[str, Any]

    # -- the retry edge (doc line 696) -------------------------------------
    #: Incremented by ``validate`` on every pass. **The bound the doc asks for.**
    #: Plain LastValue, deliberately: ``validate`` returns the whole new number
    #: (``attempts + 1``), so a reducer that added would double-count the one
    #: key whose value has to be exactly right when a budget is spent.
    attempts: int
    #: Appended to on every pass -- the visible proof the back-edge ran.
    attempt_log: Annotated[list[dict[str, Any]], append]
    #: Which attempt this failure only appears on; the injection point for the
    #: regeneration demonstration. 0 means "fails every time".
    fail_until_attempt: int
    validation: ValidationOutcome
    route: str

    # -- the human gate (doc line 538) ------------------------------------
    gate_request: GateRequest
    decision: FacultyDecision
    approved_at: str

    # -- escalation (doc line 825) ----------------------------------------
    escalation: Escalation

    # -- output -----------------------------------------------------------
    status: CourseStatus
    #: Appended by every node, so the audit trail survives the whole run and not
    #: just the last pass -- which is the only way to see the back-edge's work.
    audit_trail: Annotated[list[dict[str, Any]], append]
    #: One line per node that ran, in order. Lets a test assert that a resumed
    #: run did not re-run the nodes before the gate.
    nodes_ran: Annotated[list[str], append]


#: Doc 2.7's four checks (line 558), in the doc's order. The order is the doc's
#: because the first one is the one the KPI in line 496 is written around.
DOC_CHECKS: tuple[str, ...] = (
    "draft_grade_within_rubric_maximum",
    "late_submissions_flagged_accurately",
    "similarity_score_attached",
    "no_grade_published_without_faculty_approval",
    #: Lab 9's grounding check, fifth because it is the one that answers
    #: "is this draft supported by a real source?" rather than "is the number
    #: in range?". It was implemented in ``validation.CHECKS`` but left out of
    #: this tuple, so it never ran: a check that is not in the doc's list is a
    #: comment, not a gate.
    "evidence_grounded",
)

#: Doc 2.10 Lab 7's node names, verbatim from its pseudocode (line 696).
DOC_NODE_CHAIN: tuple[str, ...] = (
    "ingest", "plan", "workers", "validate", "assemble", "gate", "publish",
)

#: The retry edge the doc names: ``validate --fail--> workers``.
DOC_BACK_EDGE: tuple[str, str] = ("validate", "workers")

#: Doc 2.12 line 825 names ``MAX_ATTEMPTS``; Lab 7 keeps the name and the value
#: small enough to be exercised end to end in a test.
MAX_ATTEMPTS = 3

#: The branch labels ``validate`` returns. Named after the doc's own ``--fail-->``
#: and ``--pass-->`` so the topology reads against the pseudocode.
ROUTE_RETRY = "fail"
ROUTE_PASS = "pass"
ROUTE_ESCALATE = "escalate"
ROUTE_APPROVED = "approved"
ROUTE_REFUSED = "refused"

__all__ = [
    "APPROVING_DECISIONS",
    "DECISION_APPROVE",
    "DECISION_EDIT",
    "DECISION_OVERRIDE",
    "DECISION_REFUSE",
    "DOC_BACK_EDGE",
    "DOC_CHECKS",
    "DOC_NODE_CHAIN",
    "MAX_ATTEMPTS",
    "ROUTE_APPROVED",
    "ROUTE_ESCALATE",
    "ROUTE_PASS",
    "ROUTE_REFUSED",
    "ROUTE_RETRY",
    "CourseState",
    "CourseStatus",
    "DraftGradeRow",
    "Escalation",
    "FacultyDecision",
    "GateRequest",
    "ValidationOutcome",
    "append",
]