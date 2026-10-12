"""Lab 2 - the tool-using agent: read, compute, verify.

Document 2.10, Lab 2 - Tool-Using Agent
=======================================

> **Phase:** A - Foundations
>
> **Goal:** Give the agent tools it must call to act, rather than answering
> from memory.
>
> **What it adds to this worklet.** Readers for the notes/materials store and
> assignment & rubric definitions, plus a domain calculator/checker, so the
> planner acts on real data.
>
> **Inputs:** the notes/materials store; assignment & rubric definitions;
> parameters.
>
> **Outputs:** Verified computed values and parsed inputs.
>
> **Agents / nodes:** Coordinator + tool layer.
>
> **Deliverable & acceptance:** An agent that reads the notes/materials store
> and computes/validates through typed tools. Accept: **Key numbers and lookups
> always go through tools, never hand-done in the prompt.**
>
> **Nodes activated:** Tool layer feeding the early data agents.
>
> **Pitfall to avoid:** Never assert a fact in the prompt that a tool should
> verify.

What this module actually does
------------------------------

The acceptance criterion is behavioural, and a behaviour can be measured. This
module makes the criterion **checkable** with three independent mechanisms.

**1. The ledger is the evidence.** Every tool call is appended to a
:class:`~course_organizer.tools.base.ToolLedger` with its full validated input.
Lab 2's acceptance test is therefore an assertion about the ledger, not an
opinion: if a number appeared in the agent's report, a call produced it.

**2. The agent cannot reach the store.** This module imports
:class:`~course_organizer.tools.base.ToolRegistry` and nothing else that holds
data. There is no `open`, no `Path`, no ``json.load`` in this file. A test
parses this file with :mod:`ast` and fails the build if that ever changes, so
"through tools" is a structural property rather than a promise.

**3. Claims from the prompt are treated as untrusted, then checked.** The
pitfall is "never assert a fact in the prompt that a tool should verify". The
defence is to *parse* such claims out of free text, bind each to a key the
tools compute, and then look it up. This module ships a poisoned request in
which three claims are false and two are true; the agent must contradict the
first three and confirm the last two. A checker that rejected everything would
pass the same test, so the two true claims are what make the result meaningful.

Division of labour
------------------

+---------------------------+------------------------------------------------+
| Component                 | Job |
+===========================+================================================+
| ``ScriptedLLM`` / Gemini  | Turn free text into a :class:`ToolPlan`: which |
| (``get_llm``)             | tools to call and with what arguments.         |
+---------------------------+------------------------------------------------+
| :class:`ToolRegistry`     | Execute the plan. Rejects unknown tools and    |
|                           | invalid arguments. Records every call.         |
| :class:`ClaimExtractor`   | Find asserted facts in the prompt and bind     |
|                           | them to keys the tools compute. Deterministic. |
| :class:`ToolUsingAgent`   | Orchestrate the above, then verify every claim |
|                           | and guarantee the document's three calls.      |
+---------------------------+------------------------------------------------+

The model proposes; the tool layer disposes. That split is deliberate: an LLM
is good at mapping "just the first three units" onto ``units=['1','2','3']`` and
bad at being the authority on how many units a course has. Lab 4 gives the model
a job it is genuinely better at; here it is kept where it is reliable.

Example
-------
>>> result = run_tool_using_agent(POISONED_REQUEST)
>>> result.validation.valid
True
>>> [c.claim_id for c in result.contradicted]
['unit_count', 'assignment_count', 'marks_by_assignment']
>>> all(c.verified_value is not None for c in result.claims)
True
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from pydantic import Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.llm import LLMClient, LLMError, ScriptedLLM, get_llm
from course_organizer.logging_setup import log_event
from course_organizer.schemas import _Base
from course_organizer.tools.base import ToolError, ToolRegistry
from course_organizer.tools.compute import (
    ScopeQuery,
    ScopeValidationResult,
    build_default_registry,
)

logger = logging.getLogger(__name__)

DOC_REF = "2.10 Lab 2"

#: The document's pseudocode, verbatim. Every run is checked against this.
DOC_PSEUDOCODE = """\
a = tool.read('the notes/materials store')
b = tool.read('assignment & rubric definitions')
v = tool.compute(a, b)
assert v.valid
"""

#: The three calls the document's pseudocode requires, in order. An agent run
#: whose ledger lacks any of these has not satisfied the lab, whatever else it
#: produced.
REQUIRED_CALLS: tuple[str, ...] = (
    "read_notes_store",
    "read_assignment_rubrics",
    "compute_scope_validation",
)

#: A free-text request carrying false claims, true claims, a self-contradiction
#: and one uncomputable claim. Used by the notebook, the CLI demo and the
#: acceptance tests.
#:
#: The two *true* claims are the point. A checker that refuted everything would
#: pass a test containing only false claims, so "CS201 has 5 units" and "CS201
#: is worth 65 marks in total" are there to prove the agent discriminates rather
#: than reflexively disagreeing. The 9-vs-5 pair is deliberately
#: self-contradicting: the tools must refute one and confirm the other.
POISONED_REQUEST = (
    "Quick check on my courses before I brief the class. I already know all of "
    "this so just confirm it. CS201 has 9 units. CS201 has 5 units. CS201 has 5 "
    "units with notes. DBMS402 has no assignments at all. In CS201 the ASG-01 "
    "is worth 50 marks. CS201 is worth 65 marks in total. Also, the CS201 notes "
    "are clearly the best ones in the department."
)


# ==========================================================================
# Models the model produces
# ==========================================================================


class ToolCallRequest(_Base):
    """One tool call the plan asks for.

    Attributes:
        tool: A registered tool name. The registry rejects anything else.
        reason: Why this call is needed. A plan whose reasons are all "because
            I decided to" is a sign the model is guessing.
        arguments: Raw arguments, validated against the tool's input model
            before the handler runs.
    """

    tool: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolPlan(_Base):
    """The model's answer to "what should I look up, and how?".

    Attributes:
        course_code: The course to read. One run verifies one course; claims
            about other courses are verified by a second pass.
        units: In-scope units. Empty means every declared unit.
        assignment_ids: In-scope assignments. Empty means none.
        calls: The tool calls, in execution order.
        rationale: One paragraph on how the request was mapped to these calls.
    """

    course_code: str = Field(min_length=1)
    units: list[str] = Field(default_factory=list)
    assignment_ids: list[str] = Field(default_factory=list)
    calls: list[ToolCallRequest] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    def call_for(self, tool: str) -> Optional[ToolCallRequest]:
        """First planned call for a tool name, or None.

        Raises:
            KeyError: Never. Absent tools return None so callers can repair the
                plan rather than handle an exception.
        """
        for call in self.calls:
            if call.tool == tool:
                return call
        return None


# ==========================================================================
# The model produces a plan
# ==========================================================================

#: A course code as the document writes them: two to four capitals then digits.
COURSE_CODE_RE = re.compile(r"\b[A-Z]{2,4}\d{2,4}\b")

#: An assignment id as the document writes them.
ASSIGNMENT_ID_RE = re.compile(r"\bASG-\d+\b", re.IGNORECASE)

#: Ordinal and cardinal words a user may use instead of a digit. Both are
#: needed: "the first three units" mixes an ordinal head with a cardinal count.
_ORDINALS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "all": 0,
    "every": 0,
}


def _extract_units(text: str) -> list[str]:
    """Pull unit numbers out of free text.

    Recognises three forms, because these are the forms people actually type:

    * an explicit list -- "units 1, 2 and 4"
    * a range -- "units 2 to 5", "units 2-5"
    * an ordinal head -- "the first three units", "all units"

    Args:
        text: The user's free text.

    Returns:
        Unit numbers as strings, sorted numerically. Empty when the text names
        no units, which the tools read as "every declared unit".
    """
    found: set[int] = set()

    # Form 1: an explicit list, allowing "and" as a separator.
    for match in re.finditer(
        r"\bunits?\s+((?:\d+\s*(?:,|and|&|-|\s)\s*)*\d+)\b", text, re.IGNORECASE
    ):
        for number in re.findall(r"\d+", match.group(1)):
            found.add(int(number))

    # Form 2: a range, e.g. "units 2 to 5" or "units 2-5".
    for match in re.finditer(
        r"\bunits?\s+(\d+)\s*(?:to|-|through|and)\s*(\d+)\b", text, re.IGNORECASE
    ):
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end and end - start <= 200:
            found.update(range(start, end + 1))

    # Form 3: an ordinal head, e.g. "the first three units".
    for match in re.finditer(
        r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
        r"\s+(\w+)\s+units?\b",
        text,
        re.IGNORECASE,
    ):
        start = _ORDINALS[match.group(1).lower()]
        word = match.group(2).lower()
        if word in _ORDINALS:
            end = _ORDINALS[word]
        elif word.isdigit():
            end = int(word)
        else:
            continue
        if start == 0:
            return []  # "all units" means "do not narrow the scope"
        if start <= end and end - start <= 200:
            found.update(range(start, end + 1))

    return [str(n) for n in sorted(found)]


@ScriptedLLM.builder_for(ToolPlan)
def _build_tool_plan(payload: dict[str, Any], llm: ScriptedLLM) -> ToolPlan:
    """Deterministically map free text onto a `ToolPlan`.

    This is real extraction, not a stub: it finds course codes, unit
    references and assignment ids in the text, then assembles the three calls
    the document's pseudocode requires, in the order the pseudocode lists
    them. The live Gemini backend is handed the same schema and the same task
    through the same `generate_structured` call, so both backends exercise one
    code path downstream.

    Args:
        payload: The `user` string, coerced to a mapping by `ScriptedLLM`. The
            `request` key is the free text; `default_course` is the caller's
            fallback when the text names no course.
        llm: The client, for budget accounting. Unused otherwise.

    Returns:
        A schema-valid `ToolPlan`.

    Raises:
        LLMError: If the text names no course and no default was supplied.
            Guessing a course is exactly the failure Lab 2 exists to prevent,
            so this is an error rather than a silent default.
    """
    text = str(payload.get("request", ""))
    default_course = payload.get("default_course")

    courses = [c for c in COURSE_CODE_RE.findall(text) if c in {"CS201", "DBMS402"}]
    if courses:
        course = courses[0]
    elif default_course:
        course = str(default_course)
    else:
        raise LLMError(
            "could not determine a course from the request and none was "
            f"supplied. Text: {text!r}. Pass course_code=... to the runner."
        )

    units = _extract_units(text)
    assignments = [a.upper() for a in ASSIGNMENT_ID_RE.findall(text)]

    calls = [
        ToolCallRequest(
            tool="read_notes_store",
            reason=(
                f"the request makes claims about {course}'s notes, so the notes "
                "store must be read before any of them can be checked"
            ),
            arguments={"course_code": course, "units": units, "current_only": True},
        ),
        ToolCallRequest(
            tool="read_assignment_rubrics",
            reason=(
                f"the request makes claims about {course}'s marks, so the "
                "assignment and rubric definitions must be read"
            ),
            arguments={"course_code": course, "assignment_ids": assignments},
        ),
        ToolCallRequest(
            tool="compute_scope_validation",
            reason=(
                "the document requires validating the scope before acting on it "
                "(pseudocode: v = tool.compute(a, b); assert v.valid)"
            ),
            arguments={
                "course_code": course,
                "units": units,
                "assignment_ids": assignments,
            },
        ),
    ]

    return ToolPlan(
        course_code=course,
        units=units,
        assignment_ids=assignments,
        calls=calls,
        rationale=(
            f"Named {course} in the request. Narrowed to unit(s) "
            f"{', '.join(units) or 'all'} and assignment(s) "
            f"{', '.join(assignments) or 'none'}. Then issued the three calls the "
            "document's pseudocode requires, in the order it lists them."
        ),
    )


class PlanError(ToolError):
    """Raised when a plan cannot be produced or is unusable."""


# ==========================================================================
# Claims asserted in the prompt
# ==========================================================================


#: A fact measured over the whole course. Only a course-wide read can settle it.
SCOPE_COURSE = "course"

#: A fact measured over one named assignment. Any read including it will do.
SCOPE_ASSIGNMENT = "assignment"


class ClaimStatus(str, Enum):
    """The verdict on one claim found in the prompt.

    Attributes:
        CONFIRMED: The tool-computed value equals what the prompt asserted.
        CONTRADICTED: The tool-computed value differs. This is the case the
            document's pitfall is about.
        UNVERIFIABLE: No current tool computes this fact. The agent says so
            rather than inventing a value or silently agreeing.
    """

    CONFIRMED = "CONFIRMED_BY_TOOL"
    CONTRADICTED = "CONTRADICTED_BY_TOOL"
    UNVERIFIABLE = "NOT_VERIFIABLE_WITH_CURRENT_TOOLS"


class FactClaim(_Base):
    """A fact asserted in the prompt, bound to the tool that can check it.

    Attributes:
        claim_id: Stable identifier, also the key the checker looks up.
        text: The claim as written, quoted from the prompt.
        course_code: The course the claim is about.
        asserted_value: The value the prompt claimed.
        verified_value: The tool-computed value, or None when unverifiable.
        status: The verdict.
        source_tool: The tool that produced `verified_value`.
        source_call_id: That tool call's id in the ledger, so the claim can be
            traced to the exact call that settled it.
        verdict: One sentence for a human.
    """

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    course_code: str = Field(min_length=1)
    fact_scope: str = SCOPE_COURSE
    asserted_value: Any = None
    verified_value: Any = None
    verified_scope: Optional[str] = None
    status: ClaimStatus
    source_tool: Optional[str] = None
    source_call_id: Optional[str] = None
    verdict: str = Field(min_length=1)


@dataclass(frozen=True)
class ClaimPattern:
    """One recognisable way a prompt asserts a verifiable fact.

    Attributes:
        claim_id: The key the claim is checked against. It must be a key of
            `ScopeValidationResult.verified_facts`, or it will come back
            UNVERIFIABLE - which is a legitimate outcome, not a bug.
        regex: The pattern. Must define a named ``course`` group, an ``asg``
            group where relevant, and a named ``value`` group.
        scope: What the fact is measured over.

            * ``"course"`` -- a fact about the whole course, such as its unit
              count or its total available marks. It can only be verified by a
              course-wide read. Checking it against a read narrowed to some
              units or some assignments yields a confidently wrong number, which
              is worse than no answer at all.
            * ``"assignment"`` -- a fact about one named assignment, valid
              within any read that includes it.
        label: Human name for the fact.
        why_tool: Why this needs a tool rather than recall, for the report.
    """

    claim_id: str
    regex: re.Pattern[str]
    scope: str = SCOPE_COURSE
    value_group: str = "value"
    label: str = ""
    why_tool: str = ""

    def describe(self) -> str:
        """A readable declaration for the notebook and the report."""
        return (
            f"{self.claim_id:<28} {self.label}\n"
            f"{'':<28} scope: {self.scope}  (checkable only against a "
            f"{self.scope}-wide read)"
        )


_C = r"(?P<course>[A-Z]{2,4}\d{2,4})"
_A = r"(?P<asg>ASG-\d+)"
_V = r"(?P<value>\d+|no|zero|none|nothing)"

#: Words meaning zero, so "has no assignments" becomes the number 0.
_ZERO_WORDS = {"no", "zero", "none", "nothing"}

#: The documented claim set. Each entry is a fact a person might assert in a
#: prompt, and the tool that settles it. Kept small and explicit rather than
#: open-ended so the coverage is auditable: the notebook prints this table, and
#: a test asserts every pattern's `claim_id` resolves to a key the compute tool
#: actually produces - except the ones deliberately marked unverifiable.
CLAIM_PATTERNS: tuple[ClaimPattern, ...] = (
    ClaimPattern(
        claim_id="unit_count",
        regex=re.compile(rf"{_C}\s+(?:has|contains|covers|has got)\s+{_V}\s+units?\b"),
        scope=SCOPE_COURSE,
        label="how many units the course has",
        why_tool=(
            "the unit count lives in the notes store header; recalling it is "
            "the exact failure Lab 2's acceptance criterion forbids"
        ),
    ),
    ClaimPattern(
        claim_id="assignment_count",
        regex=re.compile(
            rf"{_C}\s+(?:has|contains|covers|has got)\s+{_V}\s+assignments?\b"
        ),
        scope=SCOPE_COURSE,
        label="how many assignments the course has",
        why_tool=(
            "the assignment count comes from the rubric definitions file, not "
            "from the model's memory of the course"
        ),
    ),
    ClaimPattern(
        claim_id="note_count",
        regex=re.compile(rf"{_C}\s+(?:has|contains)\s+{_V}\s+notes?\b"),
        scope=SCOPE_COURSE,
        label="how many notes the course has",
        why_tool="note_count is computed by read_notes_store, not recalled",
    ),
    ClaimPattern(
        claim_id="version_total",
        regex=re.compile(rf"{_C}\s+(?:has|contains)\s+{_V}\s+versions?\b"),
        scope=SCOPE_COURSE,
        label="how many note versions exist in total",
        why_tool=(
            "version_total is summed by read_notes_store; a prompt cannot know "
            "it, and the 2.12 guardrail on version history depends on it"
        ),
    ),
    ClaimPattern(
        claim_id="marks_by_assignment",
        regex=re.compile(
            rf"(?:in\s+)?(?:{_C}\s+)?(?:the\s+)?{_A}\s+"
            r"(?:is\s+worth|carries|is\s+out\s+of|has)\s+"
            rf"{_V}\s+marks?\b",
            re.IGNORECASE,
        ),
        scope=SCOPE_ASSIGNMENT,
        label="the maximum marks of one assignment",
        why_tool=(
            "max_marks is a field of the assignment definition; a wrong figure "
            "here would corrupt every later grade calculation"
        ),
    ),
    ClaimPattern(
        claim_id="total_available_marks",
        regex=re.compile(
            rf"{_C}\s+(?:is\s+worth|has|carries)\s+{_V}\s+marks\s+in\s+total\b"
        ),
        scope=SCOPE_COURSE,
        label="the total maximum marks available in the course",
        why_tool=(
            "the total is a sum over rubric definitions and must be computed, "
            "not summed from memory. It is course-wide: verifying it against a "
            "read narrowed to one assignment would understate it."
        ),
    ),
    ClaimPattern(
        claim_id="units_with_notes",
        regex=re.compile(
            rf"{_C}\s+has\s+{_V}\s+units?\s+(?:with|that have|having)\s+notes?\b"
        ),
        scope=SCOPE_COURSE,
        label="how many units have notes",
        why_tool="coverage is a set operation over the notes store",
    ),
    ClaimPattern(
        claim_id="notes_quality",
        regex=re.compile(
            rf"{_C}\s+(?:notes?|materials?)\s+(?:are|is)\s+"
            r"(?:clearly\s+|obviously\s+)?(?:the\s+)?"
            r"(?P<value>best|excellent|poor|outstanding|superior|weak|strong)\b"
        ),
        scope=SCOPE_COURSE,
        label="a subjective judgement about note quality",
        why_tool=(
            "DELIBERATELY NOT COMPUTABLE. No current tool measures quality, so "
            "the honest answer is to decline rather than invent a number"
        ),
    ),
)

#: Facts no current tool computes. A claim bound to one of these must come back
#: UNVERIFIABLE. Listed explicitly so a test can assert the agent never quietly
#: invents a value for them.
UNCOMPUTABLE_CLAIMS: frozenset[str] = frozenset({"notes_quality"})


class ClaimExtractor:
    """Finds fact-like claims in free text and binds them to tool keys.

    This is deliberately **rule-based, not model-based**, and the reason is
    auditability. A prompt is untrusted input. If an LLM decided which strings
    were claims, the same prompt could yield different claim sets on different
    runs, and the Lab 2 acceptance test would stop being reproducible. A fixed
    pattern list is deterministic, inspectable, and testable.

    What a model *is* used for in this lab is scope extraction
    (:func:`_extract_units` and the `ToolPlan` builder), where variation is the
    point.

    Attributes:
        patterns: The recognisers to apply.
    """

    def __init__(self, patterns: Sequence[ClaimPattern] = CLAIM_PATTERNS) -> None:
        self.patterns = tuple(patterns)

    def extract(self, text: str, *, default_course: str) -> list[FactClaim]:
        """Find every recognised claim in the text.

        Args:
            text: The user's free text.
            default_course: Course to attribute a claim to when the claim does
                not name one. Used for claims like "ASG-01 is worth 50 marks",
                which are ambiguous without a course.

        Returns:
            One unverified `FactClaim` per recognised claim, in the order they
            appear. `status` starts as UNVERIFIABLE; the agent always
            overwrites it, because only a tool may set a verdict.

        Two claims about the same fact with *different* values are both kept.
        "CS201 has 9 units, CS201 has 5 units" is a self-contradicting prompt,
        and the interesting finding is that the tools refute one and confirm
        the other. Collapsing them into a single claim would hide exactly the
        behaviour worth demonstrating.

        Raises:
            KeyError: If a pattern's named value group is missing from its own
                regex. That is a programming error in the pattern table, and
                failing loudly beats silently matching nothing.
        """
        claims: list[FactClaim] = []
        seen: set[tuple[str, str, str]] = set()

        for pattern in self.patterns:
            for match in pattern.regex.finditer(text):
                groups = match.groupdict()
                course = (groups.get("course") or default_course).upper()

                raw_value: Any = groups.get(pattern.value_group)
                if raw_value is None:
                    raise KeyError(
                        f"ClaimPattern {pattern.claim_id!r} declares value_group "
                        f"{pattern.value_group!r} but its regex has no such named "
                        f"group; groups present: {sorted(groups)}"
                    )
                if isinstance(raw_value, str) and raw_value.lower() in _ZERO_WORDS:
                    value: Any = 0
                elif isinstance(raw_value, str) and raw_value.isdigit():
                    value = int(raw_value)
                else:
                    value = raw_value

                # An assignment-scoped fact is keyed per assignment id, so two
                # claims about different assignments do not collide.
                claim_id = pattern.claim_id
                if claim_id == "marks_by_assignment" and groups.get("asg"):
                    claim_id = f"marks_by_assignment.{groups['asg'].upper()}"

                key = (claim_id, course, repr(value))
                if key in seen:
                    continue
                seen.add(key)

                claims.append(
                    FactClaim(
                        claim_id=claim_id,
                        text=match.group(0).strip(),
                        course_code=course,
                        fact_scope=pattern.scope,
                        asserted_value=value,
                        verified_value=None,
                        status=ClaimStatus.UNVERIFIABLE,
                        verdict="not yet checked - no tool has been consulted",
                    )
                )

        log_event(
            logger,
            "claims_extracted",
            count=len(claims),
            claim_ids=[c.claim_id for c in claims],
        )
        return claims

    @staticmethod
    def describe() -> str:
        """Every pattern, as a readable table for the notebook and report."""
        # Column widths are derived from the data so a long label cannot push
        # the last column off the edge.
        id_w = max(6, max(len(p.claim_id) for p in CLAIM_PATTERNS))
        scope_w = max(5, max(len(p.scope) for p in CLAIM_PATTERNS))
        label_w = max(5, max(len(p.label) for p in CLAIM_PATTERNS))
        header = (
            f"{'claim_id':<{id_w}}  {'scope':<{scope_w}}  "
            f"{'label':<{label_w}}  verdict source"
        )
        rule = "-" * len(header)
        lines = [header, rule]
        for pattern in CLAIM_PATTERNS:
            source = (
                "no tool computes this (by design)"
                if pattern.claim_id in UNCOMPUTABLE_CLAIMS
                else "computed by a registered tool"
            )
            lines.append(
                f"{pattern.claim_id:<{id_w}}  {pattern.scope:<{scope_w}}  "
                f"{pattern.label:<{label_w}}  {source}"
            )
        lines.append(rule)
        lines.append(f"{len(CLAIM_PATTERNS)} patterns registered")
        return "\n".join(lines)


# ==========================================================================
# Comparing a claim against a tool-computed value
# ==========================================================================


def _as_number(value: Any) -> Optional[float]:
    """Coerce a value to a float for comparison, or None.

    Handles ints, floats, numeric strings and booleans. Returns None for
    anything else, so the caller falls back to equality comparison rather than
    comparing incomparable types.

    Args:
        value: Any value from a claim or from `verified_facts`.

    Returns:
        The float value, or None if `value` is not numeric.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _resolve_fact(
    facts: dict[str, Any], claim_id: str
) -> tuple[bool, Any]:
    """Look up a claim's key in the verified-facts mapping.

    Handles the two shapes the compute tool produces: a flat key such as
    ``unit_count``, and a per-assignment sub-key such as
    ``marks_by_assignment.ASG-01`` which lives inside a nested mapping.

    Args:
        facts: `ScopeValidationResult.verified_facts`.
        claim_id: The key to resolve.

    Returns:
        ``(found, value)``. ``found`` is False when no current tool computes the
        fact, which the agent reports as UNVERIFIABLE rather than guessing.
    """
    if claim_id in facts:
        return True, facts[claim_id]

    if "." in claim_id:
        head, _, tail = claim_id.partition(".")
        nested = facts.get(head)
        if isinstance(nested, dict) and tail in nested:
            return True, nested[tail]

    return False, None


# ==========================================================================
# The run result
# ==========================================================================


@dataclass
class ToolRunResult:
    """Everything one tool-using agent run produced.

    Attributes:
        run_id: Correlates the ledger, the logs and this result.
        user_request: The free text that started the run.
        plan: The `ToolPlan` the model produced, after any repair.
        plan_repaired: True if the agent had to append a call the document
            requires but the plan omitted.
        validation: The primary course's `ScopeValidationResult`, i.e. ``v``.
        extra_validations: Results for any other course named in a claim.
        claims: Every claim found, each with its verdict.
        ledger_report: The audit table.
        calls_used: Number of tool calls recorded.
        llm_calls_used: LLM calls charged, for budget visibility.
    """

    run_id: str
    user_request: str
    plan: ToolPlan
    plan_repaired: bool
    validation: ScopeValidationResult
    claims: list[FactClaim]
    ledger_report: str
    calls_used: int
    llm_calls_used: int
    extra_validations: list[ScopeValidationResult] = field(default_factory=list)

    # -- claim queries -----------------------------------------------------

    @property
    def contradicted(self) -> list[FactClaim]:
        """Claims the tools refuted. The pitfall, made visible."""
        return [c for c in self.claims if c.status is ClaimStatus.CONTRADICTED]

    @property
    def confirmed(self) -> list[FactClaim]:
        """Claims the tools agreed with."""
        return [c for c in self.claims if c.status is ClaimStatus.CONFIRMED]

    @property
    def unverifiable(self) -> list[FactClaim]:
        """Claims no current tool can settle."""
        return [c for c in self.claims if c.status is ClaimStatus.UNVERIFIABLE]

    # -- report ------------------------------------------------------------

    def claim_table(self) -> str:
        """A readable claim-by-claim table.

        Column widths come from the data, so a long claim id or a long scope
        label such as ``scoped(assignments ASG-01)`` cannot push the status
        column out of alignment.
        """
        if not self.claims:
            return "no fact claims were found in the request"

        def render(claim: FactClaim) -> tuple[str, str, str, str, str]:
            """One row as (id, asserted, verified, scope, status)."""
            asserted = "-" if claim.asserted_value is None else str(claim.asserted_value)
            if claim.verified_value is None:
                verified = "n/a"
            elif isinstance(claim.verified_value, (dict, list)):
                verified = type(claim.verified_value).__name__
            else:
                verified = str(claim.verified_value)
            return (
                claim.claim_id,
                asserted,
                verified,
                claim.verified_scope or "-",
                claim.status.value,
            )

        rows = [render(claim) for claim in self.claims]
        headers = ("claim_id", "asserted", "verified", "settling scope", "status")
        widths = [
            max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(5)
        ]

        def line(cells: tuple[str, ...]) -> str:
            """Pad one row to the computed widths."""
            return "  ".join(
                cell.ljust(width) if i < 4 else cell
                for i, (cell, width) in enumerate(zip(cells, widths))
            )

        rule = "-" * len(line(headers))
        out = [line(headers), rule]
        out.extend(line(row) for row in rows)
        out.append(rule)
        out.append(
            f"{len(self.claims)} claim(s): {len(self.confirmed)} confirmed, "
            f"{len(self.contradicted)} contradicted, "
            f"{len(self.unverifiable)} unverifiable"
        )
        return "\n".join(out)

    def report(self) -> str:
        """The full acceptance report for one run.

        Ordered to answer the document's acceptance question first: did the
        key numbers come through tools, and were the prompt's claims checked
        against them?
        """
        sections = [
            "=" * 78,
            f"Lab 2 acceptance report - run {self.run_id}",
            "=" * 78,
            "",
            "[1] Document pseudocode",
            DOC_PSEUDOCODE.rstrip(),
            "",
            "[2] Tool plan (produced by the model, executed by the registry)",
            f"    course      : {self.plan.course_code}",
            f"    units       : {', '.join(self.plan.units) or '(all declared)'}",
            f"    assignments : {', '.join(self.plan.assignment_ids) or '(none)'}",
            f"    rationale   : {self.plan.rationale}",
            f"    repaired    : {self.plan_repaired}",
            "",
            "[3] Ledger: the evidence that lookups went through tools",
            self.ledger_report,
            "",
            "[4] assert v.valid",
            f"    course      : {self.validation.course_code}",
            f"    scope       : {self.validation.scope_label()}",
            f"    valid       : {self.validation.valid}",
        ]
        for check in self.validation.checks:
            mark = "PASS" if check.passed else "FAIL"
            sections.append(f"    [{mark}] {check.check_id:<20} {check.detail}")
        for extra in self.extra_validations:
            reason = (
                "claims naming that course"
                if extra.course_code != self.validation.course_code
                else "a course-wide claim needed a course-wide read"
            )
            sections.append(
                f"    additional pass: {extra.course_code} "
                f"scope={extra.scope_label()} valid={extra.valid} ({reason})"
            )

        sections += [
            "",
            "[5] Tool-verified facts (the only numbers this run may assert)",
            f"    scope of the figures below: {self.validation.scope_label()}",
        ]
        for name, value in sorted(self.validation.verified_facts.items()):
            sections.append(f"    {name:<24} {value}")

        sections += [
            "",
            "[6] Claims found in the prompt, checked against those facts",
            self.claim_table(),
            "",
        ]
        for claim in self.contradicted:
            sections.append(
                f"    REFUTED   \"{claim.text}\"\n"
                f"              the tools say {claim.verified_value} "
                f"[{claim.verified_scope}, call {claim.source_call_id}]\n"
                f"              {claim.verdict}"
            )
        for claim in self.confirmed:
            sections.append(
                f"    CONFIRMED \"{claim.text}\"\n"
                f"              the tools say {claim.verified_value} "
                f"[{claim.verified_scope}, call {claim.source_call_id}]"
            )
        for claim in self.unverifiable:
            sections.append(
                f"    UNKNOWN   \"{claim.text}\"\n              {claim.verdict}"
            )
        if not self.contradicted and not self.unverifiable:
            sections.append("    every claim in the request was confirmed by a tool")

        sections += [
            "",
            "[7] Verdict",
            f"    v.valid                = {self.validation.valid}",
            f"    required calls present = "
            f"{all(c in self.ledger_report for c in REQUIRED_CALLS)}",
            f"    tool calls made        = {self.calls_used}",
            f"    llm calls made         = {self.llm_calls_used}",
            f"    claims confirmed       = {len(self.confirmed)}",
            f"    claims contradicted    = {len(self.contradicted)}",
            f"    claims unverifiable    = {len(self.unverifiable)}",
            "=" * 78,
        ]
        return "\n".join(sections)


# ==========================================================================
# The agent
# ==========================================================================


class ToolUsingAgent:
    """Coordinates the model, the tool layer and the claim check.

    The agent is handed a :class:`ToolRegistry`, never a store. It has no
    import of :mod:`course_organizer.tools.seed_store` and no filesystem code
    at all; a test in ``tests/test_lab02_tool_using_agent.py`` parses this
    module with :mod:`ast` and fails if that ever changes.

    Args:
        registry: The tool layer. Defaults to the three Lab 2 tools.
        llm: The model backend. Defaults to `get_llm()`, which honours
            `LLM_BACKEND` in ``.env``.
        extractor: The claim recogniser. Defaults to the documented pattern set.
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        *,
        llm: Optional[LLMClient] = None,
        extractor: Optional[ClaimExtractor] = None,
    ) -> None:
        self.registry = registry or build_default_registry(run_id="placeholder")
        self.llm = llm or get_llm()
        self.extractor = extractor or ClaimExtractor()

    # -- step 1: plan ------------------------------------------------------

    def plan(self, user_request: str, *, default_course: Optional[str] = None) -> ToolPlan:
        """Ask the model which tools to call, and with what arguments.

        The model sees the tool catalogue and the request, and returns a
        `ToolPlan`. It does not see the data and cannot produce a number.

        Args:
            user_request: The free text.
            default_course: Course to use when the request names none. Pass the
                caller's known course rather than letting the model guess.

        Returns:
            A `ToolPlan`.

        Raises:
            PlanError: If no plan could be produced, with the backend's own
                message preserved so a live-backend failure is diagnosable.
        """
        catalogue = self.registry.describe_all()
        system = (
            "You are the apniLeap AURA Coordinator's tool selector. You are "
            "given a catalogue of typed tools and a user's free-text request. "
            "Choose which tools to call and with what arguments. You must not "
            "state or infer any value that a tool computes: you only decide "
            "what to look up. If the request mentions a course, scope the read "
            "to it. Honour the document's required order: notes store, then "
            "assignment and rubric definitions, then the scope validation."
        )
        # The user turn is a JSON object, matching the convention already used
        # by this package's scripted builders: the system turn carries the
        # rules, the user turn carries the data to reason over. Keeping it
        # structured means the scripted backend and Gemini receive the same
        # task, and the scripted builder spends its effort on extraction
        # rather than on re-parsing prose.
        user = json.dumps(
            {
                "document_pseudocode": DOC_PSEUDOCODE,
                "required_tool_order": list(REQUIRED_CALLS),
                "tool_catalogue": catalogue,
                "request": user_request,
                "default_course": default_course,
            },
            indent=2,
        )
        try:
            plan = self.llm.generate_structured(
                system=system, user=user, schema=ToolPlan, step="lab02_tool_selection"
            )
        except LLMError as exc:
            raise PlanError(f"could not produce a tool plan: {exc}") from exc

        log_event(
            logger,
            "tool_plan_ready",
            course_code=plan.course_code,
            units=plan.units,
            assignment_ids=plan.assignment_ids,
            planned_calls=[c.tool for c in plan.calls],
        )
        return plan

    # -- step 2: execute ---------------------------------------------------

    def execute(self, plan: ToolPlan) -> tuple[bool, dict[str, Any]]:
        """Run the plan's calls, then guarantee the document's three.

        The model proposes; the registry disposes. An unknown tool name or a
        schema-invalid argument raises from the registry rather than being
        quietly skipped, because a silently dropped call is exactly how a
        prompt-supplied number gets in.

        After the planned calls, any of `REQUIRED_CALLS` still absent is
        appended with arguments derived from the plan, and the run is flagged
        as repaired. The document's pseudocode is a floor, not a suggestion.

        Args:
            plan: The plan to execute.

        Returns:
            ``(repaired, results)`` where ``repaired`` is True if a call had to
            be appended, and ``results`` maps each required tool name to its
            `ToolResult`. The caller reuses ``results`` rather than re-invoking,
            so no tool runs twice in one run.

        Raises:
            ToolError: If any call fails. The ledger retains the failure, so
                the caller can inspect it.
        """
        results: dict[str, Any] = {}

        for call in plan.calls:
            if call.tool not in self.registry.names():
                raise ToolError(
                    f"the plan asked for tool {call.tool!r}, which is not "
                    f"registered. Available: {self.registry.names()}"
                )
            result = self.registry.invoke(call.tool, call.arguments)
            results[call.tool] = result
            if not result.ok:
                raise ToolError(
                    f"planned call {call.tool!r} failed: {result.error}. "
                    f"arguments were {call.arguments!r}"
                )

        repaired = False
        for name in REQUIRED_CALLS:
            if name in {c.tool for c in plan.calls}:
                continue
            log_event(logger, "plan_repaired", missing_tool=name)
            arguments = self._default_arguments(name, plan)
            plan.calls.append(
                ToolCallRequest(
                    tool=name,
                    reason=(
                        f"the document's pseudocode requires {name!r}; the plan "
                        "omitted it, so it was appended with the plan's own scope"
                    ),
                    arguments=arguments,
                )
            )
            result = self.registry.invoke(name, arguments)
            results[name] = result
            if not result.ok:
                raise ToolError(f"repaired call {name!r} failed: {result.error}")
            repaired = True

        return repaired, results

    @staticmethod
    def _default_arguments(tool: str, plan: ToolPlan) -> dict[str, Any]:
        """Arguments for a required call the plan omitted.

        Args:
            tool: The tool name.
            plan: The plan whose scope to reuse.

        Returns:
            An argument mapping valid for that tool.
        """
        if tool == "compute_scope_validation":
            return {
                "course_code": plan.course_code,
                "units": plan.units,
                "assignment_ids": plan.assignment_ids,
            }
        if tool == "read_notes_store":
            return {
                "course_code": plan.course_code,
                "units": plan.units,
                "current_only": True,
            }
        if tool == "read_assignment_rubrics":
            return {
                "course_code": plan.course_code,
                "assignment_ids": plan.assignment_ids,
            }
        raise ToolError(f"no default arguments known for tool {tool!r}")

    # -- step 3: verify the claims ---------------------------------------

    def verify_claims(
        self,
        claims: list[FactClaim],
        *,
        primary: ScopeValidationResult,
    ) -> list[ScopeValidationResult]:
        """Check every claim against tool-computed facts, at the right scope.

        Two rules govern which read may settle a claim.

        **A course-wide fact needs a course-wide read.** If the plan narrowed
        the read to some units or some assignments, then
        ``total_available_marks`` from that read is the total of the *subset*.
        Comparing "CS201 is worth 65 marks in total" against a read scoped to
        ASG-01 returns 20, and the agent would refute a true claim on the
        strength of its own scoping decision. So a course-wide claim against a
        narrowed plan triggers a second, course-wide read. The ledger shows it,
        and the claim records which scope settled it.

        **A claim about another course needs that course's read.** Verifying a
        claim about DBMS402 with CS201's numbers is the same error in a
        different costume.

        Args:
            claims: Unverified claims from the extractor.
            primary: The primary course's validation result. Its own
                `course_wide` flag says whether it may settle course-wide
                claims.

        Returns:
            Every additional validation run, in the order it was needed. The
            primary result is not included.
        """
        cache: dict[tuple[str, bool], Optional[ScopeValidationResult]] = {
            (primary.course_code, primary.course_wide): primary
        }
        extras: list[ScopeValidationResult] = []

        for claim in claims:
            # A secondary course is always read course-wide, so it can settle
            # both scopes. The primary can settle course-wide claims only when
            # the plan did not narrow it.
            wide = claim.fact_scope == SCOPE_COURSE or (
                claim.course_code != primary.course_code
            )
            key = (claim.course_code, wide)

            if key in cache:
                continue

            arguments: dict[str, Any] = {"course_code": claim.course_code}
            if wide:
                # A course-wide read narrows nothing, whatever the plan asked
                # for. The primary's units belong to its own course.
                arguments["units"] = []
                arguments["assignment_ids"] = []
            else:
                # An assignment-scoped claim can reuse the primary's own scope.
                arguments["units"] = list(primary.units)
                arguments["assignment_ids"] = list(primary.assignment_ids)

            result = self.registry.invoke("compute_scope_validation", arguments)
            if result.ok:
                cache[key] = result.value
                extras.append(result.value)
                log_event(
                    logger,
                    "scope_widened_for_claim",
                    claim_id=claim.claim_id,
                    course_code=claim.course_code,
                    scope=result.value.scope_label(),
                )
            else:
                cache[key] = None
                log_event(
                    logger,
                    "claim_unverifiable",
                    claim_id=claim.claim_id,
                    course_code=claim.course_code,
                    reason=result.error,
                )

        for claim in claims:
            wide = claim.fact_scope == SCOPE_COURSE or (
                claim.course_code != primary.course_code
            )
            self._settle(claim, cache.get((claim.course_code, wide)), wide=wide)

        return extras

    def _settle(
        self,
        claim: FactClaim,
        validation: Optional[ScopeValidationResult],
        *,
        wide: bool,
    ) -> None:
        """Set a claim's verdict from a validation result.

        Only a tool may set a verdict, which is why this is a separate method
        called after the reads. A claim with no validation behind it stays
        UNVERIFIABLE, whatever the prompt said.

        The settled claim records the ledger ``call_id`` of the compute call
        that produced its verified value, plus the scope that call covered, so
        any number in the report can be traced to the exact invocation and
        cannot be mistaken for a course-wide figure when it is not.

        Args:
            claim: The claim to settle, mutated in place.
            validation: The result for the claim's course at the right scope, or
                None if that read could not be performed.
            wide: True when `validation` covers the whole course.
        """
        if validation is None:
            if claim.verdict.startswith("not yet checked"):
                claim.verdict = (
                    f"could not verify: no scope validation succeeded for "
                    f"{claim.course_code}, so this claim stays unverified"
                )
            claim.status = ClaimStatus.UNVERIFIABLE
            return

        found, value = _resolve_fact(validation.verified_facts, claim.claim_id)
        if not found:
            claim.status = ClaimStatus.UNVERIFIABLE
            claim.verdict = (
                f"no current tool computes {claim.claim_id!r}. Declining to "
                "confirm or refute it. A subjective claim needs a human, not a "
                "number."
            )
            return

        claim.verified_value = value
        claim.verified_scope = validation.scope_label()
        claim.source_tool = "compute_scope_validation"
        claim.source_call_id = self._call_id_for(validation)

        claimed_number = _as_number(claim.asserted_value)
        verified_number = _as_number(value)
        if claimed_number is not None and verified_number is not None:
            equal = abs(claimed_number - verified_number) < 1e-9
        else:
            equal = claim.asserted_value == value

        scope_note = "" if wide else " within the read's own scope"
        if equal:
            claim.status = ClaimStatus.CONFIRMED
            claim.verdict = (
                f"the tools computed {value!r} for {claim.course_code}"
                f"{scope_note}, which matches the request"
            )
        else:
            claim.status = ClaimStatus.CONTRADICTED
            claim.verdict = (
                f"the prompt says {claim.asserted_value!r}, but the tools "
                f"computed {value!r} for {claim.course_code}{scope_note}. The "
                "tool value is used; the prompt value is rejected."
            )

    def _call_id_for(self, validation: ScopeValidationResult) -> Optional[str]:
        """Find the ledger id of the compute call that produced a validation.

        The ledger is append-only and ordered, so the *last* successful
        `compute_scope_validation` call whose arguments match this result's
        course **and scope** is the one that produced these facts. Matching the
        scope matters: a run may hold both a narrowed and a course-wide read for
        the same course, and citing the wrong one would misattribute the number.

        Args:
            validation: The result to attribute.

        Returns:
            The `call_id`, or None when the ledger holds no matching call.
        """
        found: Optional[str] = None
        for entry in self.registry.ledger:
            if entry.tool != "compute_scope_validation" or not entry.ok:
                continue
            if entry.input_payload.get("course_code") != validation.course_code:
                continue
            same_units = list(entry.input_payload.get("units") or []) == list(validation.units)
            same_assignments = list(
                entry.input_payload.get("assignment_ids") or []
            ) == list(validation.assignment_ids)
            if same_units and same_assignments:
                found = entry.call_id
        return found

    # -- the whole run -----------------------------------------------------

    def run(
        self, user_request: str, *, course_code: Optional[str] = None
    ) -> ToolRunResult:
        """Plan, execute, then verify every claim in the request.

        This is the agent loop of Lab 2. There is no retry edge and no back
        edge here: Lab 2's job is that the agent acts through tools, and Lab 6
        adds bounded retry when a tool is genuinely flaky.

        Args:
            user_request: Free text, which may contain asserted facts.
            course_code: The caller's known course. Used when the request does
                not name one.

        Returns:
            A `ToolRunResult`.

        Raises:
            PlanError: If no plan could be produced.
            ToolError: If a tool call failed.
        """
        run_id = self.registry.run_id or uuid.uuid4().hex[:12]
        claims = self.extractor.extract(
            user_request, default_course=course_code or "UNSPECIFIED"
        )

        plan = self.plan(user_request, default_course=course_code)
        repaired, results = self.execute(plan)

        # Reuse the compute result from execution rather than re-invoking it,
        # so exactly one scope validation runs per course per run and the
        # ledger count is meaningful.
        primary_result = results["compute_scope_validation"]
        if not primary_result.ok:
            raise ToolError(
                f"scope validation failed for {plan.course_code!r}: "
                f"{primary_result.error}"
            )
        primary = primary_result.value

        extras = self.verify_claims(claims, primary=primary)

        log_event(
            logger,
            "lab02_run_complete",
            run_id=run_id,
            course_code=plan.course_code,
            valid=primary.valid,
            claims=len(claims),
            contradicted=len([c for c in claims if c.status is ClaimStatus.CONTRADICTED]),
            calls=len(self.registry.ledger),
        )

        return ToolRunResult(
            run_id=run_id,
            user_request=user_request,
            plan=plan,
            plan_repaired=repaired,
            validation=primary,
            claims=claims,
            ledger_report=self.registry.ledger.report(),
            calls_used=len(self.registry.ledger),
            llm_calls_used=self.llm.calls_used,
            extra_validations=extras,
        )


# ==========================================================================
# Entry points
# ==========================================================================


def run_tool_using_agent(
    user_request: str,
    *,
    course_code: Optional[str] = None,
    llm: Optional[LLMClient] = None,
    run_id: Optional[str] = None,
) -> ToolRunResult:
    """Run the Lab 2 agent once, from a fresh registry and ledger.

    Args:
        user_request: Free text, which may contain asserted facts.
        course_code: The caller's known course, used when the request names
            none.
        llm: Model backend. Defaults to `get_llm()`.
        run_id: Ledger and log correlation id. Generated when omitted.

    Returns:
        A `ToolRunResult`.

    Raises:
        PlanError: If no plan could be produced.
        ToolError: If a tool call failed.
    """
    resolved_run_id = run_id or f"lab02-{uuid.uuid4().hex[:8]}"
    registry = build_default_registry(run_id=resolved_run_id)
    agent = ToolUsingAgent(registry, llm=llm)
    return agent.run(user_request, course_code=course_code)


def _main() -> int:
    """CLI demo: run the poisoned request and print the acceptance report.

    Returns:
        A process exit code: 0 if the run behaved as the lab requires, 1
        otherwise. Used as a self-checking demo, so a regression shows up as a
        non-zero exit rather than a subtly wrong report.
    """
    from course_organizer.logging_setup import setup_logging

    setup_logging()

    print("=" * 78)
    print("Lab 2 - Tool-Using Agent")
    print("=" * 78)
    print()
    print("[a] Document requirements this lab satisfies")
    print(
        "    Goal:  Give the agent tools it must call to act, rather than\n"
        "          answering from memory."
    )
    print(
        "    Accept: Key numbers and lookups always go through tools, never\n"
        "            hand-done in the prompt."
    )
    print("    Pitfall: Never assert a fact in the prompt that a tool should verify.")
    print()
    print("[b] The typed tool layer")
    print(ClaimExtractor.describe())
    print()
    print("[c] Running the document's pseudocode on a poisoned request")
    print()
    for line in POISONED_REQUEST.split(". "):
        if line.strip():
            print(f"    - {line.strip()}.")
    print()
    print("-" * 78)
    print()

    result = run_tool_using_agent(POISONED_REQUEST)

    print("[d] The tool plan the model produced")
    for call in result.plan.calls:
        print(f"    {call.tool}")
        print(f"        reason    : {call.reason}")
        print(f"        arguments : {call.arguments}")
    print()
    print(f"    plan repaired by the agent: {result.plan_repaired}")
    print()

    print("[e] Verified computed values, from the tools only")
    print(f"    scope of the figures below: {result.validation.scope_label()}")
    for name, value in sorted(result.validation.verified_facts.items()):
        print(f"    {name:<24} {value}")
    print()
    print(f"    assert v.valid -> {result.validation.valid}")
    for extra in result.extra_validations:
        print(
            f"    additional pass -> {extra.course_code} "
            f"scope={extra.scope_label()} valid={extra.valid}"
        )
    print()

    print("[f] Claims in the prompt, checked against those values")
    print(result.claim_table())
    print()
    for claim in result.contradicted:
        print(f"    REFUTED \"{claim.text}\"")
        print(f"            the tools computed {claim.verified_value} "
              f"[{claim.verified_scope}]")
        print(f"            {claim.verdict}")
    for claim in result.confirmed:
        print(f"    CONFIRMED \"{claim.text}\" -> {claim.verified_value} "
              f"[{claim.verified_scope}]")
    for claim in result.unverifiable:
        print(f"    UNKNOWN \"{claim.text}\"")
        print(f"            {claim.verdict}")
    print()

    print("[g] Audit ledger - every lookup, with the call that made it")
    print(result.ledger_report)
    print()

    print("[h] Acceptance")
    missing = [c for c in REQUIRED_CALLS if c not in result.ledger_report]
    # The expected split is asserted rather than merely printed, so a
    # regression in the claim checker fails the demo instead of quietly
    # producing a different report.
    ok = (
        result.validation.valid
        and not missing
        and len(result.contradicted) == 3
        and len(result.confirmed) == 3
        and len(result.unverifiable) == 1
        and all(c.source_call_id for c in result.contradicted + result.confirmed)
    )
    print(f"    required calls present : {not missing}" + (f" (missing {missing})" if missing else ""))
    print(f"    v.valid                : {result.validation.valid}")
    print(f"    claims contradicted    : {len(result.contradicted)} (expected 3)")
    print(f"    claims confirmed       : {len(result.confirmed)} (expected 3)")
    print(f"    claims unverifiable    : {len(result.unverifiable)} (expected 1)")
    print(f"    every settled claim traces to a call: "
          f"{all(c.source_call_id for c in result.claims if c.verified_value is not None)}")
    print(f"    human authority        : {HUMAN_AUTHORITY} (unchanged; Lab 2 approves nothing)")
    print()
    print("    RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())
