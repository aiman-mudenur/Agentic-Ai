"""The swappable port: what a data source must be able to do.

This module is the whole of the "swapping a source needs no agent change"
clause in doc 2.10 Lab 5's acceptance criteria, expressed as code:

    *"All data access goes through the connector; swapping a source needs no
    agent change."*

The first clause is about the **callers** and is enforced by an AST test in
``tests/test_lab05_mcp_connector.py``: only this package may name a source
adapter. The second clause is about this file. An agent imports
:class:`~course_organizer.connector.client.ConnectorClient`; it never names
:class:`SourceAdapter`, never sees :class:`SourceDescriptor`, and cannot tell
:class:`~course_organizer.connector.adapters.LocalSeedAdapter` from
:class:`~course_organizer.connector.adapters.GoogleWorkspaceAdapter` except
by reading the provenance string the connector returns alongside the data.

Four methods, one per concern, no I/O types in the signature. ``Path``,
``dict[str, Any]`` and ``requests.Session`` are all implementation details of
*some* source; a port that leaks them is a port every adapter has to imitate.

The document fixes the three reads (doc 2.7, ``connector.expose``):

    connector.expose:
        read('the notes/materials store')
        read('assignment & rubric definitions')
        read('submission records')
    agents.use(connector)   # no agent touches a source directly

so the port has three read methods and they are not negotiable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.logging_setup import log_event
from course_organizer.schemas import (
    AssignmentDefinition,
    NoteMaterial,
    SubmissionRecord,
)

__all__ = [
    "SourceAdapter",
    "SourceDescriptor",
    "SourceHealth",
    "NotesPage",
]

logger = logging.getLogger(__name__)

#: The three reads, named exactly as doc 2.7 names them. The port takes a
#: ``course_code`` because all three are per-course in the data model (doc 2.6)
#: and a connector that could not scope a read to a course would hand an agent
#: the whole institution's data.
NotesPage = tuple[dict[str, Any], list[NoteMaterial]]


class _Base(BaseModel):
    """Reject unknown keys so a typo in a descriptor surfaces loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)


class SourceHealth(_Base):
    """Whether a source can currently be reached, and if not, why.

    Returned by :meth:`SourceAdapter.health` rather than raised, because "is
    the source up?" is a question the connector should be able to answer *at
    startup*, before any agent has decided it needs data. A source that cannot
    be used should be a clear refusal at the first call, not a crash during
    the first read.

    Attributes:
        ok: True if the source is usable.
        reason: A one-line, operator-facing explanation. Always present, so
            the unhappy path is never just ``False``.
        detail: Optional extra context -- which setting is missing, which file
            is absent, which course codes were found.
    """

    ok: bool
    reason: str = Field(min_length=1)
    detail: str = ""


class SourceDescriptor(_Base):
    """Self-description, returned *with* the data rather than configured into it.

    The provenance travels beside the payload, not ahead of it, for the same
    reason the ledger records every call: an answer that cannot say where it
    came from is not evidence. An agent asking the connector for
    ``CS201``'s submissions gets records that carry ``descriptor.name`` and
    ``descriptor.provenance``, and can report them without asking a second
    question.

    Attributes:
        name: A stable slug for the adapter, e.g. ``local_seed``. This is the
            value :data:`course_organizer.config.Settings.source_adapter`
            selects between.
        transport: What it actually speaks to, e.g. ``"local JSON files"`` or
            ``"Google Drive + Classroom REST"``. Human-facing, and the honest
            place to admit the source is a directory.
        provenance: A prefix the returned records carry, e.g.
            ``"data/seed"``. Rendered OS-independently so an audit is diffable
            between machines.
        writable: Whether this source supports writes. Lab 5 is read-only and
            says so, rather than implying a write path exists.
        requires_source_credentials: True when the *source itself* needs
            setup (an OAuth client, an API key). Distinct from the
            connector's own bearer auth, which every source shares.
        swap_note: One line on what a reader should know about this source,
            shown in the lab report.
    """

    name: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    writable: bool = False
    requires_source_credentials: bool = False
    swap_note: str = ""

    def describe(self) -> str:
        """One line, safe to print. No credentials appear here.

        Returns:
            ``name via transport, provenance=..., writable=...``.
        """
        return (
            f"{self.name} via {self.transport}, "
            f"provenance={self.provenance}, writable={self.writable}"
        )


@runtime_checkable
class SourceAdapter(Protocol):
    """The interface every data source implements to sit behind the connector.

    Three reads, matching doc 2.7's ``connector.expose`` block, plus enough
    metadata for the connector to describe itself honestly. There is
    deliberately no ``write``: doc 2.10 Lab 5 is a read connector, and a
    ``Protocol`` that declared one would be a promise nothing implements.

    Note:
        Every implementation is expected to raise
        :class:`~course_organizer.connector.errors.SourceUnavailableError` for
        "configured but unusable" and
        :class:`~course_organizer.connector.errors.ConnectorNotFoundError` for
        "this course does not exist". The protocol cannot enforce either, so
        the connector's tests assert both on every adapter it ships.
    """

    @property
    def descriptor(self) -> SourceDescriptor:
        """How this source describes itself. Must not raise."""
        ...

    def health(self) -> SourceHealth:
        """Can this source be used right now? Must not raise."""
        ...

    def course_codes(self) -> list[str]:
        """Every course code this source can serve, sorted.

        The connector exposes this as an endpoint so an agent can ask what is
        available instead of guessing a code and eating a not-found.
        """
        ...

    def read_notes(self, course_code: str) -> NotesPage:
        """``read('the notes/materials store')``.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            ``(course_header, notes)``.
        """
        ...

    def read_assignments(self, course_code: str) -> list[AssignmentDefinition]:
        """``read('assignment & rubric definitions')``.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            Validated definitions, sorted by assignment id.
        """
        ...

    def read_submissions(self, course_code: str) -> list[SubmissionRecord]:
        """``read('submission records')``.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            Validated records, sorted by submission id.
        """
        ...

    def backing(
        self, kind: str, course_code: Optional[str] = None
    ) -> list[str]:
        """The concrete locations a read of ``kind`` came from.

        Added for the audit trail, and it exists because of a real gap rather
        than a hypothetical one. ``LocalSeedStore`` could name the exact file
        behind a read, so Lab 2's tools put a real path in
        ``source_files``. Once the tools went behind the connector they could
        no longer ask the store -- and the honest options were to drop the field
        (losing an audit trail the lab had already fixed a bug to add) or to
        invent a path. So the port asks the source itself, and each source
        answers in its own vocabulary:

        * ``local_seed`` -> ``["data/seed/CS201/notes.json"]``
        * ``in_memory`` -> ``["in-memory://ZZ999/notes"]``
        * ``google``     -> ``["gdrive:<file-id>"]``

        A source that genuinely cannot say may return an empty list; the
        provenance block then carries no location, which is visibly incomplete
        rather than quietly wrong.

        Args:
            kind: One of ``"courses"``, ``"notes"``, ``"assignments"`` or
                ``"submissions"`` -- the connector's own vocabulary, not the
                source's. An adapter maps it to whatever it has.
            course_code: The course, or None for corpus-wide kinds.

        Returns:
            Zero or more human-readable locations. Never raises.
        """
        ...


def log_source_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log a source-layer event on the shared structured logger.

    A thin wrapper kept here so the adapters do not each import
    :mod:`course_organizer.logging_setup`, and so the log-event naming
    convention for this package is stated once.

    Args:
        logger: The module logger to use.
        event: The event name, e.g. ``connector.source.read``.
        **fields: Structured fields. Never include a credential.
    """
    log_event(logger, event, **fields)
