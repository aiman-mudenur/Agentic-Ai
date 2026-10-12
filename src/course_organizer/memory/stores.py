"""The two stores, and the loader that fills the archive.

Document 2.10, Lab 4:

    "Session state (the current draft) vs stored reference - the
     notes/materials store and the history of past assignment submissions -
     retrieved by tags and by similarity."

So there are two collections, and this module implements both plus the one
path that fills the long-term one.

**This is the only module in the worklet allowed to build a `MemoryRecord`.**
A test in ``tests/test_lab04_memory_retrieval.py`` asserts that by walking
every source file in the package. The assertion matters: a record is where the
layer label and the provenance string live, so a record constructed anywhere
else is a record nobody boundary-checked. Labs 2 and 3 enforce the same
discipline on their own types -- `seed_store.py` is the only module that opens
`data/seed`, and only `skills/` builds a `SkillSpec`.

| Class | Layer | Filled by | Read by |
|-------|-------|-----------|---------|
| :class:`ArchiveStore` | ``ARCHIVE`` | :meth:`ArchiveStore.load_from_tools` | tag and similarity retrievers |
| :class:`RunState` | ``RUN_STATE`` | :meth:`RunState.set_draft` | the no-repeat seed only |

Why the archive is read-only
----------------------------
:class:`ArchiveStore` inherits :meth:`MemoryStore.append` and does **not**
override it, but its :attr:`allowed_provenance` is the set of real source
files for the course. A run-state draft carries the sentinel
:data:`~course_organizer.memory.base.RUN_STATE_PROVENANCE`, which is not a
path, so the provenance check refuses it even if the caller has relabelled its
``layer``. That is the whole boundary, and it is two lines of configuration
rather than a convention.

The three demo students' records come from the tools, not from here
-----------------------------------------------------------------
:func:`draft_to_record` converts a `Draft` into a run-state record, and a
`Draft` may be produced by :mod:`course_organizer.labs.lab02_tool_using_agent`
or typed by a person. Neither is mentioned in this module's docstrings on
purpose: see the AST test ``test_no_another_module_constructs_memory_records``
and its comment in ``tests/test_lab04_memory_retrieval.py``. This package has
no knowledge of any specific course, note, assignment or student.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from course_organizer.logging_setup import log_event
from course_organizer.memory.base import (
    LAYER_PURPOSE,
    RUN_STATE_PROVENANCE,
    MemoryError,
    MemoryLayer,
    MemoryRecord,
    MemoryStore,
)
from course_organizer.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

#: What a run-state record's `kind` is called. One value, so a caller cannot
#: file a draft in the archive by calling it something else -- the layer and
#: provenance checks catch that anyway, but a single kind keeps the audit
#: readable.
KIND_CANDIDATE_DRAFT = "candidate_draft"


def _single_source(sources: list[str], endpoint: str) -> str:
    """Pick the one provenance string an archive record should carry.

    ``Provenance.sources`` is a *list* because a source may legitimately read
    from more than one place -- a Google adapter could fan out across several
    Drive files. But a memory record carries a single ``provenance`` string that
    is also its membership key in the archive's allow-list, so the list has to
    collapse to one value.

    Choosing the first is right for the sources in this project: each of the
    three reads is backed by exactly one location, and the list is ordered by
    the connector. What matters is that the fallback is *loud*. An empty list
    would otherwise be filed under ``""``, which is not in the allow-list, so
    every record would be refused with no explanation -- a silent total failure
    dressed as an empty archive.

    Args:
        sources: The connector-reported locations for one read.
        endpoint: Which endpoint reported them, for the message.

    Returns:
        The first location, or a string that cannot match any allow-list entry.
    """
    if sources:
        return sources[0]
    log_event(
        logger,
        "memory.archive.provenance_unreported",
        endpoint=endpoint,
    )
    return f"(unreported by {endpoint})"

#: The two archive record kinds, named after the two document reads they come
#: from. 2.7: ``read('the notes/materials store')`` and
#: ``read('submission records')``.
KIND_NOTE_MATERIAL = "note_material"
KIND_SUBMISSION = "submission"


# ==========================================================================
# Run state -- the short-term half
# ==========================================================================


class RunState(MemoryStore):
    """Short-term, in-process memory for the current draft.

    Document 2.10, Lab 4, first pseudocode line::

        state.draft = candidate                      # short-term

    What this class deliberately does not have: a ``save``, a ``flush``, a
    ``persist``, or any other method that writes anywhere. There is nothing to
    call if a caller wants to keep a draft after the run, because
    :attr:`MemoryRecord.layer` and :attr:`MemoryRecord.provenance` would both
    have to change, and the only way to change them is to construct a new
    record -- which is a *read* of a source, not a copy of a draft.

    The two methods it does have, :meth:`set_draft` and :meth:`drafts`, exist
    so the no-repeat seed can see what this run is working on, and so a report
    can print it. Neither of them can be mistaken for persistence.

    Attributes:
        layer: Always :attr:`MemoryLayer.RUN_STATE`.
    """

    layer = MemoryLayer.RUN_STATE

    def set_draft(
        self,
        content: str,
        *,
        title: str = "current draft",
        tags: Optional[list[str]] = None,
        meta: Optional[dict] = None,
    ) -> MemoryRecord:
        """Place a candidate draft into run state, replacing any previous one.

        The document's line is ``state.draft = candidate``, an assignment, not
        an append. So this replaces rather than accumulates: run state holds
        *the* draft, and having two of them would make "what is this run
        working on?" ambiguous.

        Args:
            content: The draft text. This is what the similarity retriever
                embeds, so it must be the actual candidate and not a
                description of it.
            title: A short label for reports.
            tags: Optional tags. These are the draft's own tags and are used
                for nothing: run state has no tags to filter against, which is
                precisely why the tag retriever is bound to the archive.
            meta: Optional small facts about the draft.

        Returns:
            The stored `MemoryRecord`, whose `record_id` is what the retrieval
            bundle cites.

        Raises:
            ValueError: If `content` is empty. An empty draft embedded as a
                query vector would return the archive's own ordering and look
                like a confident answer.
        """
        if not content or not content.strip():
            raise ValueError(
                "a run-state draft must have content: an empty draft embeds to "
                "a zero vector, and a zero vector returns the archive's own "
                "order, which reads as a confident answer rather than as 'no "
                "similarity to measure'"
            )
        previous = self.draft()
        if previous is not None:
            # `state.draft = candidate` is an assignment, so the old draft
            # leaves. Dropping it also keeps the layer's fingerprint stable
            # across revisions of the same candidate.
            self._records.pop(previous.record_id, None)

        record = MemoryRecord(
            record_id=f"draft-{uuid.uuid4().hex[:8]}",
            layer=MemoryLayer.RUN_STATE,
            course_code=self.course_code,
            kind=KIND_CANDIDATE_DRAFT,
            title=title,
            content=content,
            tags=list(tags or []),
            meta=dict(meta or {}),
            provenance=RUN_STATE_PROVENANCE,
            created_at=datetime.now(tz=timezone.utc),
        )
        return self.append(record)

    def draft(self) -> Optional[MemoryRecord]:
        """The current draft, or None when nothing has been set.

        Returns:
            The most recent draft record.
        """
        drafts = [r for r in self._records.values() if r.kind == KIND_CANDIDATE_DRAFT]
        if not drafts:
            return None
        return max(drafts, key=lambda r: r.created_at)

    def drafts(self) -> list[MemoryRecord]:
        """Every draft ever set in this run state, oldest first.

        With :meth:`set_draft` replacing rather than appending this is normally
        at most one record. It is a list because a caller may build several
        candidates before choosing, and the audit should show all of them.
        """
        return sorted(
            (r for r in self._records.values() if r.kind == KIND_CANDIDATE_DRAFT),
            key=lambda r: (r.created_at, r.record_id),
        )

    def clear(self) -> None:
        """Drop everything. What a run does when it ends.

        Provided so the "drafts do not outlive the run" claim has a method that
        demonstrates it, and so a test can show the archive is untouched while
        run state is emptied.
        """
        self._records.clear()
        log_event(
            logger,
            "run_state_cleared",
            course_code=self.course_code,
            drafts_discarded=len(self.drafts()),
        )


# ==========================================================================
# The archive -- the long-term half
# ==========================================================================


class ArchiveStore(MemoryStore):
    """Long-term, source-backed memory: notes store plus submission history.

    Document 2.10, Lab 4, second and third pseudocode lines::

        hits  = retrieve(store, tags)                     # long-term
        prior = retrieve(history, embed(candidate), k=5)  # similarity

    Both of those read *this* store, and neither writes to it. Its contents
    arrive through :meth:`load_from_tools`, which calls the document's
    connector reads, so the archive inherits the tool layer's guarantee that
    nothing reaches `data/seed` except ``seed_store.py``.

    Attributes:
        layer: Always :attr:`MemoryLayer.ARCHIVE`.
        _sources: The repo-relative paths backing this archive, as
            :attr:`allowed_provenance`. A record claiming any other provenance
            is refused.
    """

    layer = MemoryLayer.ARCHIVE

    #: The endpoints whose backing locations together form a course's archive
    #: provenance. Every one the archive fills from, and nothing else, so the
    #: allow-list cannot be wider than what was actually read.
    _ARCHIVE_ENDPOINTS = (
        "read_notes_store",
        "read_assignment_rubrics",
        "read_submission_records",
    )

    def __init__(self, *, run_id: str, course_code: str) -> None:
        """Create an empty archive.

        The allowed-provenance set is resolved **lazily**, on first use, rather
        than here. Two reasons, both practical:

        * It needs a connector call to answer, and constructing an archive should
          not perform I/O -- a caller that builds one and never fills it should
          not have read a corpus as a side effect.
        * ``SOURCE_ADAPTER`` is read when the connector is built, so resolving
          eagerly would freeze a decision made before configuration finished
          loading.

        Args:
            run_id: Run id, stamped on the records this store loads.
            course_code: Which course this archive covers.
        """
        super().__init__(run_id=run_id, course_code=course_code)
        self._sources: Optional[frozenset[str]] = None

    def _resolve_sources(self) -> frozenset[str]:
        """Ask the connector which locations back this course.

        This replaces a direct call to ``LocalSeedStore.archive_sources()``,
        which Lab 4 originally made. That call was a second front door to the
        corpus, and doc 2.10 Lab 5 is explicit that there must not be one:
        *"No agent should touch a source directly; a bypass defeats governance
        later."* A memory layer that could name its own allow-list without
        authorisation could also have named a wider one, which would quietly
        weaken the very check it exists to enforce.

        Going through the connector means the allow-list is the union of the
        provenance the connector itself reported for the reads this archive is
        built from. If a source cannot say where it read, it contributes
        nothing and its records are then refused -- visibly incomplete rather
        than quietly permissive.

        Returns:
            The sorted set of allowed provenance strings, cached after the first
            call.
        """
        if self._sources is not None:
            return self._sources

        from course_organizer.tools.readers import connector as _connector

        client = _connector()
        found: set[str] = set()
        for endpoint in self._ARCHIVE_ENDPOINTS:
            try:
                result = client.read(endpoint, {"course_code": self.course_code})
            except Exception as exc:  # noqa: BLE001 - an empty allow-list refuses
                log_event(
                    logger,
                    "memory.archive.source_unresolved",
                    endpoint=endpoint,
                    course_code=self.course_code,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            found.update(getattr(result.provenance, "sources", []) or [])
        self._sources = frozenset(found)
        return self._sources

    @property
    def allowed_provenance(self) -> frozenset[str]:
        """The real source locations for this course.

        Anything else is refused, which is what keeps a run-state draft out
        even if a caller relabels its layer.
        """
        return self._resolve_sources()

    @property
    def sources(self) -> list[str]:
        """The source locations backing this archive, sorted."""
        return sorted(self._resolve_sources())

    # -- filling ------------------------------------------------------------

    def load_from_tools(self, tools: ToolRegistry) -> "ArchiveStore":
        """Fill the archive by calling the document's connector reads.

        Uses `read_notes_store` and `read_submission_records`. Deliberately
        *not* :meth:`~course_organizer.tools.base.ToolRegistry.invoke` on
        `compute_scope_validation`: the archive is reference material, not a
        verdict, and a validation result would put a check outcome into
        long-term memory where a later retriever might read it as a fact about
        the content.

        Both calls are recorded on the *tool* ledger at depth 0, so the audit
        shows exactly which reads built the archive.

        Args:
            tools: The tool registry to read through. Must carry the two
                readers.

        Returns:
            This store, for chaining.

        Raises:
            MemoryError: Propagated from a failed read. A half-loaded archive
                is worse than an empty one, because the retriever would report
                a small corpus as if it were the truth.
        """
        notes_result = tools.invoke(
            "read_notes_store",
            {"course_code": self.course_code, "current_only": False},
        )
        if not notes_result.ok:
            raise MemoryError(
                f"archive load failed: read_notes_store -> {notes_result.error}"
            )
        notes = notes_result.value
        # The provenance comes from the read that produced the data, not from a
        # separate trip to the store. That was Lab 4's original arrangement and
        # it had two defects: a second front door to the corpus, and a chance to
        # disagree with the read it was labelling. Now there is exactly one
        # source of that answer.
        notes_provenance = _single_source(notes.source_files, "read_notes_store")

        for note in notes.notes:
            for version in note.versions:
                self.append(
                    MemoryRecord(
                        record_id=f"{note.note_id}@v{version.version}",
                        layer=MemoryLayer.ARCHIVE,
                        course_code=self.course_code,
                        kind=KIND_NOTE_MATERIAL,
                        title=f"{note.title} (v{version.version})",
                        content=version.content,
                        # Unit and kind become tags, because the document says
                        # notes are "tagged by unit/topic and version" and a
                        # tag retriever is the only way to use that. The
                        # store's own tags come first so a human reading the
                        # output sees them where they expect.
                        tags=_normalise_tags(
                            [*note.tags, f"unit:{note.unit}", note.kind.value]
                        ),
                        meta={
                            "note_id": note.note_id,
                            "version": version.version,
                            "unit": note.unit,
                            "topic": note.topic,
                            "artifact_kind": note.kind.value,
                            "author": version.author,
                            "content_sha256": version.content_sha256,
                        },
                        provenance=notes_provenance,
                        created_at=version.created_at,
                        is_current=version.version == note.current_version.version,
                    )
                )

        submissions_result = tools.invoke(
            "read_submission_records", {"course_code": self.course_code}
        )
        if not submissions_result.ok:
            raise MemoryError(
                "archive load failed: read_submission_records -> "
                f"{submissions_result.error}"
            )
        submissions = submissions_result.value
        submissions_provenance = _single_source(
            submissions.source_files, "read_submission_records"
        )

        for record in submissions.submissions:
            # The lateness here is the *store's* number, carried through from
            # the tool result. A retriever must not recompute it: the store
            # derived it from the immutable timestamp, and a second derivation
            # would be a second chance to disagree.
            self.append(
                MemoryRecord(
                    record_id=record.submission_id,
                    layer=MemoryLayer.ARCHIVE,
                    course_code=self.course_code,
                    kind=KIND_SUBMISSION,
                    title=f"{record.assignment_id} / {record.student_name}",
                    content=record.content,
                    tags=_normalise_tags(
                        [
                            f"assignment:{record.assignment_id}",
                            f"status:{record.status.value}",
                            "late" if record.is_late else "on_time",
                        ]
                    ),
                    meta={
                        "assignment_id": record.assignment_id,
                        "student_id": record.student_id,
                        "submitted_at": record.submitted_at.isoformat(),
                        "late_by_seconds": record.late_by_seconds,
                        "similarity_score": record.similarity_score,
                        "similarity_against": list(record.similarity_against),
                    },
                    provenance=submissions_provenance,
                    # The document's guardrail is "never alter a submission
                    # timestamp", so the record's own timestamp is the
                    # collected one and not the moment it was read.
                    created_at=record.submitted_at,
                )
            )

        log_event(
            logger,
            "archive_loaded",
            course_code=self.course_code,
            records=len(self),
            fingerprint=self.fingerprint(),
            sources=self.sources,
        )
        return self
    # -- the two retrieval surfaces the document names ---------------------

    def notes(self) -> list[MemoryRecord]:
        """Archive records from the notes/materials store.

        Raises:
            MemoryError: If the archive was never loaded.
        """
        return self._by_kind(KIND_NOTE_MATERIAL)

    def submissions(self) -> list[MemoryRecord]:
        """Archive records from the submission history.

        Raises:
            MemoryError: If the archive was never loaded.
        """
        return self._by_kind(KIND_SUBMISSION)

    def _by_kind(self, kind: str) -> list[MemoryRecord]:
        """Every record of one kind.

        Raises:
            MemoryError: If the archive holds no records at all, which means
                `load_from_tools` was never called. Returning an empty list
                instead would let a retriever report "nothing matches" when the
                truth is "nothing was ever loaded".
        """
        if not self._records:
            raise MemoryError(
                f"archive for {self.course_code!r} is empty. Call "
                "load_from_tools(tools) before reading it; an empty archive "
                "and an unsearched one look identical to a retriever."
            )
        return [r for r in self.records() if r.kind == kind]

    def current_notes(self) -> list[MemoryRecord]:
        """Only the authoritative version of each note.

        Doc 2.6: notes are versioned. Doc 2.12: keep a clear version history.
        Both hold here: historic versions stay in the archive and stay
        retrievable, and a caller that wants the answer students currently see
        asks for this.
        """
        return [r for r in self.notes() if r.is_current]

    def describe(self) -> str:
        """A readable summary of what this archive holds, and where from."""
        lines = [
            f"archive: {self.course_code}  ({len(self)} records, "
            f"fingerprint {self.fingerprint()})",
        ]
        if not self._records:
            lines.append("  (empty - load_from_tools has not run)")
            return "\n".join(lines)
        everything = self.records()
        for kind in sorted({r.kind for r in everything}):
            subset = [r for r in everything if r.kind == kind]
            current = sum(1 for r in subset if r.is_current)
            lines.append(
                f"  {kind:<20} {len(subset):>3} records "
                f"({current} current)  "
                f"provenance={sorted({r.provenance for r in subset})}"
            )
        for path in self.sources:
            lines.append(f"  source: {path}")
        return "\n".join(lines)


# ==========================================================================
# Helpers
# ==========================================================================


def _normalise_tags(tags: list[str]) -> list[str]:
    """Lowercase, de-duplicate and sort a tag list.

    Tag matching in this lab is exact, so normalisation happens once on the way
    in rather than at every comparison. A tag the store spelled `Linked-List`
    and a query that spelled it `linked-list` are the same tag, and finding
    that out at query time would mean every retriever remembered to lowercase.

    Args:
        tags: Raw tags from a source.

    Returns:
        Sorted, de-duplicated, lowercased tags.
    """
    return sorted({t.strip().lower() for t in tags if t and t.strip()})


def draft_to_record(
    *,
    course_code: str,
    content: str,
    title: str = "current draft",
    tags: Optional[list[str]] = None,
    meta: Optional[dict] = None,
) -> MemoryRecord:
    """Build a run-state `MemoryRecord` without opening a store.

    Useful when a caller wants to *hand* a candidate to the no-repeat seed
    rather than to a `RunState`. It is in this module because this is the
    module allowed to construct records, and a run-state record is a thing that
    only the short-term half has any business making.

    Args:
        course_code: Which course the draft is for.
        content: The candidate text.
        title: A short label.
        tags: Optional tags.
        meta: Optional small facts.

    Returns:
        A `MemoryRecord` in the `RUN_STATE` layer with the run-state
        provenance sentinel.

    Raises:
        ValueError: If `content` is empty. See `RunState.set_draft`.
    """
    if not content or not content.strip():
        raise ValueError(
            "a run-state draft must have content: an empty draft embeds to a "
            "zero vector, and a zero vector returns the archive's own order, "
            "which reads as a confident answer rather than as 'no similarity "
            "to measure'"
        )
    return MemoryRecord(
        record_id=f"draft-{uuid.uuid4().hex[:8]}",
        layer=MemoryLayer.RUN_STATE,
        course_code=course_code,
        kind=KIND_CANDIDATE_DRAFT,
        title=title,
        content=content,
        tags=_normalise_tags(list(tags or [])),
        meta=dict(meta or {}),
        provenance=RUN_STATE_PROVENANCE,
        created_at=datetime.now(tz=timezone.utc),
    )


__all__ = [
    "KIND_CANDIDATE_DRAFT",
    "KIND_NOTE_MATERIAL",
    "KIND_SUBMISSION",
    "LAYER_PURPOSE",
    "ArchiveStore",
    "MemoryError",
    "RunState",
    "draft_to_record",
]
