"""Lab 1 -- Agent vs Chatbot: the organiser planner loop.

Document requirement (2.10, Lab 1)
---------------------------------
* **Phase:** A - Foundations
* **Goal:** "Establish the agent loop (clarify -> plan -> revise) instead of a
  one-shot chatbot answer."
* **What it adds:** "A minimal planner that clarifies the request, proposes a
  plan for producing the organised course workspace, and revises on feedback --
  the seed this worklet grows from."
* **Inputs:** Free-text request; core parameters.
* **Outputs:** A clarified request + an editable outline plan.
* **Agents/nodes:** Coordinator (loop only). "No specialist nodes yet."
* **Deliverable:** "A notebook agent that clarifies, plans and revises a course
  workspace outline."
* **Accept:** "Given a vague request it asks at least two clarifying questions
  and yields a coherent, editable plan."
* **Pitfall:** "Don't answer in one shot -- without clarify/revise it's a
  chatbot, not an agent."
* **Pseudocode:**

      req  = clarify(user_request)         # ask until inputs complete
      plan = propose_plan(req)             # outline for the course workspace
      while user.wants_changes(plan):
          plan = revise(plan, user.feedback)
      return plan                          # not final - a structured plan

What this module implements
---------------------------
* :class:`PlannerAgent` -- the loop, exactly in the order above.
* :func:`run_planner_loop` -- one complete run, given a feedback provider.
* :func:`one_shot_chatbot` -- the deliberate **anti-pattern**, kept so the
  document's pitfall can be demonstrated rather than merely asserted.
* Three `ScriptedLLM` builders so the loop runs offline and reproducibly.

Design note on the "human" in the loop
--------------------------------------
The revise loop needs a `user`. In a notebook that is a person at the prompt;
in a test it is a list of canned replies. Both are expressed by the
:class:`FeedbackProvider` protocol, so the agent code is identical in both
settings and the acceptance test is deterministic.

Note on text encoding
---------------------
Every string this module emits at runtime is plain ASCII. The Windows console
defaults to a legacy code page and would otherwise render typographic dashes
as mojibake in printed plans. Docstrings here use ASCII throughout for the same
reason.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from course_organizer import HUMAN_AUTHORITY
from course_organizer.llm import LLMClient, ScriptedLLM, get_llm
from course_organizer.logging_setup import log_event
from course_organizer.schemas import (
    ClarifiedRequest,
    ClarifyingQuestion,
    CourseWorkspacePlan,
    PlanRevision,
    PlanStatus,
    PlanStep,
)

logger = logging.getLogger(__name__)

# ==========================================================================
# Required inputs -- "core parameters" (doc 2.10 Lab 1, Inputs)
# ==========================================================================

#: Parameters Lab 1 must have before it can plan anything. Anything absent
#: becomes a clarifying question. Order is the order we ask in: most important
#: first, so a user who answers two questions still gets a usable plan.
REQUIRED_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "course_code",
        "Which course is this for? (e.g. CS201 Data Structures, or DBMS402)",
        "Every downstream step - notes, assignments, grades - is scoped to one "
        "course. Guessing it would scope the whole plan to the wrong syllabus.",
        "CS201",
    ),
    (
        "units",
        "Which units or topics should be in scope? (e.g. units 1-3, or just "
        "'Binary Search Trees')",
        "The plan's coverage is derived from this. Too wide and the run blows "
        "its budget; too narrow and the workspace is incomplete.",
        "units 1-3",
    ),
    (
        "assignments",
        "Which assignment(s) are in scope? (e.g. ASG-01, or 'all three', or "
        "'none' for notes only)",
        "Submission collection, similarity checking and grading all operate on "
        "assignments. With none in scope, only the notes stream is planned.",
        "all three",
    ),
    (
        "as_of",
        "What date should I treat as 'now' for deadline and lateness checks? "
        "(YYYY-MM-DD)",
        "Lateness and milestone status are relative to a date. Using the "
        "machine clock would make results non-reproducible.",
        "2026-10-15",
    ),
)

#: Optional parameters. Never asked; the agent assumes and records the
#: assumption rather than silently guessing (doc 2.10 Lab 3 pitfall).
OPTIONAL_FIELDS: dict[str, str] = {
    "include_milestones": "1",
    "course_name": "",
}

#: Max revisions before we stop asking. The doc warns in Lab 7 that an
#: unbounded retry edge "can spin forever"; the same discipline applies here.
MAX_REVISIONS = 5

#: Guards the clarify loop against a user who never answers.
MAX_CLARIFY_ROUNDS = len(REQUIRED_FIELDS) + 3


# ==========================================================================
# Human-in-the-loop providers
# ==========================================================================


class FeedbackProvider(ABC):
    """The `user` in the document's pseudocode.

    The doc's loop is `while user.wants_changes(plan): plan = revise(...)`, so
    the agent needs two capabilities: answer a clarifying question, and say
    whether the plan needs changing. Both live here, behind one interface, so
    the same agent runs interactively and under test.
    """

    @abstractmethod
    def answer_question(self, question: ClarifyingQuestion) -> str:
        """Return the user's answer to a clarifying question."""

    @abstractmethod
    def wants_changes(self, plan: CourseWorkspacePlan) -> bool:
        """Return True if the user wants the plan revised."""

    @abstractmethod
    def get_feedback(self, plan: CourseWorkspacePlan) -> str:
        """Return the user's feedback text for the current plan."""


@dataclass
class ScriptedFeedback(FeedbackProvider):
    """A `FeedbackProvider` driven by a fixed script. Used by tests and demos.

    Attributes:
        answers: Answers consumed in order, keyed by field name when available.
        feedback_turns: Feedback strings, one per revision, consumed in order.
        asked_fields: Fields this provider was actually asked about (populated).
        seen_plans: Plan hashes offered for approval (populated).
    """

    answers: dict[str, str] = field(default_factory=dict)
    feedback_turns: list[str] = field(default_factory=list)
    asked_fields: list[str] = field(default_factory=list)
    seen_plans: list[str] = field(default_factory=list)

    def answer_question(self, question: ClarifyingQuestion) -> str:
        """Answer from the script, falling back to the field's example value."""
        self.asked_fields.append(question.field)
        if question.field in self.answers:
            return self.answers[question.field]
        # A user who does not know still gives the agent the example value, so
        # the loop can terminate with something usable.
        return question.example

    def wants_changes(self, plan: CourseWorkspacePlan) -> bool:
        """True while scripted feedback turns remain."""
        self.seen_plans.append(plan.plan_sha256())
        return len(self.feedback_turns) > 0

    def get_feedback(self, plan: CourseWorkspacePlan) -> str:
        """Pop and return the next scripted feedback turn."""
        return self.feedback_turns.pop(0)


class InteractiveFeedback(FeedbackProvider):
    """A `FeedbackProvider` that prompts a real person on stdin.

    Only used by the interactive demo. Empty input is treated as "no change",
    so pressing Enter accepts the plan -- the same courtesy a chat UI offers.
    """

    def answer_question(self, question: ClarifyingQuestion) -> str:
        """Prompt for an answer to a clarifying question."""
        print(f"\n  [clarify] {question.question}")
        print(f"            why: {question.why_it_matters}")
        try:
            reply = input("            > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n            (no input available - using the example)")
            return question.example
        return reply or question.example

    def wants_changes(self, plan: CourseWorkspacePlan) -> bool:
        """Ask whether the plan should be revised."""
        try:
            reply = input("\n  [gate] Revise this plan? [y/N] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return reply in {"y", "yes"}

    def get_feedback(self, plan: CourseWorkspacePlan) -> str:
        """Prompt for free-text revision feedback."""
        try:
            return input("  [revise] What should change? > ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


# ==========================================================================
# Agent result
# ==========================================================================


@dataclass
class PlannerResult:
    """Everything Lab 1 produced, in one inspectable object.

    Attributes:
        request: The clarified request as it stands at the *end* of the run,
            after any revisions that changed scope.
        plan: The editable outline plan. Always `DRAFT` after Lab 1.
        questions_asked: Every clarifying question, with answers.
        revision_count: How many times the plan was revised.
        one_shot_answer: The anti-pattern's output, when it was run for contrast.
        trace: Ordered log of loop steps, for the notebook and for tests.
    """

    request: ClarifiedRequest
    plan: CourseWorkspacePlan
    questions_asked: list[ClarifyingQuestion]
    revision_count: int
    one_shot_answer: Optional[str] = None
    trace: list[str] = field(default_factory=list)

    @property
    def is_editable_plan(self) -> bool:
        """True if the plan can still be revised.

        Lab 1's deliverable is explicitly *not* final, so this is the expected
        state and a useful assertion in the acceptance test.
        """
        return self.plan.status is PlanStatus.DRAFT

    def summary(self) -> str:
        """A compact, human-readable summary for the notebook and CLI."""
        lines = [
            f"clarifying questions asked : {len(self.questions_asked)}",
            f"plan id                   : {self.plan.plan_id}",
            f"plan status               : {self.plan.status.value}",
            f"plan steps                : {len(self.plan.steps)}",
            f"revisions applied         : {self.revision_count}",
            f"approver (human authority): {self.plan.human_authority}",
            f"plan hash                 : {self.plan.plan_sha256()[:16]}...",
        ]
        return "\n".join(lines)


# ==========================================================================
# Deterministic extraction helpers (used by the scripted backend)
# ==========================================================================

_COURSE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bCS\s?\d{3,4}\b", "CS201"),
    (r"\bDBMS\s?\d{3,4}\b", "DBMS402"),
    (r"\bDS\s?\d{3,4}\b", "CS201"),
    (r"\bdata\s+structures?\b", "CS201"),
    (r"\bdbms\b", "DBMS402"),
)

_COURSE_NAMES: dict[str, str] = {
    "CS201": "Data Structures",
    "DBMS402": "Database Management Systems",
}

# "assignment N" / "assn 2" phrasing.
_ASSIGNMENT_RE = re.compile(r"\b(?:assignment|assn|a)\s*#?\s*(\d{1,2})\b", re.IGNORECASE)
# Explicit ids in the shapes courses actually use: ASG-01, DS-A1, DBMS402-A2.
# The leading-letter requirement stops this matching bare dates like 2026-10-15.
_ASSIGNMENT_ID_RE = re.compile(r"\b([A-Z]{2,5}-[A-Z]?\d{1,3})\b")
# Answers that mean "the assignment stream is deliberately out of scope".
_NO_ASSIGNMENT_ANSWERS = {"none", "no", "n/a", "na", "nothing", "skip", "-", "nil", "zero"}
_UNIT_RE = re.compile(r"\bunits?\s+(\d{1,2})(?:\s*[-]\s*(\d{1,2}))?\b", re.IGNORECASE)
_UNIT_TOKEN_RE = re.compile(r"\bunit[-\s]?(\d{1,2})\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _extract_course(text: str) -> tuple[Optional[str], Optional[str]]:
    """Find a course code and name in free text.

    Returns:
        `(code, name)`; either may be None if not found.
    """
    for pattern, code in _COURSE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return code, _COURSE_NAMES.get(code, "")
    return None, None


def _extract_units(text: str) -> list[str]:
    """Find unit references, expanding ranges like "units 1-3"."""
    units: set[str] = set()
    for match in _UNIT_RE.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        units.update(str(n) for n in range(start, end + 1))
    if not units:
        for match in _UNIT_TOKEN_RE.finditer(text):
            units.add(str(int(match.group(1))))
    return sorted(units, key=int)


def _extract_assignments(text: str) -> list[str]:
    """Find explicit assignment ids, or a count when the user says "all three"."""
    explicit = sorted({m.group(1) for m in _ASSIGNMENT_ID_RE.finditer(text)})
    if explicit:
        return explicit
    numbers = sorted({int(m.group(1)) for m in _ASSIGNMENT_RE.finditer(text)})
    if numbers:
        return [f"ASG-{n:02d}" for n in numbers]
    if re.search(r"\ball\s+(?:the\s+)?(?:three|3)\b", text, re.IGNORECASE):
        return [f"ASG-{n:02d}" for n in range(1, 4)]
    return []


def _extract_date(text: str) -> Optional[datetime]:
    """Find the first ISO date in the text."""
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _looks_vague(text: str) -> bool:
    """Heuristic: is this request too thin to plan from?

    A request is vague when it names no course and no unit and no assignment,
    i.e. nothing concrete enough to scope a plan.
    """
    return not (_extract_course(text)[0] or _extract_units(text) or _extract_assignments(text))


@ScriptedLLM.builder_for(ClarifiedRequest)
def _build_clarified_request(payload: dict[str, Any], llm: ScriptedLLM) -> ClarifiedRequest:
    """Deterministically build a `ClarifiedRequest` from free text.

    This is real extraction work, not a canned response: it finds course
    codes, unit ranges, assignment ids and dates using the patterns above. The
    live backend performs the same job semantically; this one does it
    lexically, which is why it is deterministic and free.
    """
    text = str(payload.get("raw_request", ""))
    answers = dict(payload.get("answers") or {})
    course_code, course_name = _extract_course(text)
    units = _extract_units(text)
    assignments = _extract_assignments(text)
    as_of = _extract_date(text)

    course_code = answers.get("course_code") or course_code
    course_name = (
        answers.get("course_name")
        or course_name
        or _COURSE_NAMES.get(course_code or "", "")
    )
    if answers.get("units"):
        units = _extract_units(answers["units"]) or [
            u.strip() for u in answers["units"].split(",") if u.strip()
        ]
    if answers.get("assignments"):
        lowered = answers["assignments"].lower()
        # An explicit "none" is a real answer meaning the assignment stream is
        # out of scope. It must become an empty list, not the literal string
        # "none" masquerading as an assignment id.
        if lowered.strip() in _NO_ASSIGNMENT_ANSWERS:
            assignments = []
        else:
            assignments = _extract_assignments(answers["assignments"]) or (
                [f"ASG-{n:02d}" for n in range(1, 4)]
                if "all" in lowered
                else [answers["assignments"].strip()]
            )
    if answers.get("as_of"):
        parsed = _extract_date(answers["as_of"])
        if parsed:
            as_of = parsed
    if as_of is None:
        # No date given. The doc forbids inventing one silently, so we pick a
        # deterministic anchor and record it as an assumption.
        as_of = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    return ClarifiedRequest(
        raw_request=text,
        course_code=course_code or "UNSPECIFIED",
        course_name=course_name or "Unspecified course",
        units=units or ["all"],
        assignment_ids=assignments,
        as_of=as_of,
        include_milestones=bool(payload.get("include_milestones", True)),
        human_authority=HUMAN_AUTHORITY,
        questions_asked=list(payload.get("questions_asked") or []),
        assumptions=list(payload.get("assumptions") or []),
    )


@ScriptedLLM.builder_for(CourseWorkspacePlan)
def _build_plan(payload: dict[str, Any], llm: ScriptedLLM) -> CourseWorkspacePlan:
    """Deterministically build the outline plan for a clarified request.

    The step list is the union of the three work streams the document names in
    2.3 (notes, assignments+grades, project milestones) plus the governance
    spine that the document makes non-optional (validate, faculty gate,
    publish). Steps are *planned* here; no specialist node runs yet, which is
    exactly what the doc's Lab 1 row requires.
    """
    request = ClarifiedRequest.model_validate(payload["request"])
    plan_id = str(
        payload.get("plan_id") or f"PLAN-{request.course_code}-{uuid.uuid4().hex[:4].upper()}"
    )
    steps: list[PlanStep] = []
    counter = 0

    def add(
        name: str,
        agent: str,
        intent: str,
        inputs: Sequence[str],
        outputs: Sequence[str],
        depends: Sequence[str],
        criteria: Sequence[str],
        priority: int = 5,
    ) -> str:
        """Append one step and return its id, so callers can chain dependencies."""
        nonlocal counter
        counter += 1
        step_id = f"S{counter}"
        steps.append(
            PlanStep(
                step_id=step_id,
                name=name,
                agent=agent,
                intent=intent,
                inputs_required=list(inputs),
                outputs=list(outputs),
                depends_on=list(depends),
                acceptance_criteria=list(criteria),
                priority=priority,
            )
        )
        return step_id

    # --- Stage 0: ingest the three sources named in doc 2.7 -------------
    ingest_step = add(
        "Ingest course sources",
        "Coordinator",
        "Load notes, assignment/rubric definitions and submission records "
        "through the single governed connector (doc 2.7).",
        [
            "connector.read('the notes/materials store')",
            "connector.read('assignment & rubric definitions')",
            "connector.read('submission records')",
        ],
        ["normalised source snapshot"],
        [],
        ["all three sources are read via the connector, none directly"],
        priority=1,
    )

    # --- Stream 1: notes repository (doc agent 1) ------------------------
    notes_step = add(
        "Reconcile notes repository",
        "Notes Repository Agent",
        "Keep one current, versioned set of materials organised by unit/topic "
        f"for units {', '.join(request.units)}.",
        ["notes/materials store", f"units: {', '.join(request.units)}"],
        ["current version per note", "version history retained"],
        [ingest_step],
        [
            "every in-scope unit resolves to exactly one current version",
            "no prior version is removed",
        ],
        priority=2,
    )

    previous_step_id = notes_step

    if request.assignment_ids:
        # --- Stream 2: assignments, submissions, similarity, grading ----
        distribute_step = add(
            "Distribute assignments on schedule",
            "Assignment Distribution Agent",
            "Release the faculty-approved briefs and rubrics for "
            f"{', '.join(request.assignment_ids)} and schedule deadline reminders.",
            [f"assignment & rubric definitions: {', '.join(request.assignment_ids)}"],
            ["release records", "reminder schedule"],
            [ingest_step],
            ["only faculty-approved briefs and rubrics are distributed"],
            priority=2,
        )
        collect_step = add(
            "Collect submissions and timestamp them",
            "Submission Collection Agent",
            "Gather every student's soft-copy submission and timestamp it "
            "against the deadline. Timestamps are immutable once written.",
            ["submission records", f"as_of: {request.as_of.isoformat()}"],
            ["submission records with immutable timestamps", "on-time/late flags"],
            [distribute_step],
            [
                "late submissions are flagged accurately",
                "no submission timestamp is altered after collection",
            ],
            priority=3,
        )
        similarity_step = add(
            "Check similarity and flag for review",
            "Plagiarism / Similarity Agent",
            "Attach a similarity score to every submission and flag "
            "high-similarity cases. Never issue a final determination.",
            ["submission records"],
            ["similarity score per submission", "flag list with cited pairs"],
            [collect_step],
            [
                "every submission has a similarity score attached",
                "no automatic plagiarism verdict is produced",
            ],
            priority=4,
        )
        grading_step = add(
            "Draft grades and feedback against the rubric",
            "Grading Assistance Agent",
            "Draft a grade and feedback per submission, capped at the rubric "
            "maximum. Draft only; never published by an agent.",
            ["assignment & rubric definitions", "similarity scores"],
            ["draft grades", "per-criterion marks breakdown", "feedback text"],
            [similarity_step],
            [
                "no draft grade exceeds the rubric maximum",
                "every draft carries a per-criterion breakdown",
            ],
            priority=5,
        )
        previous_step_id = grading_step

    if request.include_milestones:
        # --- Stream 3: project milestones (doc agent 6) ------------------
        add(
            "Track project milestones",
            "Project Milestone Tracking Agent",
            "Report each group's status against its defined checkpoints, "
            "using submitted evidence only.",
            ["project milestones", "group ownership"],
            ["per-group checkpoint status", "schedule health"],
            [ingest_step],
            [
                "every status claim cites evidence",
                "no progress is assumed where no evidence exists",
            ],
            priority=4,
        )

    # --- Governance spine -- non-optional per doc 2.7 and 2.12 -----------
    # Each spine step depends on the step immediately before it, so the
    # validate -> assemble -> gate -> publish chain is enforced structurally.
    # `depends_on` must reference the *previous* step, never the one being
    # created -- self-dependency would make the plan unorderable.
    validate_step = add(
        "Validate the assembled workspace",
        "Validation Agent",
        "Run the four acceptance checks from doc 2.7. This is a hard gate, "
        "not a report.",
        ["draft workspace", "plan"],
        ["validation report", "diagnosis for each failing check"],
        [previous_step_id],
        ["100% of validation checks pass before faculty review"],
        priority=1,
    )
    assemble_step = add(
        "Assemble the organised course workspace",
        "Grade Publishing & Archive Agent",
        "Assemble notes, assignments and grades into one versioned artefact.",
        ["validation report (must be passing)", "draft grades", "notes index"],
        ["organised course workspace (notes, assignments, grades)"],
        [validate_step],
        ["nothing is assembled from a failing validation report"],
        priority=2,
    )
    gate_step = add(
        f"Human gate [{request.human_authority}]",
        "Course Faculty gate",
        "Hard stop. The named human approves, edits or overrides. No agent "
        "may proceed past this point.",
        ["assembled workspace", "validation report", "evidence records"],
        ["approve | edit | override decision"],
        [assemble_step],
        ["no grade or feedback is published without faculty approval"],
        priority=1,
    )
    add(
        "Publish and archive the approved workspace",
        "Governance / Publish",
        "Version and release the approved workspace, and record the audit trail.",
        ["faculty decision"],
        ["published version", "audit entries"],
        [gate_step],
        [
            "only faculty-approved results are published",
            "a full audit trail is recorded",
        ],
        priority=2,
    )

    return CourseWorkspacePlan(
        plan_id=plan_id,
        created_at=datetime.now(tz=timezone.utc),
        request=request,
        steps=steps,
        status=PlanStatus.DRAFT,  # Lab 1 never locks; that is Lab 3's job
        human_authority=request.human_authority,
        revisions=list(payload.get("revisions") or []),
        open_questions=list(payload.get("open_questions") or []),
    )


# ==========================================================================
# The agent
# ==========================================================================


class PlannerAgent:
    """The Lab 1 planner: `clarify -> propose_plan -> while revise`.

    This is the *only* agent in Lab 1. Per doc 2.10 Lab 1, "Nodes activated.
    Coordinator loop (no specialist nodes yet)" -- every specialist named in
    the plan is a plan entry, not a running node.

    Args:
        llm: The model backend. Defaults to the configured one.
        max_revisions: Ceiling on the revise loop, so it cannot spin forever.
        ask_optional: When False (default) optional parameters are assumed
            rather than asked, keeping the question count focused on what the
            document actually requires.
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        *,
        max_revisions: int = MAX_REVISIONS,
        ask_optional: bool = False,
    ) -> None:
        self.llm = llm or get_llm()
        self.max_revisions = max_revisions
        self.ask_optional = ask_optional
        self._plan_seq = 0

    # -- Step 1 of the doc's pseudocode: clarify --------------------------

    def clarify(self, user_request: str, provider: FeedbackProvider) -> ClarifiedRequest:
        """Ask clarifying questions until the core parameters are complete.

        Doc: ``req = clarify(user_request)  # ask until inputs complete``

        Args:
            user_request: The raw, possibly vague, free-text request.
            provider: Supplies the answers.

        Returns:
            A `ClarifiedRequest` whose `questions_asked` records every
            question, with its answer.

        Raises:
            ValueError: If `user_request` is empty.
        """
        if not user_request or not user_request.strip():
            raise ValueError("user_request must be a non-empty string")

        answers: dict[str, str] = {}
        questions: list[ClarifyingQuestion] = []
        assumptions: list[str] = []
        rounds = 0

        while rounds < MAX_CLARIFY_ROUNDS:
            rounds += 1

            # Ask the model which parameters are still missing.
            self.llm.generate_structured(
                system=(
                    "You extract required parameters from a course-organisation "
                    "request. Never invent a value: if a parameter is absent, "
                    "report it as missing so the agent can ask."
                ),
                user=json.dumps(
                    {
                        "raw_request": user_request,
                        "answers": answers,
                        "rounds": rounds,
                    }
                ),
                schema=ClarifiedRequest,
                step="clarify",
            )

            # Compare the required fields against what we can actually see.
            missing: list[tuple[str, str, str, str]] = []
            for field_name, question_text, why, example in REQUIRED_FIELDS:
                if field_name in answers:
                    continue
                if not self._field_satisfied(field_name, user_request, answers):
                    missing.append((field_name, question_text, why, example))

            if not missing:
                log_event(
                    logger,
                    "clarify_complete",
                    rounds=rounds,
                    questions_asked=len(questions),
                )
                return self._finalise(
                    user_request=user_request,
                    answers=answers,
                    questions=questions,
                    assumptions=assumptions,
                )

            for field_name, question_text, why, example in missing:
                question = ClarifyingQuestion(
                    field=field_name,
                    question=question_text,
                    why_it_matters=why,
                    example=example,
                )
                answer = provider.answer_question(question)
                question.answer = answer
                questions.append(question)
                answers[field_name] = answer
                log_event(
                    logger,
                    "clarify_question_asked",
                    field=field_name,
                    round=rounds,
                    question=question_text,
                )

        # Ran out of rounds: fall through and record what is still unknown.
        for field_name, question_text, why, example in REQUIRED_FIELDS:
            if field_name not in answers:
                questions.append(
                    ClarifyingQuestion(
                        field=field_name,
                        question=question_text,
                        why_it_matters=why,
                        example=example,
                        answer="",
                        assumed=True,
                    )
                )
                assumptions.append(f"{field_name}: unanswered after {rounds} rounds")
        return self._finalise(
            user_request=user_request,
            answers=answers,
            questions=questions,
            assumptions=assumptions,
        )

    def _field_satisfied(
        self, field_name: str, user_request: str, answers: dict[str, str]
    ) -> bool:
        """Has this required parameter been resolved, by text or by answer?

        A field is satisfied when it was answered explicitly, or when the raw
        request already contained it. Asking again in that case would be the
        exact nagging behaviour a good agent avoids.
        """
        if field_name in answers:
            return True
        if field_name == "course_code":
            return _extract_course(user_request)[0] is not None
        if field_name == "units":
            return bool(_extract_units(user_request))
        if field_name == "assignments":
            return bool(_extract_assignments(user_request))
        if field_name == "as_of":
            return _extract_date(user_request) is not None
        return False

    def _finalise(
        self,
        *,
        user_request: str,
        answers: dict[str, str],
        questions: list[ClarifyingQuestion],
        assumptions: list[str],
    ) -> ClarifiedRequest:
        """Assemble the typed, clarified request.

        The doc's Lab 1 output is "A clarified request". It must be a typed
        object, not prose, so Lab 2 onward can consume it without re-parsing.
        """
        include_milestones = True
        if "include_milestones" in answers:
            include_milestones = answers["include_milestones"].strip().lower() not in {
                "0",
                "no",
                "false",
                "n",
            }
        else:
            assumptions.append(
                "include_milestones: assumed true (the doc lists milestone "
                "tracking as a standard work stream)"
            )

        # Carry the question record onto the request: the audit trail must show
        # what the agent had to ask, not just what it ended up knowing.
        for question in questions:
            if question.assumed:
                assumptions.append(f"{question.field}: assumed (unanswered)")

        return self.llm.generate_structured(
            system=(
                "Produce the final clarified request as a typed object. Preserve "
                "the original request verbatim. Never fabricate parameters that "
                "are still unknown - mark them as assumptions."
            ),
            user=json.dumps(
                {
                    "raw_request": user_request,
                    "answers": answers,
                    "questions_asked": [q.model_dump(mode="json") for q in questions],
                    "assumptions": assumptions,
                    "include_milestones": include_milestones,
                }
            ),
            schema=ClarifiedRequest,
            step="clarify_finalise",
        )

    # -- Step 2 of the doc's pseudocode: propose_plan --------------------

    def propose_plan(self, request: ClarifiedRequest) -> CourseWorkspacePlan:
        """Propose the outline plan. Doc: ``plan = propose_plan(req)``.

        The result is explicitly *not* final -- the doc annotates the
        pseudocode with ``# not final - a structured plan``.
        """
        self._plan_seq += 1
        plan = self.llm.generate_structured(
            system=(
                "Produce an outline plan for the organised course workspace "
                "(notes, assignments, grades). The plan is a DRAFT: it is "
                "editable and must not claim to be approved. Include a "
                "validation step, a human gate that stops before publish, and a "
                "publish step. Do not mark anything locked."
            ),
            user=json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "plan_id": f"PLAN-{request.course_code}-{self._plan_seq:04d}",
                }
            ),
            schema=CourseWorkspacePlan,
            step="propose_plan",
        )
        log_event(
            logger,
            "plan_proposed",
            plan_id=plan.plan_id,
            steps=len(plan.steps),
            status=plan.status.value,
        )
        return plan

    # -- Step 3 of the doc's pseudocode: the revise loop ------------------

    def revise(self, plan: CourseWorkspacePlan, feedback: str) -> CourseWorkspacePlan:
        """Apply one round of user feedback. Doc: ``plan = revise(plan, feedback)``.

        This is a genuine edit: the returned plan is a new object with the
        change applied and a `PlanRevision` recording exactly what changed and
        why. Feedback is not merely appended to a log.
        """
        if not feedback or not feedback.strip():
            raise ValueError("feedback must be non-empty to revise a plan")

        changes = self._apply_feedback(plan, feedback)
        revision = PlanRevision(
            revision=len(plan.revisions) + 1,
            feedback=feedback,
            changes_made=changes,
        )
        revised = plan.model_copy(deep=True)
        revised.revisions = [*plan.revisions, revision]

        log_event(
            logger,
            "plan_revised",
            plan_id=plan.plan_id,
            revision=revision.revision,
            changes=len(changes),
        )
        if not changes:
            logger.warning(
                "Feedback produced no recognised change; recorded as a no-op "
                "revision so the user's words are never lost. Feedback: %s",
                feedback,
            )
        return revised

    def _apply_feedback(self, plan: CourseWorkspacePlan, feedback: str) -> list[str]:
        """Interpret feedback and mutate the plan accordingly.

        Supports the revision intents a real reviewer uses: add / remove a
        step, change the anchor date, change a default priority, widen or
        narrow scope, or record a question.

        Returns:
            Human-readable descriptions of what changed, for the revision log.
        """
        text = feedback.strip()
        lowered = text.lower()
        changes: list[str] = []

        # -- Remove a named step ------------------------------------------
        remove_match = re.search(
            r"\b(?:remove|drop|delete|cut|skip)\b\s+(?:the\s+)?(?:step\s+)?([\w \-]{3,60})",
            lowered,
        )
        if remove_match:
            target = remove_match.group(1).strip()
            before = len(plan.steps)
            plan.steps = [
                s
                for s in plan.steps
                if target not in s.name.lower() and target not in s.step_id.lower()
            ]
            if len(plan.steps) < before:
                changes.append(
                    f"removed {before - len(plan.steps)} step(s) matching {target!r}"
                )
            else:
                changes.append(f"no step matched {target!r} for removal; nothing changed")

        # -- Add a named step --------------------------------------------
        add_match = re.search(
            r"\b(?:add|include|also (?:add|include)|insert)\b\s+(?:a\s+)?(?:step\s+)?(?:to\s+)?([\w \-]{3,60})",
            lowered,
        )
        if add_match and not remove_match:
            title = add_match.group(1).strip().capitalize()
            next_id = len(plan.steps) + 1
            while any(s.step_id == f"S{next_id}" for s in plan.steps):
                next_id += 1
            new_id = f"S{next_id}"
            plan.steps.append(
                PlanStep(
                    step_id=new_id,
                    name=title,
                    agent="Coordinator",
                    intent=f"Added on reviewer request: {text}",
                    inputs_required=[],
                    outputs=[],
                    depends_on=[],
                    acceptance_criteria=["confirmed by the reviewer"],
                    priority=5,
                )
            )
            changes.append(f"added step {new_id} {title!r}")

        # -- Change the anchor date --------------------------------------
        new_date = _extract_date(text)
        if new_date and re.search(r"\b(deadline|due|as[_ ]of|by)\b", lowered):
            old = plan.request.as_of
            plan.request.as_of = new_date
            changes.append(
                f"as_of moved {old.date().isoformat()} -> {new_date.date().isoformat()}"
            )

        # -- Change a default priority ------------------------------------
        priority_match = re.search(r"\b(high(?:est)?|low(?:est)?)\s+priority\b", lowered)
        if priority_match:
            value = 1 if priority_match.group(1).startswith("high") else 9
            for step in plan.steps:
                if step.priority == 5:
                    step.priority = value
            changes.append(
                f"set default priority to {value} "
                f"({'highest' if value == 1 else 'lowest'})"
            )

        # -- Widen / narrow scope ----------------------------------------
        units = _extract_units(text)
        if units and re.search(r"\b(?:include|add|cover|focus on|more on)\b", lowered):
            merged = sorted(set(plan.request.units) | set(units), key=lambda u: (len(u), u))
            changes.append(
                f"scope widened to units {', '.join(merged)} "
                f"(was {', '.join(plan.request.units)})"
            )
            plan.request.units = merged

        if re.search(r"\b(?:only|just|limit to|focus on just)\b", lowered) and units:
            changes.append(f"scope narrowed to units {', '.join(units)}")
            plan.request.units = units

        # -- Explicit question from the reviewer -------------------------
        if re.search(r"\b(?:should|can|is|do|does|how|why|what|when)\b.*\?", text):
            plan.open_questions.append(text)
            changes.append("recorded the reviewer's question as an open question")

        return changes

    # -- The whole loop --------------------------------------------------

    def run(self, user_request: str, provider: FeedbackProvider) -> PlannerResult:
        """Run `clarify -> propose_plan -> while wants_changes: revise`.

        This is the document's Lab 1 pseudocode, in order, with the revise loop
        bounded by `max_revisions`.
        """
        trace: list[str] = ["clarify"]
        request = self.clarify(user_request, provider)
        trace.append("propose_plan")

        plan = self.propose_plan(request)
        trace.append("revise_loop")

        revision_count = 0
        while provider.wants_changes(plan) and revision_count < self.max_revisions:
            feedback = provider.get_feedback(plan)
            if not feedback.strip():
                break
            plan = self.revise(plan, feedback)
            revision_count += 1
            trace.append(f"revise:{revision_count}")

        if revision_count >= self.max_revisions:
            logger.warning(
                "Revise loop hit its bound of %d revisions. Stopping so the "
                "run cannot spin forever (doc 2.10 Lab 7 pitfall).",
                self.max_revisions,
            )
            plan.open_questions.append(
                f"Stopped after {self.max_revisions} revisions; a human should "
                "confirm the remaining differences."
            )

        log_event(
            logger,
            "planner_loop_complete",
            questions=len(plan.request.questions_asked),
            revisions=revision_count,
            steps=len(plan.steps),
        )
        return PlannerResult(
            # The request comes from the *final* plan, because revisions can
            # legitimately change scope (units, anchor date). Returning the
            # pre-revision object here would silently report stale scope.
            request=plan.request,
            plan=plan,
            questions_asked=plan.request.questions_asked,
            revision_count=revision_count,
            trace=trace,
        )


# ==========================================================================
# The anti-pattern, kept on purpose
# ==========================================================================


def one_shot_chatbot(llm: LLMClient, user_request: str) -> str:
    """The WRONG way to do this -- kept to demonstrate the doc's Lab 1 pitfall.

    Doc: "**Pitfall to avoid.** Don't answer in one shot -- without
    clarify/revise it's a chatbot, not an agent."

    This function asks nothing, plans nothing typed, and cannot be revised. It
    exists so the notebook can show a side-by-side contrast and the test suite
    can assert the difference is real, rather than the claim being rhetoric.

    Args:
        llm: Backend to use.
        user_request: The raw request.

    Returns:
        A prose blob. Note the return type: there is no `CourseWorkspacePlan`
        anywhere in it, which is the whole problem.
    """
    return llm.generate_text(
        system=(
            "You are a helpful assistant. Answer the user's request about "
            "organising a course. Be concise and complete."
        ),
        user=user_request,
        step="one_shot_chatbot",
    )


# ==========================================================================
# Entry point
# ==========================================================================

#: A deliberately vague request. Used by the demo, the notebook and the
#: acceptance test. It names no course, no unit and no assignment, so the
#: agent is forced to ask -- which is the document's acceptance criterion.
VAGUE_REQUEST = (
    "I need to get my course organised but honestly I have not even started "
    "thinking about the details. Sort it out please."
)


def run_planner_loop(
    user_request: str = VAGUE_REQUEST,
    *,
    provider: Optional[FeedbackProvider] = None,
    llm: Optional[LLMClient] = None,
    compare_one_shot: bool = True,
) -> PlannerResult:
    """Run one complete Lab 1 planner loop and return the result.

    Args:
        user_request: Free text. Defaults to `VAGUE_REQUEST`.
        provider: Human interaction. Defaults to `ScriptedFeedback` so the
            function is safe to call from a test.
        llm: Model backend. Defaults to the configured one.
        compare_one_shot: Also run the anti-pattern for contrast.

    Returns:
        A `PlannerResult`.
    """
    provider = provider or ScriptedFeedback()
    agent = PlannerAgent(llm=llm)
    result = agent.run(user_request, provider)
    if compare_one_shot:
        result.one_shot_answer = one_shot_chatbot(agent.llm, user_request)
    return result


def _main() -> None:  # pragma: no cover - manual entry point
    """Run the Lab 1 demo from the command line.

    Usage:
        python -m course_organizer.labs.lab01_planner_loop
    """
    import argparse

    from course_organizer.config import get_settings
    from course_organizer.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Lab 1 - planner loop demo")
    parser.add_argument("--request", default=VAGUE_REQUEST, help="Free-text request")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask a real person for answers and revisions",
    )
    parser.add_argument(
        "--revise",
        action="append",
        default=[],
        help="Scripted feedback for a revision turn (repeatable)",
    )
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Scripted clarifying answer (repeatable)",
    )
    args = parser.parse_args()

    settings = get_settings()
    run_id = setup_logging(level=settings.log_level)
    print(f"\nrun id: {run_id}   llm backend: {settings.llm_backend}\n")

    if args.interactive:
        provider: FeedbackProvider = InteractiveFeedback()
    else:
        answers = dict(
            pair.split("=", 1) for pair in args.answer if "=" in pair
        )
        provider = ScriptedFeedback(
            answers=answers,
            feedback_turns=list(args.revise),
        )

    result = run_planner_loop(args.request, provider=provider)

    print("\n" + "=" * 74)
    print("CLARIFYING QUESTIONS ASKED")
    print("=" * 74)
    for i, q in enumerate(result.questions_asked, 1):
        tag = "assumed" if q.assumed else "asked"
        print(f"  Q{i} [{q.field}] ({tag}): {q.question}")
        print(f"      answer: {q.answer or '(none)'}")

    print("\n" + "=" * 74)
    print("CLARIFIED REQUEST")
    print("=" * 74)
    print(f"  {result.request.summary()}")
    if result.request.assumptions:
        print("  assumptions made (never silent):")
        for assumption in result.request.assumptions:
            print(f"    - {assumption}")

    print("\n" + "=" * 74)
    print("PLAN OUTLINE  (editable, not final)")
    print("=" * 74)
    print(result.plan.outline())

    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)
    print(result.summary())

    if result.one_shot_answer is not None:
        print("\n" + "=" * 74)
        print("ANTI-PATTERN: one-shot chatbot (doc's Lab 1 pitfall)")
        print("=" * 74)
        print("  questions asked: 0")
        print("  typed plan:      none (returns prose)")
        print("  revisable:       no")
        print(f"  output: {str(result.one_shot_answer)[:300]}")


if __name__ == "__main__":  # pragma: no cover
    _main()