"""The three reads the document names, as typed tools.

Document 2.10, Lab 2, "What it adds to this worklet":

    "Readers for the notes/materials store and assignment & rubric definitions,
     plus a domain calculator/checker, so the planner acts on real data."

and its pseudocode:

    a = tool.read('the notes/materials store')
    b = tool.read('assignment & rubric definitions')
    v = tool.compute(a, b)
    assert v.valid

Document 2.7 names the connector surface as exactly three reads. The third,
``read('submission records')``, is not needed by Lab 2 but *is* an input of
Lab 4 ("history of prior assignment submissions"), so it lives here with its
siblings rather than in a lab module.

**How these tools reach data (changed by Lab 5, and the point of Lab 5).**
Originally each of these three functions called ``get_seed_store()`` itself.
That made the tool layer a *second* front door to the corpus, which is exactly
what doc 2.10 Lab 5 forbids -- *"No agent should touch a source directly; a
bypass defeats governance later"* -- and it made the acceptance criterion
*"swapping a source needs no agent change"* unprovable, because the tools held
their own private path to the filesystem.

So the three functions now go through
:func:`course_organizer.connector.client.ConnectorClient`, the same typed,
authorised, ledger-recording interface any agent would use. Nothing else in this
module names a source. Swapping ``SOURCE_ADAPTER`` to ``in_memory`` or
``google`` changes what these three functions return without changing a line
here, and ``tests/test_lab05_mcp_connector.py`` proves it by AST: outside
``course_organizer.connector``, only this one module has ever been allowed to
reach a source, and it no longer does.

One deliberate difference in *filtering* survives the move, and it is worth
stating because the two layers disagree on purpose:

* the **connector** refuses an unknown ``unit`` and names the real ones, because
  for an endpoint a guessed unit that returns an empty list reads as "this
  course has no notes";
* these **tools** report an unknown unit as ``units_missing`` and return
  ``ok=True``, because Lab 2's job is to report a *coverage gap* to a planner,
  and a hard error would stop the plan instead of describing the gap.

So the tools read the whole course through the connector and apply the unit and
topic filters themselves. Every byte of note, rubric and submission data still
arrives through the connector; only the *filtering* stays local.

Each read is a tool with a declared input model and output model, so:

* the planner cannot pass an untyped argument,
* the result is a validated object the next lab can consume directly,
* and every call lands on the ledger, which is how the acceptance criterion
  "key numbers and lookups always go through tools, never hand-done in the
  prompt" becomes checkable.

The tool names deliberately mirror the document's own phrasing, so a plan
written against Chapter 2 names the same things this module implements.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.connector.client import (
    ConnectorClient,
    InProcessConnectorClient,
)
from course_organizer.connector.errors import ConnectorError
from course_organizer.schemas import (
    AssignmentDefinition,
    NoteMaterial,
    SubmissionRecord,
    SubmissionStatus,
    _Base,
)
from course_organizer.tools.base import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

DOC_REF = "2.10 Lab 2"


# ---------------------------------------------------------------------------
# The one data door
# ---------------------------------------------------------------------------
# Built once per process and reused, for two reasons. The connector's own
# `get_seed_store` equivalent is cached for the same reason (repeated tool calls
# in one run should not re-read the corpus), and a fresh `EndpointRegistry` per
# tool call would give every call a fresh ledger, so the audit trail Lab 5 is
# built on would have one entry per call with no run-level view of them.
#
# `SOURCE_ADAPTER` is read at *build* time, so a test that swaps the adapter must
# call `reset_connector()` first. That is stated on the function rather than
# discovered later.
_CLIENT: Optional[ConnectorClient] = None
_REGISTRY = None


def connector() -> ConnectorClient:
    """The process-wide connector client these tools read through.

    Returns:
        A live :class:`ConnectorClient`. ``InProcessConnectorClient`` by
        default, because a tool call is in the same process as its caller and
        spawning a subprocess per tool call would cost seconds. The MCP client
        satisfies the same Protocol and is what Lab 5 uses to show the
        equivalent behaviour across a real process boundary.
    """
    global _CLIENT, _REGISTRY
    if _CLIENT is None:
        from course_organizer.connector import build_default_connector

        _REGISTRY = build_default_connector()
        _CLIENT = InProcessConnectorClient(
            registry=_REGISTRY, token=_default_token(_REGISTRY)
        )
    return _CLIENT


def _default_token(registry) -> str:
    """A token the connector's own registry will accept.

    The registry seeds its faculty principal from ``MCP_CONNECTOR_TOKEN`` (or an
    ephemeral one), so asking it for that same principal's token is the only
    way to get a credential without reading configuration here and duplicating
    the logic.
    """
    faculty = registry.principal_registry.subjects()
    for subject in faculty:
        if subject.startswith("faculty"):
            return registry.principal_registry.token_for(subject)
    return registry.principal_registry.token_for(faculty[0])


def reset_connector() -> None:
    """Drop the cached client so the next call re-reads ``SOURCE_ADAPTER``.

    Needed by any test that changes the adapter between cases. Not called
    implicitly on every read -- a tool must not be able to reconfigure the
    governance layer as a side effect of being invoked.
    """
    global _CLIENT, _REGISTRY
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None
    _REGISTRY = None


def use_connector(registry, token: str) -> None:
    """Install a specific connector for these tools to read through.

    The seam Lab 5's tests need, and the one that replaced monkeypatching
    ``readers.get_seed_store``. That patch stopped being possible the moment
    the tools stopped reaching for a store, and it was a poor seam anyway: it
    replaced the *data source* while leaving the connector -- the thing that
    authorises and records -- configured for a different corpus than the one it
    was serving. Installing a whole connector over a temporary seed directory
    is both expressible and honest.

    Args:
        registry: An :class:`~course_organizer.connector.core.EndpointRegistry`
            built over whatever source the caller wants.
        token: A credential that registry will accept.
    """
    global _CLIENT, _REGISTRY
    if _CLIENT is not None:
        _CLIENT.close()
    _REGISTRY = registry
    _CLIENT = InProcessConnectorClient(registry=registry, token=token)


# ==========================================================================
# Tool 1 -- read('the notes/materials store')
# ==========================================================================


class NotesQuery(_Base):
    """Arguments for `read_notes_store`.

    Attributes:
        course_code: Which course's notes store to read. Required, because the
            document scopes every read to one course.
        units: Restrict to these unit numbers. Empty means the whole store.
        topic: Case-insensitive substring match against title and topic.
        current_only: Return only each note's current version. The document's
            guardrail is "keep clear version history", so history is *retained*
            in the store; this flag controls what the caller is handed, not
            what is kept.
    """

    course_code: str = Field(min_length=1, examples=["CS201"])
    units: list[str] = Field(default_factory=list, examples=[["1", "2", "3"]])
    topic: Optional[str] = Field(default=None)
    current_only: bool = True


class NotesReadResult(_Base):
    """Verified output of the notes/materials read.

    Every count here is *computed from the store*, never supplied by a caller.
    That is the point: these are the numbers a prompt might otherwise assert,
    and they are produced where the data lives.

    Attributes:
        course_code: Echo of the query.
        course_name: From the store header.
        units_declared: Units the course header declares.
        units_requested: Units the caller asked for; empty means all.
        units_covered: Requested units that have at least one note.
        units_missing: Requested units with no notes at all. A coverage gap.
        notes: The matching notes, filtered by unit and topic.
        note_count: How many notes matched.
        version_total: Total versions across matched notes, current and historic.
        tags: Sorted union of tags across matched notes.
        source_files: Seed files backing this read, for the audit trail.
    """

    course_code: str
    course_name: str
    units_declared: list[str] = Field(default_factory=list)
    units_requested: list[str] = Field(default_factory=list)
    units_covered: list[str] = Field(default_factory=list)
    units_missing: list[str] = Field(default_factory=list)
    notes: list[NoteMaterial] = Field(default_factory=list)
    note_count: int = 0
    version_total: int = 0
    tags: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)

    def unit_coverage(self) -> float:
        """Fraction of requested units that have notes, in [0, 1].

        Returns 1.0 when nothing was requested, because there is nothing to
        have missed. Returns 0.0 when units were requested and none are covered.
        """
        if not self.units_requested:
            return 1.0
        return round(len(self.units_covered) / len(self.units_requested), 4)

    def current_texts(self) -> dict[str, str]:
        """Map note_id to its current version's text, for downstream retrieval.

        Lab 4 embeds exactly this mapping.
        """
        if not self.notes:
            return {}
        return {note.note_id: note.current_version.content for note in self.notes}

    def notes_by_unit(self) -> dict[str, int]:
        """How many notes each covered unit has.

        Computed here, from the notes this read actually returned, rather than
        by a caller counting them. Lab 2's acceptance criterion is that key
        numbers always go through tools, and a per-unit count assembled
        downstream would be a key number assembled downstream.

        A declared unit with no notes is included with a count of 0 rather than
        omitted, so the distinction between "no notes" and "unit not in the
        store at all" survives into whatever consumes this. Units with notes
        but no declared unit cannot occur -- `units_declared` comes from the
        same read.

        Returns:
            Unit id to note count, sorted by unit. Includes every requested or
            declared unit, so an empty unit is visible as `0`.
        """
        counts: dict[str, int] = {unit: 0 for unit in self.units_declared}
        for unit in self.units_requested:
            counts.setdefault(unit, 0)
        for note in self.notes:
            counts[note.unit] = counts.get(note.unit, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: _unit_sort_key(item[0])))


def _unit_sort_key(unit: str) -> tuple[int, int | str]:
    """Sort unit ids numerically where possible, then lexically.

    Plain string sorting puts "10" before "2", which would render a coverage
    table in an order no syllabus uses. Non-numeric ids (a course that labels
    its units by topic) sort after numeric ones, alphabetically.

    Args:
        unit: A unit id.

    Returns:
        A sort key: `(0, number)` for a numeric unit, `(1, text)` otherwise.
    """
    try:
        return (0, int(unit))
    except ValueError:
        return (1, unit)


def register_read_notes_store(registry: ToolRegistry) -> ToolRegistry:
    """Register `read_notes_store` on a registry.

    Args:
        registry: The registry to add to.

    Returns:
        The same registry, for chaining.
    """

    @registry.tool(
        "read_notes_store",
        summary=(
            "Read 'the notes/materials store' for one course. Use this to look "
            "up lecture notes, slides and reference material, their units and "
            "their version history. Call this instead of recalling any note "
            "text or unit count from memory."
        ),
        input_model=NotesQuery,
        output_model=NotesReadResult,
        doc_ref=f"{DOC_REF} pseudocode a = tool.read('the notes/materials store')",
    )
    def read_notes_store(query: NotesQuery, ctx: ToolContext) -> NotesReadResult:
        """Read and summarise the notes/materials store.

        Args:
            query: Which course, units and topic to read.
            ctx: Tool context; unused here, but present for interface parity.

        Returns:
            A validated `NotesReadResult` whose counts are computed from what
            the connector returned.

        Raises:
            ConnectorError: If the connector refuses the read -- an unknown
                course, a bad argument, or a role without `notes:read`. The
                tool runner turns it into a failed `ToolResult`; see the module
                docstring for why this is the tools' one sanctioned way of
                failing.
        """
        result = connector().read(
            "read_notes_store", {"course_code": query.course_code}
        )

        declared = list(result.declared_units)
        requested = [str(u) for u in query.units] or declared

        matched = list(result.notes)
        if query.units:
            wanted = {str(u) for u in query.units}
            matched = [note for note in matched if note.unit in wanted]
        if query.topic:
            needle = query.topic.lower()
            matched = [
                note
                for note in matched
                if needle in note.title.lower() or needle in note.topic.lower()
            ]

        covered = sorted(
            {note.unit for note in matched if note.unit in set(requested)},
            key=lambda u: (len(u), u),
        )
        missing = [u for u in requested if u not in set(covered)]
        tag_union = sorted({tag for note in matched for tag in note.tags})

        return NotesReadResult(
            course_code=query.course_code,
            course_name=result.title,
            units_declared=sorted(declared, key=lambda u: (len(u), u)),
            units_requested=sorted(requested, key=lambda u: (len(u), u)),
            units_covered=covered,
            units_missing=missing,
            notes=matched,
            note_count=len(matched),
            version_total=sum(len(note.versions) for note in matched),
            tags=tag_union,
            source_files=list(result.provenance.sources),
        )

    return registry


# ==========================================================================
# Tool 2 -- read('assignment & rubric definitions')
# ==========================================================================


class AssignmentQuery(_Base):
    """Arguments for `read_assignment_rubrics`.

    Attributes:
        course_code: Which course's definitions to read.
        assignment_ids: Restrict to these ids. Empty means every definition.
    """

    course_code: str = Field(min_length=1, examples=["DBMS402"])
    assignment_ids: list[str] = Field(default_factory=list)


class AssignmentReadResult(_Base):
    """Verified output of the assignment/rubric read.

    Attributes:
        course_code: Echo of the query.
        assignments: The matching definitions.
        found_ids: Requested ids that exist.
        missing_ids: Requested ids that do not exist.
        total_available_marks: Sum of `max_marks` across `assignments`. This is
            the number a prompt is most likely to get wrong.
        marks_by_assignment: `assignment_id -> max_marks`. Per-assignment
            values, so a prompt claiming "ASG-02 is worth 50" is directly
            checkable against a tool-verified number.
        rubric_problems: Flattened arithmetic problems found by
            `AssignmentDefinition.check_rubric`.
        faculty_approved: Whether every returned definition names course
            faculty as approver.
        source_files: Seed files backing this read.
    """

    course_code: str
    assignments: list[AssignmentDefinition] = Field(default_factory=list)
    found_ids: list[str] = Field(default_factory=list)
    missing_ids: list[str] = Field(default_factory=list)
    total_available_marks: float = 0.0
    marks_by_assignment: dict[str, float] = Field(default_factory=dict)
    rubric_problems: list[str] = Field(default_factory=list)
    faculty_approved: bool = True
    source_files: list[str] = Field(default_factory=list)

    def criterion_count(self) -> int:
        """Total rubric criteria across the returned definitions."""
        return sum(len(a.rubric) for a in self.assignments)


def register_read_assignment_rubrics(registry: ToolRegistry) -> ToolRegistry:
    """Register `read_assignment_rubrics` on a registry.

    Args:
        registry: The registry to add to.

    Returns:
        The same registry, for chaining.
    """

    @registry.tool(
        "read_assignment_rubrics",
        summary=(
            "Read 'assignment & rubric definitions' for one course: briefs, due "
            "dates, maximum marks and per-criterion rubrics. Call this instead "
            "of recalling any mark value, due date or rubric total from memory."
        ),
        input_model=AssignmentQuery,
        output_model=AssignmentReadResult,
        doc_ref=f"{DOC_REF} pseudocode b = tool.read('assignment & rubric definitions')",
    )
    def read_assignment_rubrics(
        query: AssignmentQuery, ctx: ToolContext
    ) -> AssignmentReadResult:
        """Read and check the assignment & rubric definitions.

        Args:
            query: Which course and which assignment ids to read.
            ctx: Tool context; unused, present for interface parity.

        Returns:
            A validated `AssignmentReadResult`.

        Raises:
            ConnectorError: If the connector refuses the read. Surfaced as a
                failed `ToolResult`, not a crash.
        """
        result = connector().read(
            "read_assignment_rubrics", {"course_code": query.course_code}
        )
        everything = list(result.assignments)

        matched = list(everything)
        if query.assignment_ids:
            wanted = {a.strip() for a in query.assignment_ids}
            matched = [a for a in matched if a.assignment_id in wanted]

        found = [a.assignment_id for a in matched]
        missing = [
            requested
            for requested in query.assignment_ids
            if requested.strip() not in set(found)
        ]

        problems: list[str] = []
        for definition in matched:
            problems.extend(
                f"{definition.assignment_id}: {problem}"
                for problem in definition.check_rubric()
            )

        return AssignmentReadResult(
            course_code=query.course_code,
            assignments=matched,
            found_ids=found,
            missing_ids=missing,
            total_available_marks=round(
                sum(a.max_marks for a in matched), 4
            ),
            marks_by_assignment={a.assignment_id: a.max_marks for a in matched},
            rubric_problems=problems,
            faculty_approved=all(
                a.approved_by == HUMAN_AUTHORITY for a in matched
            ),
            source_files=list(result.provenance.sources),
        )

    return registry


# ==========================================================================
# Tool 3 -- read('submission records')
#
# Added by Lab 4. Document 2.7 names exactly three connector reads and 2.10
# Lab 2 only needed the first two, so the third lands here. It is the input the
# document calls "history of prior assignment submissions".
# ==========================================================================


class SubmissionHistoryQuery(_Base):
    """Arguments for `read_submission_records`.

    Attributes:
        course_code: Which course's history to read.
        assignment_ids: Restrict to these assignments. Empty means every
            assignment with history. Lab 4's similarity search compares a
            candidate against one assignment's history: comparing a linked-list
            answer against a normalisation answer produces a number that means
            nothing.
        statuses: Restrict to these collection statuses. Empty means all.
    """

    course_code: str = Field(min_length=1, examples=["CS201"])
    assignment_ids: list[str] = Field(default_factory=list)
    statuses: list[SubmissionStatus] = Field(default_factory=list)


class SubmissionHistoryResult(_Base):
    """Verified output of the submission-records read.

    Attributes:
        course_code: Echo of the query.
        submissions: The matching records, in store order.
        found_ids: Submission ids returned.
        missing_ids: Requested assignment ids with no history at all. A
            coverage gap the caller must notice rather than read as "no
            submissions exist", which is a different claim.
        assignment_ids: Assignments represented in the returned history.
        total_available: How many records matched.
        on_time_count: Records that are not late. Doc 2.7 check 1.
        late_count: Records past their due date. Doc 2.7 check 1, computed here
            because `status` and `late_by_seconds` are derived by the store.
        previously_scored_count: Records that already carry a
            `similarity_score`, i.e. a prior term's check exists. A score that
            is absent means "not yet checked" and is deliberately not counted
            as zero.
        lateness_by_submission: `submission_id -> late_by_seconds`, so a
            prompt claiming a submission was on time is directly checkable.
        source_files: Seed files backing this read.
    """

    course_code: str
    submissions: list[SubmissionRecord] = Field(default_factory=list)
    found_ids: list[str] = Field(default_factory=list)
    missing_ids: list[str] = Field(default_factory=list)
    assignment_ids: list[str] = Field(default_factory=list)
    total_available: int = 0
    on_time_count: int = 0
    late_count: int = 0
    previously_scored_count: int = 0
    lateness_by_submission: dict[str, int] = Field(default_factory=dict)
    source_files: list[str] = Field(default_factory=list)


def register_read_submission_records(registry: ToolRegistry) -> ToolRegistry:
    """Register `read_submission_records` on a registry.

    Args:
        registry: The registry to add to.

    Returns:
        The same registry, for chaining.
    """

    @registry.tool(
        "read_submission_records",
        summary=(
            "Read 'submission records': the history of prior assignment "
            "submissions, with each record's collected timestamp and the "
            "lateness the store derived from it. Call this to get comparable "
            "prior work, or to check whether a submission was late. Never "
            "recall a lateness or a submission count from memory."
        ),
        input_model=SubmissionHistoryQuery,
        output_model=SubmissionHistoryResult,
        doc_ref=f"{DOC_REF} / 2.7 read('submission records'); 2.10 Lab 4 prior = retrieve(history, ...)",
    )
    def read_submission_records(
        query: SubmissionHistoryQuery, ctx: ToolContext
    ) -> SubmissionHistoryResult:
        """Read the history of prior submissions.

        Args:
            query: Which course, assignments and statuses to read.
            ctx: Tool context; unused, present for interface parity.

        Returns:
            A validated `SubmissionHistoryResult` whose lateness counts are
            computed from the records the store returned.

        Raises:
            ConnectorError: If the connector refuses the read. Surfaced as a
                failed `ToolResult`, not a crash.
        """
        result = connector().read(
            "read_submission_records", {"course_code": query.course_code}
        )
        everything = list(result.submissions)

        matched = list(everything)
        if query.assignment_ids:
            wanted = {a.strip() for a in query.assignment_ids}
            matched = [r for r in matched if r.assignment_id in wanted]
        if query.statuses:
            wanted_status = set(query.statuses)
            matched = [r for r in matched if r.status in wanted_status]

        represented = {r.assignment_id for r in matched}
        missing = [
            requested
            for requested in query.assignment_ids
            if requested.strip() not in represented
        ]

        return SubmissionHistoryResult(
            course_code=query.course_code,
            submissions=matched,
            found_ids=[r.submission_id for r in matched],
            missing_ids=missing,
            assignment_ids=sorted(represented),
            total_available=len(matched),
            on_time_count=sum(1 for r in matched if not r.is_late),
            late_count=sum(1 for r in matched if r.is_late),
            previously_scored_count=sum(
                1 for r in matched if r.similarity_score is not None
            ),
            lateness_by_submission={
                r.submission_id: r.late_by_seconds for r in matched
            },
            source_files=list(result.provenance.sources),
        )

    return registry


__all__ = [
    "AssignmentQuery",
    "AssignmentReadResult",
    "NotesQuery",
    "NotesReadResult",
    "SubmissionHistoryQuery",
    "SubmissionHistoryResult",
    "register_read_assignment_rubrics",
    "register_read_notes_store",
    "register_read_submission_records",
]