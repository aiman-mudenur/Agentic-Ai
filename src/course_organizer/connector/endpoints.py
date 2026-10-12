"""The endpoints themselves: the document's three reads, plus one it implies.

Doc 2.7 gives the connector's surface verbatim, and this module implements
exactly that block:

    connector.expose:
        read('the notes/materials store')
        read('assignment & rubric definitions')
        read('submission records')
    agents.use(connector)   # no agent touches a source directly

Those three are :func:`register_default_endpoints`'s first three
declarations, with the document's phrase kept in each
``EndpointSpec.doc_read`` so the code can be matched to the pseudocode by eye.

There is a **fourth** endpoint, :func:`list_courses`, and it is labelled as
this project's addition rather than presented as a documented requirement,
because it is not one -- :attr:`EndpointSpec.documented` decides that by
matching ``doc_read`` against the document's three phrases, so the label cannot
be asserted into existence. It exists for a concrete reason: the other
three all take a ``course_code``, so an agent that does not know the codes has
no governed way to find them and would guess -- producing a not-found, or
worse, guessing a code that happens to exist. One extra read-only endpoint is
cheaper than letting the model improvise the argument to the three that
matter.

Every endpoint here is **read-only**. Doc 2.10 Lab 5 is a read connector, and
the Governance layer's *"audit log of grades published"* is Lab 6's audit log
over a Lab 7/8 workflow, not a write path invented here. A connector that
could write would need its own approval flow, and building one nobody asked
for would be a worse answer than declining to build it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.connector.auth import (
    ASSIGNMENTS_READ,
    NOTES_READ,
    SUBMISSIONS_READ,
    Principal,
)
from course_organizer.connector.core import EndpointRegistry
from course_organizer.connector.errors import ConnectorNotFoundError
from course_organizer.connector.port import SourceDescriptor
from course_organizer.logging_setup import log_event
from course_organizer.schemas import (
    AssignmentDefinition,
    NoteMaterial,
    SubmissionRecord,
)

__all__ = [
    "AssignmentQuery",
    "AssignmentResult",
    "CourseListResult",
    "NotesQuery",
    "NotesResult",
    "Provenance",
    "SubmissionQuery",
    "SubmissionResult",
    "register_default_endpoints",
]

logger = logging.getLogger(__name__)

#: Doc 2.10 Lab 5's requirement, quoted once so every ``doc_ref`` matches it.
LAB5_REF = "doc 2.10 Lab 5: 'A connector server with typed endpoints and auth; agents call only through it.'"


class _Base(BaseModel):
    """Reject unknown keys so a typo in an argument surfaces at the edge."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Provenance(_Base):
    """Where a result came from and who was allowed to have it.

    Travels **with** the payload rather than being logged beside it, for the
    reason doc 2.3 gives: *"access control per role, version history, audit log
    of grades published."* An answer that cannot say who asked and from where
    is not evidence, and a provenance block that can be dropped in transit
    does not travel. Note it carries no credential -- only the source's
    identity, the caller's subject and their role.

    Attributes:
        endpoint: Which connector endpoint produced this.
        source: The adapter's name, e.g. ``local_seed``. Comparing this field
            across two runs is how a source swap is verified.
        source_transport: What that adapter actually talks to.
        provenance: The adapter's own path string, e.g. ``data/seed``.
        fetched_by: The authenticated caller's subject.
        role: The role that authorised the call.
        fetched_at: When the adapter returned.
        sources: The concrete locations this read came from, in the source's own
            vocabulary -- ``data/seed/CS201/notes.json``, ``classroom:...``,
            ``in-memory://ZZ999/notes``. May be empty if the source cannot say,
            which is visibly incomplete rather than quietly wrong. This is what
            Lab 2's tools used to assemble themselves by naming a seed file
            directly, before the tools went behind this boundary.
    """

    endpoint: str
    source: str
    source_transport: str
    provenance: str
    fetched_by: str
    role: str
    fetched_at: datetime
    sources: list[str] = Field(default_factory=list)


def _provenance(
    endpoint: str,
    principal: Principal,
    descriptor: SourceDescriptor,
    *,
    kind: str = "",
    course_code: Optional[str] = None,
    adapter: Any = None,
) -> Provenance:
    """Build the provenance block for one call.

    Args:
        endpoint: The endpoint name.
        principal: The authenticated caller.
        descriptor: The source's self-description.
        kind: The connector's vocabulary for what was read, e.g. ``"notes"``.
            Passed to :meth:`SourceAdapter.backing` so the source can name its
            own concrete locations.
        course_code: The course read, when the endpoint is per-course.
        adapter: The source adapter, asked for its ``backing`` locations. May be
            None or may not implement ``backing``; either way the read
            succeeds. Provenance is evidence *about* a read, so it must never be
            the reason a read fails.

    Returns:
        A populated :class:`Provenance`.
    """
    return Provenance(
        endpoint=endpoint,
        source=descriptor.name,
        source_transport=descriptor.transport,
        provenance=descriptor.provenance,
        fetched_by=principal.subject,
        role=principal.role,
        fetched_at=datetime.now(tz=timezone.utc),
        sources=_backing(adapter, endpoint, kind, course_code),
    )


def _backing(
    adapter: Any, endpoint: str, kind: str, course_code: Optional[str]
) -> list[str]:
    """Ask a source where a read came from, tolerating every failure mode.

    Returns an empty list if there is no adapter, if the adapter predates
    :meth:`SourceAdapter.backing`, or if calling it raises. All three are
    logged at debug level rather than propagated.

    Args:
        adapter: The source adapter, or None.
        endpoint: For the log line only.
        kind: The connector's vocabulary for what was read.
        course_code: The course read, or None.

    Returns:
        Zero or more locations, in the source's own vocabulary.
    """
    getter = getattr(adapter, "backing", None)
    if getter is None:
        return []
    try:
        return list(getter(kind, course_code))
    except Exception as exc:  # noqa: BLE001 - provenance must not fail a read
        log_event(
            logger,
            "connector.provenance.backing_failed",
            endpoint=endpoint,
            error=f"{type(exc).__name__}: {exc}",
        )
        return []


def _declared_units(header: dict) -> list[str]:
    """The unit ids a source declares for a course, sorted, de-duplicated.

    A source's notes header is allowed to describe its units either as bare
    strings or as ``{"unit": ..., "title": ...}`` mappings -- the seed corpus
    uses the second, and the in-memory and Google adapters were written to
    match it. Both are accepted, because a connector whose *own* header format
    is ambiguous is a connector that normalises nothing.

    An earlier version of this endpoint did ``str(u)`` over the raw list, which
    turned the seed corpus's five unit mappings into five stringified dicts and
    reported them as the course's units. ``declared_units`` was garbage and the
    smoke test's output looked plausible, which is the worst combination.

    Args:
        header: The source's notes header.

    Returns:
        Sorted, de-duplicated unit ids.
    """
    out: list[str] = []
    for entry in header.get("units") or []:
        if isinstance(entry, dict):
            unit = str(entry.get("unit", "")).strip()
        else:
            unit = str(entry).strip()
        if unit and unit not in out:
            out.append(unit)
    return sorted(out, key=_unit_sort_key)


def _course_title(header: dict[str, Any], fallback: str) -> str:
    """The course's display name from a notes header.

    Two key names are in play and the connector accepts both. The seed corpus's
    ``notes.json`` header says ``course_name`` (``"Data Structures"``); the
    in-memory adapter originally said ``title``. An earlier version of this
    endpoint read **only** ``title``, so every real read silently fell back to
    the bare course code -- ``"CS201"`` instead of ``"Data Structures"`` -- and
    because the fallback is a perfectly plausible string, nothing looked wrong.
    ``course_organizer.tools.compute`` builds its ground facts from this field,
    so the wrong value would have propagated into Lab 2's numbers as a cosmetic
    error nobody would catch.

    Args:
        header: The source's notes header.
        fallback: Used when the header names the course in neither form --
            normally the course code, which is always better than "".

    Returns:
        The display name, or ``fallback``.
    """
    for key in ("course_name", "title", "name"):
        value = header.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _unit_sort_key(unit: str) -> tuple[int, int | str]:
    """Sort ``"1" .. "10"`` numerically when possible, lexically otherwise.

    The seed corpus names units with bare numbers, and a lexical sort would put
    ``"10"`` between ``"1"`` and ``"2"``. Units that are not numbers fall back
    to a string sort after the numeric ones, so a mixed corpus still orders
    predictably.
    """
    try:
        return (0, int(unit))
    except ValueError:
        return (1, unit)


# ---------------------------------------------------------------------------
# Endpoint 0 (Lab 5 addition): which courses exist
# ---------------------------------------------------------------------------
class CourseListResult(_Base):
    """``list_courses`` -- the governed way to learn a valid ``course_code``.

    Attributes:
        provenance: Who asked, of which source, when.
        course_codes: Every course the current source can serve, sorted.
        source_count: How many, repeated as a field so a client does not have
            to import ``len`` to check a length.
    """

    provenance: Provenance
    course_codes: list[str]
    source_count: int = Field(ge=0)

    def describe(self) -> str:
        """One line for the lab report."""
        return (
            f"{self.source_count} course(s) from {self.provenance.source}: "
            f"{', '.join(self.course_codes) or '(none)'}"
        )


# ---------------------------------------------------------------------------
# Endpoint 1: read('the notes/materials store')
# ---------------------------------------------------------------------------
class NotesQuery(_Base):
    """Arguments for ``read_notes_store``.

    Attributes:
        course_code: The course to read, e.g. ``CS201``.
        unit: Optional unit filter, e.g. ``"Unit 1"``. Filtering happens in
            the connector, not in the agent, so the amount of data crossing the
            boundary is bounded by what was asked for. Doc 2.6 tags notes by
            unit/topic, so this is a field that exists rather than a guess.
    """

    course_code: str = Field(min_length=1, description="Course code, e.g. CS201.")
    unit: Optional[str] = Field(
        default=None,
        description=(
            "Optional unit filter. Use the exact unit id the course declares, "
            "which list_courses and the unfiltered read reveal -- for the seed "
            "corpus those are '1'..'5', not 'Unit 1'."
        ),
    )


class NotesResult(_Base):
    """The notes/materials store for one course.

    Doc 2.6: *"Lecture notes, slides, and reference material tagged by
    unit/topic and version."* Every field below is either that entity or a fact
    about it -- nothing here is inferred by the connector.

    Attributes:
        provenance: Who asked, of which source, when.
        course_code: Echoed back, so a result can be filed without its request.
        title: The course's title, as the source spells it.
        declared_units: Every unit the source declares for this course. May be
            wider than ``units_present`` when a declared unit has no notes, and
            saying so is more useful than silently narrowing the list.
        units_present: Units that actually have at least one note after the
            unit filter was applied.
        notes: The note materials.
        note_count: ``len(notes)``, for a client that should not import ``len``.
        total_versions: Sum of every note's version count. Doc 2.3's *"keep
            clear version history"* is visible in this number, which is how a
            reviewer can see that history survived the connector.
        unit_filter: The unit filter that was applied, or None.
    """

    provenance: Provenance
    course_code: str
    title: str
    declared_units: list[str]
    units_present: list[str]
    notes: list[NoteMaterial]
    note_count: int = Field(ge=0)
    total_versions: int = Field(ge=0)
    unit_filter: Optional[str] = None

    def describe(self) -> str:
        """One line for the lab report."""
        return (
            f"{self.course_code}: {self.note_count} note(s), "
            f"{self.total_versions} version(s), units "
            f"{', '.join(self.units_present) or '(none)'}"
        )


# ---------------------------------------------------------------------------
# Endpoint 2: read('assignment & rubric definitions')
# ---------------------------------------------------------------------------
class AssignmentQuery(_Base):
    """Arguments for ``read_assignment_rubrics``.

    Attributes:
        course_code: The course to read, e.g. ``CS201``.
    """

    course_code: str = Field(min_length=1, description="Course code, e.g. CS201.")


class AssignmentResult(_Base):
    """Assignment briefs and their rubrics, per doc 2.6.

    Doc 2.6: *"Assignment brief, due date, marks, and rubric."* The connector
    returns those definitions and no judgement about them; rubric-aligned
    grading is a later lab.

    Attributes:
        provenance: Who asked, of which source, when.
        course_code: Echoed back.
        assignments: The definitions, sorted by assignment id.
        assignment_count: ``len(assignments)``.
        rubric_criteria: Total criteria across every assignment. Two courses
            with the same assignment count can carry very different rubrics,
            and this is the number that tells them apart.
    """

    provenance: Provenance
    course_code: str
    assignments: list[AssignmentDefinition]
    assignment_count: int = Field(ge=0)
    rubric_criteria: int = Field(ge=0)

    def describe(self) -> str:
        """One line for the lab report."""
        return (
            f"{self.course_code}: {self.assignment_count} assignment(s), "
            f"{self.rubric_criteria} rubric criterion/criteria"
        )


# ---------------------------------------------------------------------------
# Endpoint 3: read('submission records')
# ---------------------------------------------------------------------------
class SubmissionQuery(_Base):
    """Arguments for ``read_submission_records``.

    Attributes:
        course_code: The course to read, e.g. ``CS201``.
        assignment_id: Optional single-assignment filter.
    """

    course_code: str = Field(min_length=1, description="Course code, e.g. CS201.")
    assignment_id: Optional[str] = Field(
        default=None, description="Optional single-assignment filter."
    )


class SubmissionResult(_Base):
    """The history of prior submissions, per doc 2.6.

    Doc 2.6: *"Student submission, timestamp, and similarity/plagiarism
    score."* The third of those is **absent by design**: doc 2.12 requires the
    system to *"flag for human review, never auto-penalise"*, so no record
    that reaches an agent carries a verdict. ``SubmissionRecord`` has
    ``similarity_score`` and ``similarity_against`` because Lab 4's retriever
    needs somewhere to put a *measurement*; it deliberately has no
    ``is_plagiaristic`` field at all, and
    ``tests/test_lab05_mcp_connector.py`` asserts the connector's wire schema
    does not grow one.

    Attributes:
        provenance: Who asked, of which source, when.
        course_code: Echoed back.
        assignment_id: The filter applied, or None.
        submissions: The records, sorted by submission id.
        submission_count: ``len(submissions)``.
        late_count: How many are late. A fact of collection, not a judgement.
        with_similarity_score: How many already carry a similarity
            measurement. Zero for a source that has never been through Lab 4,
            which is the honest state of a freshly-swapped source.
    """

    provenance: Provenance
    course_code: str
    assignment_id: Optional[str] = None
    submissions: list[SubmissionRecord]
    submission_count: int = Field(ge=0)
    late_count: int = Field(ge=0)
    with_similarity_score: int = Field(ge=0)

    def describe(self) -> str:
        """One line for the lab report."""
        return (
            f"{self.course_code}: {self.submission_count} submission(s), "
            f"{self.late_count} late, "
            f"{self.with_similarity_score} scored for similarity"
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_default_endpoints(registry: EndpointRegistry) -> EndpointRegistry:
    """Register the document's three reads plus ``list_courses``.

    Args:
        registry: The connector to add to.

    Returns:
        The same registry, for chaining.
    """

    @registry.endpoint(
        "list_courses",
        summary=(
            "List the course codes this connector's source can serve. Call "
            "this first when you do not already know a valid course_code; the "
            "other three endpoints all require one."
        ),
        scope=NOTES_READ,
        input_model=_Base,
        output_model=CourseListResult,
        doc_ref=LAB5_REF,
        doc_read="(Lab 5 addition: the three reads below all need a course_code)",
        added_by_lab="Lab 5",
        count_field="course_codes",
    )
    def list_courses(
        _query: _Base, principal: Principal, descriptor: SourceDescriptor
    ) -> CourseListResult:
        """Every course code the current source serves."""
        codes = sorted(registry.adapter.course_codes())
        return CourseListResult(
            provenance=_provenance(
                "list_courses",
                principal,
                descriptor,
                kind="courses",
                # `adapter=` was missing here, so this was the one endpoint whose
                # result could not say where it came from: `_backing` received
                # `None`, returned `[]`, and the provenance looked authoritative
                # while being blank. The three per-course reads all passed it.
                adapter=registry.adapter,
            ),
            course_codes=codes,
            source_count=len(codes),
        )

    @registry.endpoint(
        "read_notes_store",
        summary=(
            "Read 'the notes/materials store' for one course: lecture notes, "
            "slides and reference material, their units and version history. "
            "Call this instead of recalling any note text or unit count from "
            "memory. If you pass `unit`, it must be an exact unit id from the "
            "course (call list_courses, or read without a filter first to see "
            "them); an unknown unit is refused with the real list rather than "
            "returning an empty result."
        ),
        scope=NOTES_READ,
        input_model=NotesQuery,
        output_model=NotesResult,
        doc_ref=LAB5_REF,
        doc_read="read('the notes/materials store')",
        count_field="notes",
    )
    def read_notes_store(
        query: NotesQuery, principal: Principal, descriptor: SourceDescriptor
    ) -> NotesResult:
        """The notes/materials store, optionally narrowed to one unit."""
        header, notes = registry.adapter.read_notes(query.course_code)

        declared = _declared_units(header)
        unfiltered = notes
        if query.unit is not None:
            notes = [n for n in notes if n.unit == query.unit]
            # A filter that matches nothing is not an empty course. Without
            # this, an agent that guessed "Unit 1" instead of "1" would get
            # `note_count: 0` and conclude there is no material -- a false
            # negative that looks exactly like a valid answer. Refusing, and
            # naming the units that do exist, turns a silent wrong turn into a
            # one-glance correction.
            if not notes and unfiltered:
                raise ConnectorNotFoundError(
                    f"course {query.course_code!r} has no unit {query.unit!r}. "
                    f"Its units are {declared}. Note that the seed corpus "
                    f"names units by bare number ('1'), not 'Unit 1'."
                )

        return NotesResult(
            provenance=_provenance(
                "read_notes_store",
                principal,
                descriptor,
                kind="notes",
                course_code=query.course_code,
                adapter=registry.adapter,
            ),
            course_code=query.course_code,
            title=_course_title(header, query.course_code),
            declared_units=declared,
            units_present=sorted({n.unit for n in notes}, key=_unit_sort_key),
            notes=notes,
            note_count=len(notes),
            total_versions=sum(len(n.versions) for n in notes),
            unit_filter=query.unit,
        )

    @registry.endpoint(
        "read_assignment_rubrics",
        summary=(
            "Read 'assignment & rubric definitions' for one course: the brief, "
            "the due date, the marks and the rubric criteria. Call this instead "
            "of recalling an assignment's marks or criteria from memory."
        ),
        scope=ASSIGNMENTS_READ,
        input_model=AssignmentQuery,
        output_model=AssignmentResult,
        doc_ref=LAB5_REF,
        doc_read="read('assignment & rubric definitions')",
        count_field="assignments",
    )
    def read_assignment_rubrics(
        query: AssignmentQuery, principal: Principal, descriptor: SourceDescriptor
    ) -> AssignmentResult:
        """The assignment and rubric definitions for one course."""
        assignments = list(registry.adapter.read_assignments(query.course_code))
        return AssignmentResult(
            provenance=_provenance(
                "read_assignment_rubrics",
                principal,
                descriptor,
                kind="assignments",
                course_code=query.course_code,
                adapter=registry.adapter,
            ),
            course_code=query.course_code,
            assignments=assignments,
            assignment_count=len(assignments),
            rubric_criteria=sum(len(a.rubric) for a in assignments),
        )

    @registry.endpoint(
        "read_submission_records",
        summary=(
            "Read 'submission records' for one course: who submitted what, "
            "when, and whether it was late. This is the cohort's history, so "
            "it is the endpoint the role policy restricts most tightly. Call "
            "this instead of assuming a submission's status or timestamp. If "
            "you pass `assignment_id`, it must be an exact id; an unknown one "
            "is refused with the real list rather than returning an empty "
            "result, because 'no submissions' is a claim a grader might act on."
        ),
        scope=SUBMISSIONS_READ,
        input_model=SubmissionQuery,
        output_model=SubmissionResult,
        doc_ref=LAB5_REF,
        doc_read="read('submission records')",
        count_field="submissions",
    )
    def read_submission_records(
        query: SubmissionQuery, principal: Principal, descriptor: SourceDescriptor
    ) -> SubmissionResult:
        """The history of prior submissions for one course."""
        records = list(registry.adapter.read_submissions(query.course_code))
        if query.assignment_id is not None:
            # Same reasoning as the unit filter above, and it matters more
            # here: "zero submissions" is a statement a grader might act on.
            # A guessed assignment id must not be able to produce one. The
            # available ids are taken from the records already fetched under
            # the caller's own `submissions:read` scope -- the connector
            # deliberately does not make a second, differently-scoped read
            # while serving this one.
            available = sorted({r.assignment_id for r in records})
            records = [r for r in records if r.assignment_id == query.assignment_id]
            if not records and available:
                raise ConnectorNotFoundError(
                    f"course {query.course_code!r} has no submissions for "
                    f"assignment {query.assignment_id!r}. Its submissions cover "
                    f"assignments {available}."
                )
        return SubmissionResult(
            provenance=_provenance(
                "read_submission_records",
                principal,
                descriptor,
                kind="submissions",
                course_code=query.course_code,
                adapter=registry.adapter,
            ),
            course_code=query.course_code,
            assignment_id=query.assignment_id,
            submissions=records,
            submission_count=len(records),
            late_count=sum(1 for r in records if r.is_late),
            with_similarity_score=sum(
                1 for r in records if r.similarity_score is not None
            ),
        )

    return registry
