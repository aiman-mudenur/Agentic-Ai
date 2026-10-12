"""The memory boundary: two layers, one direction, enforced in code.

Document 2.10, Lab 4, states the goal exactly:

    "Separate short-term run state from long-term stored knowledge."

and names the failure mode:

    **Pitfall to avoid.** Don't conflate run state with the archive -
    mixing them corrupts the baseline.

Its pseudocode is three lines long and every line crosses the boundary::

    state.draft = candidate                           # short-term
    hits  = retrieve(store, tags)                     # long-term
    prior = retrieve(history, embed(candidate), k=5)  # similarity

Read left to right: the candidate is *placed* in run state, then used as the
probe for two long-term retrievals. Nothing ever travels back the other way.
That asymmetry is the whole design, and a pitfall written in prose is not
enforced, so this module makes it structural:

1. **Every record declares its layer.** :class:`MemoryRecord.layer` is
   :class:`MemoryLayer`, and its two values are the two halves of the
   document's sentence: ``RUN_STATE`` ("session state - the current draft")
   and ``ARCHIVE`` ("stored reference").
2. **A store accepts only its own layer.**
   :meth:`MemoryStore.append` raises :class:`MemoryBoundaryError` on a record
   from the other layer. This is the direction the pitfall names.
3. **A record cannot change layers.** There is no ``promote()``/``demote()``.
   A run-state draft is a draft for the life of the process; when a draft
   genuinely becomes reference, it is re-read from its source by
   :meth:`MemoryStore.append`, which is a separate, auditable act.
4. **The baseline is observable.** :meth:`MemoryStore.fingerprint` returns a
   content hash over the whole layer. A run can therefore *assert* -- rather
   than claim -- that it left the archive untouched, by comparing the value
   before and after.

On naming: the document's word is *archive*, so that is the word used here
rather than "vector store" or "long-term store". A vector is an
implementation detail of one retriever; the archive is the concept.

Provenance
----------
:meth:`MemoryStore.allowed_provenance` is the second half of the boundary,
and it exists because ``layer`` alone is not enough. A caller can always
``model_copy(update={"layer": ...})`` its way past a field check, and a
boundary that a one-line dict update defeats is not a boundary. So the archive
also requires each record's ``provenance`` to name a real read source, which a
caller cannot invent for its own draft. Run-state records carry the sentinel
:data:`RUN_STATE_PROVENANCE`, which is deliberately not a path: a draft has no
source file behind it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Generic, Iterator, Mapping, Optional, TypeVar

from pydantic import BaseModel, Field, ValidationError

from course_organizer.logging_setup import log_event

logger = logging.getLogger(__name__)

TQuery = TypeVar("TQuery", bound=BaseModel)
TResult = TypeVar("TResult", bound=BaseModel)


# ==========================================================================
# Errors
# ==========================================================================


class MemoryError(RuntimeError):
    """Raised for a memory-layer failure the caller must handle.

    Covers an unknown retriever id and schema-invalid *queries*. A retriever
    that cannot answer is not this: it returns a result whose ``ok`` is False,
    so the failed attempt appears in the ledger rather than vanishing.
    """


class MemoryBoundaryError(MemoryError):
    """Raised when something tries to cross the run-state/archive boundary.

    This is the exception the document's pitfall is about. It fires in three
    situations, all of which amount to treating a draft as if it were
    reference:

    * writing a ``RUN_STATE`` record into an archive store;
    * registering a retriever that declares one layer against a store of the
      other;
    * running a registered retriever against a store of the wrong layer.
    """


# ==========================================================================
# The two layers
# ==========================================================================


class MemoryLayer(str, Enum):
    """Which half of the document's memory split a record belongs to.

    Values:
        RUN_STATE: Short-term, in-process, dies with the run. Doc 2.10 Lab 4:
            "Session state (the current draft)". A draft written here is
            reachable by this run and by no other.
        ARCHIVE: Long-term, source-backed. Doc 2.10 Lab 4: "stored reference -
            the notes/materials store and the history of past assignment
            submissions". Read-only to agents, because every record here came
            from a real read of a real source and 2.12 asks for a clear version
            history rather than an agent-editable one.
    """

    RUN_STATE = "run_state"
    ARCHIVE = "archive"

    def other(self) -> "MemoryLayer":
        """The other layer. Used in boundary messages and in assertions."""
        return (
            MemoryLayer.ARCHIVE
            if self is MemoryLayer.RUN_STATE
            else MemoryLayer.RUN_STATE
        )

    @property
    def is_ephemeral(self) -> bool:
        """True for run state, which is not meant to outlive its run."""
        return self is MemoryLayer.RUN_STATE


#: Docstring wording, kept in one place so the CLI, the tests and the report
#: cannot drift apart from the prose they quote.
LAYER_PURPOSE: dict[MemoryLayer, str] = {
    MemoryLayer.RUN_STATE: "short-term run state (the current draft)",
    MemoryLayer.ARCHIVE: (
        "long-term stored knowledge (notes store + submission history)"
    ),
}

#: Provenance sentinel for run-state drafts. Not a path, and never will be:
#: the whole point is that a draft has no source file behind it.
RUN_STATE_PROVENANCE = "run_state:draft"


# ==========================================================================
# The record
# ==========================================================================


class MemoryRecord(BaseModel):
    """One retrievable item, tagged with the layer it lives in.

    Only :mod:`course_organizer.memory.stores` should construct these; a test
    in Lab 4 asserts that, because the record is where the layer label lives
    and a record built anywhere else is a record nobody audited.

    Attributes:
        record_id: Stable id. For archive records this is the source entity's
            own id -- a note id or a submission id -- so a hit can be traced
            back to the row it came from. For run state it is generated.
        layer: Which half of the split this record is in. See `MemoryLayer`.
        course_code: Which course it belongs to. Retrieval is always scoped to
            one course, so this is required rather than optional.
        kind: What sort of thing it is. Archive records use the source's own
            vocabulary (``note_material``, ``submission``); run-state records
            use ``candidate_draft``. A free string, because a new source brings
            new kinds and this layer should not have to be edited for each.
        title: A short human label, for reports and the notebook.
        content: The text retrieval runs over. For a note, its current
            version's text; for a submission, the submitted text.
        tags: Free tags, lowercase. Archive notes carry the store's own tags
            plus ``unit:<n>``; archive submissions carry ``assignment:<id>``,
            ``status:<s>`` and ``late`` when applicable, because those are the
            filters a later lab needs and a later lab should not have to
            re-derive them.
        meta: Small structured facts that must not be searchable -- source
            ids, mark totals, store-verified lateness. Deliberately excluded
            from :meth:`content_payload`, so adding a fact here cannot change a
            record's identity in the baseline.
        provenance: Where the record came from, as a repo-relative source path.
            This is the field the boundary checks: a caller can forge `layer`
            with a `model_copy`, but it cannot make its own draft's provenance
            name a file it never read.
        created_at: When the record entered its layer. For archive records
            this is the source's own timestamp where one exists -- a
            submission's ``submitted_at`` -- because 2.12 forbids altering a
            submission timestamp.
        is_current: Whether this version is the authoritative one. Doc 2.6:
            notes are "tagged by unit/topic and version". Historic versions stay
            in the archive and stay retrievable; this flag says which one wins.
    """

    model_config = {"extra": "forbid", "validate_assignment": True}

    record_id: str = Field(min_length=1)
    layer: MemoryLayer
    course_code: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    provenance: str = Field(min_length=1)
    created_at: datetime
    is_current: bool = True

    def content_payload(self) -> str:
        """The retrievable text as one normalised string.

        Whitespace is collapsed, so a re-indented note does not read as
        different content. The title is included because a query that matched
        on a word in the title should also rank on that word.
        """
        joined = " ".join(f"{self.title}. {self.content}".split())
        return joined.lower()

    def fingerprint(self) -> str:
        """A content hash of this record, for the layer baseline.

        Deliberately excludes `created_at`, `meta` and `is_current`. The
        baseline answers "did the *text* of this layer change", which is the
        question the pitfall poses; a timestamp would change on every rebuild
        and make the fingerprint useless for that purpose.
        """
        payload = json.dumps(
            {
                "record_id": self.record_id,
                "layer": self.layer.value,
                "course_code": self.course_code,
                "kind": self.kind,
                "content": self.content_payload(),
                "tags": sorted(self.tags),
                "provenance": self.provenance,
                "is_current": self.is_current,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> str:
        """One line, for CLI output and audit records."""
        return (
            f"{self.record_id} [{self.kind}] {self.title} "
            f"({len(self.content)} chars, {len(self.tags)} tags)"
        )


# ==========================================================================
# The ledger
# ==========================================================================


@dataclass(frozen=True)
class RetrievalCall:
    """One recorded retrieval.

    Attributes:
        call_id: Unique id, so a hit set can be traced back to its call.
        retriever: Registered retriever id.
        layer: The layer that was read. Recorded so an audit can show that no
            run-state read reached the archive, and vice versa.
        query_payload: The validated query, as JSON-compatible data.
        result_count: How many hits came back. Zero is a legitimate answer.
        corpus_size: How many records were available to search. Without this a
            result of zero hits is indistinguishable from a result over an
            empty store, which is a very different finding.
        embedder: Which embedder produced the vectors, or None for a retriever
            that does not embed. Recorded because a similarity score is
            meaningless without knowing the model behind it.
        ok: False when the retriever reported a failure.
        error: Failure reason when ``ok`` is False.
        elapsed_ms: Wall-clock duration.
        at: Timezone-aware UTC timestamp.
    """

    call_id: str
    retriever: str
    layer: MemoryLayer
    query_payload: dict[str, Any]
    result_count: int
    corpus_size: int
    embedder: Optional[str]
    ok: bool
    error: Optional[str]
    elapsed_ms: float
    at: datetime

    def summary(self) -> str:
        """One-line human-readable form."""
        flag = "ok " if self.ok else "FAIL"
        tail = f" {self.error}" if self.error else ""
        return (
            f"[{flag}] {self.retriever} on {self.layer.value}: "
            f"{self.result_count}/{self.corpus_size} in {self.elapsed_ms:.1f}ms{tail}"
        )


@dataclass
class RetrievalLedger:
    """Append-only record of every retrieval made during a run.

    The document asks for this in 2.12 -- "a full audit log" -- and Lab 4 is
    where the third kind of governed action appears, after Lab 2's tool calls
    and Lab 3's skill calls. Lab 6 makes all three durable on disk.

    Attributes:
        run_id: The run this ledger belongs to.
        entries: Calls in issue order.
    """

    run_id: str
    entries: list[RetrievalCall] = field(default_factory=list)

    def record(self, call: RetrievalCall) -> None:
        """Append one call. Never mutates an earlier entry."""
        self.entries.append(call)

    def names(self) -> list[str]:
        """Every retriever id invoked, in order, including repeats."""
        return [entry.retriever for entry in self.entries]

    def layers_read(self) -> list[MemoryLayer]:
        """The distinct layers read, in first-seen order."""
        seen: list[MemoryLayer] = []
        for entry in self.entries:
            if entry.layer not in seen:
                seen.append(entry.layer)
        return seen

    def failures(self) -> list[RetrievalCall]:
        """Every failed call, for diagnosis."""
        return [entry for entry in self.entries if not entry.ok]

    def total_time_ms(self) -> float:
        """Summed retriever time across all calls."""
        return round(sum(entry.elapsed_ms for entry in self.entries), 3)

    def report(self) -> str:
        """A readable audit table."""
        if not self.entries:
            return "retrieval ledger is empty - nothing was retrieved"
        header = (
            f"{'#':>3}  {'retriever':<26}{'layer':<11}{'hits':>6}"
            f"{'corpus':>8}{'ms':>9}  status"
        )
        lines = [header, "-" * len(header)]
        for i, entry in enumerate(self.entries, 1):
            status = "ok" if entry.ok else f"FAIL: {entry.error}"
            lines.append(
                f"{i:>3}  {entry.retriever:<26}{entry.layer.value:<11}"
                f"{entry.result_count:>6}{entry.corpus_size:>8}"
                f"{entry.elapsed_ms:>9.2f}  {status}"
            )
        lines.append("-" * len(header))
        lines.append(
            f"{len(self.entries):>3}  {'TOTAL':<26}{'':11}{'':6}{'':8}"
            f"{self.total_time_ms():>9.2f}  {len(self.failures())} failed"
        )
        return "\n".join(lines)

    def __len__(self) -> int:
        """Number of recorded calls."""
        return len(self.entries)

    def __iter__(self) -> Iterator[RetrievalCall]:
        """Iterate calls in order."""
        return iter(self.entries)


# ==========================================================================
# Result
# ==========================================================================


@dataclass(frozen=True)
class RetrievalResult(Generic[TResult]):
    """The outcome of one retrieval.

    Attributes:
        retriever: Retriever id.
        call: The ledger entry this result produced.
        ok: Whether the retriever answered.
        value: The validated result model, present only when ``ok``.
        error: Failure reason when ``ok`` is False.
    """

    retriever: str
    call: RetrievalCall
    ok: bool
    value: Optional[TResult] = None
    error: Optional[str] = None

    def unwrap(self) -> TResult:
        """Return the result value, or raise if the retrieval failed.

        Raises:
            MemoryError: When ``ok`` is False.
        """
        if not self.ok or self.value is None:
            raise MemoryError(f"{self.retriever} failed: {self.error}")
        return self.value


# ==========================================================================
# Spec
# ==========================================================================


Handler = Callable[[Any, "MemoryStore", RetrievalLedger], BaseModel]


@dataclass(frozen=True)
class RetrieverSpec(Generic[TQuery, TResult]):
    """A typed retriever declaration, bound to exactly one memory layer.

    Attributes:
        retriever_id: Unique registered name. The document's pseudocode reads
            ``retrieve(store, tags)`` and ``retrieve(history, ...)``, so the
            names used here are ``retrieve_by_tags`` and
            ``retrieve_similar_prior`` and read like the document's own.
        description: One line, saying when to reach for it. This is what a
            planner would read when deciding which retriever to call.
        query_model: Pydantic model for the query.
        result_model: Pydantic model for the answer.
        handler: ``(query, store, ledger) -> result_model``. The store is a
            parameter rather than a closure so that the registry, not the
            handler, decides which layer is searched -- and can therefore
            refuse a mismatch.
        doc_ref: The document requirement this serves.
        reads: The layer this retriever may read. A handler cannot choose
            otherwise: the registry validates the store's layer against this
            both at registration and before every call.
    """

    retriever_id: str
    description: str
    query_model: type[TQuery]
    result_model: type[TResult]
    handler: Handler
    doc_ref: str
    reads: MemoryLayer

    def describe(self) -> str:
        """A human-readable declaration, for the notebook and reports."""
        fields = ", ".join(
            f"{name}: {info.annotation}"
            for name, info in self.query_model.model_fields.items()
        )
        return (
            f"{self.retriever_id}({fields}) -> {self.result_model.__name__}"
            f"  [reads {self.reads.value}]\n"
            f"    {self.description}\n"
            f"    doc: {self.doc_ref}"
        )


# ==========================================================================
# Registry
# ==========================================================================


class MemoryRegistry:
    """Holds the retrievers and runs them against a layer, on one ledger.

    Mirrors :class:`course_organizer.tools.base.ToolRegistry` deliberately:
    Labs 2, 3 and 4 are three governed action kinds, and Lab 6 wraps all three
    in a runtime, so a reviewer who has read one ledger can read the other
    two.

    It adds one thing the tool registry does not need -- a layer check on
    every invocation, because a retriever reading the wrong layer is the exact
    failure Lab 4 exists to prevent.

    Attributes:
        run_id: The run this registry belongs to.
        ledger: The append-only retrieval record.
    """

    def __init__(self, *, run_id: str) -> None:
        """Create an empty registry bound to a run id.

        Args:
            run_id: Correlates retrievals with the logging run id and ledger.
        """
        self._retrievers: dict[str, RetrieverSpec[Any, Any]] = {}
        self._bound: dict[str, MemoryStore] = {}
        self.ledger = RetrievalLedger(run_id=run_id)
        self.run_id = run_id

    # -- registration ------------------------------------------------------

    def register(
        self,
        spec: RetrieverSpec[TQuery, TResult],
        store: "MemoryStore",
    ) -> RetrieverSpec[TQuery, TResult]:
        """Add a retriever, bound to the store it is allowed to search.

        Args:
            spec: The declaration to add.
            store: The layer store this retriever reads.

        Returns:
            The same spec, so registration can decorate a definition inline.

        Raises:
            MemoryBoundaryError: If the store's layer is not the one the spec
                declares. Checked once at wiring time so the error names the
                mistake instead of surfacing later as a mysterious empty
                result.
            MemoryError: If the id is already taken.
        """
        if store.layer is not spec.reads:
            raise MemoryBoundaryError(
                f"retriever {spec.retriever_id!r} declares it reads "
                f"{spec.reads.value!r} but was wired to a {store.layer.value} "
                f"store. {LAYER_PURPOSE[spec.reads]} and "
                f"{LAYER_PURPOSE[store.layer]} are separate memories; one "
                "retriever may not read both."
            )
        if spec.retriever_id in self._retrievers:
            raise MemoryError(
                f"retriever {spec.retriever_id!r} is already registered"
            )
        self._retrievers[spec.retriever_id] = spec
        self._bound[spec.retriever_id] = store
        log_event(
            logger,
            "retriever_registered",
            retriever=spec.retriever_id,
            layer=spec.reads.value,
            doc_ref=spec.doc_ref,
        )
        return spec

    def retriever(
        self,
        retriever_id: str,
        *,
        description: str,
        query_model: type[TQuery],
        result_model: type[TResult],
        doc_ref: str,
        reads: MemoryLayer,
        store: "MemoryStore",
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`.

        Usage::

            @registry.retriever(
                "retrieve_by_tags",
                description="...",
                query_model=TagQuery,
                result_model=TagResult,
                doc_ref="2.10 Lab 4 pseudocode hits = retrieve(store, tags)",
                reads=MemoryLayer.ARCHIVE,
                store=archive,
            )
            def retrieve_by_tags(
                q: TagQuery, store: MemoryStore, ledger: RetrievalLedger
            ) -> TagResult:
                ...

        Args:
            retriever_id: Unique name.
            description: When to call it, in one line.
            query_model: Pydantic query model.
            result_model: Pydantic result model.
            doc_ref: Requirement this serves.
            reads: The layer this retriever may read.
            store: The store to bind.

        Returns:
            A decorator returning the handler unchanged.

        Raises:
            MemoryBoundaryError: If the store's layer disagrees with `reads`.
            MemoryError: If the id is already taken.
        """

        def decorate(handler: Handler) -> Handler:
            self.register(
                RetrieverSpec(
                    retriever_id=retriever_id,
                    description=description,
                    query_model=query_model,
                    result_model=result_model,
                    handler=handler,
                    doc_ref=doc_ref,
                    reads=reads,
                ),
                store,
            )
            return handler

        return decorate

    # -- lookup ------------------------------------------------------------

    def names(self) -> list[str]:
        """Every registered retriever id, sorted."""
        return sorted(self._retrievers)

    def get(self, retriever_id: str) -> RetrieverSpec[Any, Any]:
        """Look up one spec.

        Raises:
            MemoryError: If unknown, listing what is available.
        """
        if retriever_id not in self._retrievers:
            raise MemoryError(
                f"unknown retriever {retriever_id!r}; available: {self.names()}"
            )
        return self._retrievers[retriever_id]

    def describe_all(self) -> str:
        """Every spec's declaration, as a readable block."""
        return "\n".join(
            self._retrievers[name].describe() for name in self.names()
        )

    def store_for(self, retriever_id: str) -> "MemoryStore":
        """The store a registered retriever is bound to.

        Raises:
            MemoryError: If the retriever is unknown, or was somehow
                registered without a store. The second case cannot happen
                through :meth:`register`; it is checked rather than assumed.
        """
        self.get(retriever_id)
        store = self._bound.get(retriever_id)
        if store is None:
            raise MemoryError(
                f"retriever {retriever_id!r} has no bound store; this is a "
                "registry wiring bug"
            )
        return store

    def layers_covered(self) -> list[MemoryLayer]:
        """The distinct layers any registered retriever reads, in first-seen
        order.

        A registry covering only one layer is a smell worth surfacing: the
        document's pseudocode reads both, so a run that only ever touches one
        has silently skipped half the lab.
        """
        seen: list[MemoryLayer] = []
        for name in self.names():
            layer = self._retrievers[name].reads
            if layer not in seen:
                seen.append(layer)
        return seen

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        retriever_id: str,
        query: BaseModel | Mapping[str, Any],
        *,
        store: Optional["MemoryStore"] = None,
    ) -> RetrievalResult[Any]:
        """Execute a retriever and record the call.

        Args:
            retriever_id: Registered retriever id.
            query: A validated query model, or a mapping to validate.
            store: Which layer to search. Must match the spec's declared
                layer; omit to use the bound store.

        Returns:
            A `RetrievalResult`.

        Raises:
            MemoryError: If the id is unknown, or the query does not satisfy
                the declared query model.
            MemoryBoundaryError: If the store's layer is not the one the
                retriever declares.
        """
        spec = self.get(retriever_id)
        target = store if store is not None else self.store_for(retriever_id)

        if target.layer is not spec.reads:
            raise MemoryBoundaryError(
                f"retriever {retriever_id!r} reads {spec.reads.value!r}; "
                f"refusing to run it against a {target.layer.value} store"
            )

        if isinstance(query, BaseModel):
            validated = spec.query_model.model_validate(query.model_dump())
        else:
            try:
                validated = spec.query_model.model_validate(dict(query))
            except ValidationError as exc:
                raise MemoryError(
                    f"invalid query for retriever {retriever_id!r}: "
                    f"{exc.error_count()} problem(s). First: "
                    f"{exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"
                ) from exc

        call_id = uuid.uuid4().hex[:12]
        corpus_size = len(target)
        embedder: Optional[str] = None
        started = time.perf_counter()
        ok = True
        error: Optional[str] = None
        value: Optional[BaseModel] = None

        try:
            value = spec.handler(validated, target, self.ledger)
            if not isinstance(value, spec.result_model):
                ok = False
                error = (
                    f"handler returned {type(value).__name__}, "
                    f"expected {spec.result_model.__name__}"
                )
            else:
                embedder = getattr(value, "embedder", None)
        except Exception as exc:  # noqa: BLE001 - recorded, then reported
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("Retriever %s failed: %s", retriever_id, error)

        result_count = 0
        if value is not None and hasattr(value, "hits"):
            result_count = len(value.hits)  # type: ignore[attr-defined]

        elapsed_ms = (time.perf_counter() - started) * 1000
        call = RetrievalCall(
            call_id=call_id,
            retriever=retriever_id,
            layer=target.layer,
            query_payload=validated.model_dump(mode="json"),
            result_count=result_count,
            corpus_size=corpus_size,
            embedder=embedder,
            ok=ok,
            error=error,
            elapsed_ms=round(elapsed_ms, 4),
            at=datetime.now(tz=timezone.utc),
        )
        self.ledger.record(call)
        log_event(
            logger,
            "retrieval_call",
            retriever=retriever_id,
            call_id=call_id,
            layer=target.layer.value,
            ok=ok,
            hits=result_count,
            corpus_size=corpus_size,
            elapsed_ms=round(elapsed_ms, 2),
        )
        return RetrievalResult(
            retriever=retriever_id, call=call, ok=ok, value=value, error=error
        )

    # -- interoperability --------------------------------------------------

    def as_langchain_tools(self) -> list[Any]:
        """Expose the retrievers as LangChain `StructuredTool` objects.

        Lab 7 binds these to graph nodes, so a retrieval node receives a
        validated model rather than a string.

        Returns:
            One `StructuredTool` per registered retriever.

        Raises:
            ImportError: If langchain-core is not installed.
        """
        try:
            from langchain_core.tools import StructuredTool
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ImportError(
                "langchain-core is required for LangGraph interop. "
                "Install it with: pip install langchain-core==1.6.6"
            ) from exc

        tools = []
        for name in self.names():
            spec = self._retrievers[name]

            def make_handler(spec: RetrieverSpec[Any, Any]) -> Callable[..., str]:
                """Build the callable LangChain will invoke.

                Returns the output as JSON so the text interface stays
                lossless; the typed path is `registry.invoke` directly.
                """

                def handler(**kwargs: Any) -> str:
                    """Invoke this retriever and return its output as JSON."""
                    result = self.invoke(spec.retriever_id, kwargs)
                    if not result.ok:
                        return f'{{"ok": false, "error": {result.error!r}}}'
                    return result.value.model_dump_json()  # type: ignore[union-attr]

                handler.__name__ = spec.retriever_id
                handler.__doc__ = spec.description
                return handler

            tools.append(
                StructuredTool.from_function(
                    func=make_handler(spec),
                    name=spec.retriever_id,
                    description=spec.description,
                    args_schema=spec.query_model,
                )
            )
        return tools


# ==========================================================================
# The store interface
# ==========================================================================


class MemoryStore(ABC):
    """One memory layer: a named, fingerprinted collection of records.

    The base class is abstract so that the two halves cannot grow a shared
    escape hatch. A subclass says which layer it is and implements
    :meth:`allowed_provenance`; :meth:`_check_appendable` then decides what may
    be written on top of the layer check every store shares.

    The one rule both subclasses inherit: a record is stamped with *this*
    store's layer on the way in, so no caller has to get it right and no
    caller can get it wrong.

    Attributes:
        layer: The layer this store holds. Set by the subclass.
        run_id: The run that opened this store.
        course_code: Which course this store covers. Retrieval is always
            course-scoped, so it is required.
    """

    #: The layer this store holds. Declared with no value, so a subclass that
    #: never sets one fails at class-definition time; see
    #: `__init_subclass__` below.
    layer: MemoryLayer

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a subclass that never says which layer it holds.

        `MemoryStore` declares `layer` as an annotation with no value, so a
        subclass that forgets to set one resolves it to None and is caught
        here, at class-definition time, where the fix is a one-liner rather
        than a runtime surprise three layers away.

        Inheriting a layer is allowed and is how a deliberately broken subclass
        such as the lab's `ContaminatedArchive` can exist at all: it is an
        archive that takes one extra record, not a third kind of memory.
        """
        super().__init_subclass__(**kwargs)
        if getattr(cls, "layer", None) is None:
            raise MemoryError(
                f"{cls.__name__} never declares a `layer`. A store that does "
                "not say which half of the memory split it holds cannot be "
                "checked against the boundary, which is the one job these two "
                "stores have. Inherit it from a subclass that declares one, or "
                "declare it here."
            )

    def __init__(self, *, run_id: str, course_code: str) -> None:
        """Create an empty store.

        Args:
            run_id: Run id, stamped on anything this store loads.
            course_code: Which course this store covers.
        """
        self.run_id = run_id
        self.course_code = course_code
        self._records: dict[str, MemoryRecord] = {}

    # -- layer contract ----------------------------------------------------

    @property
    def allowed_provenance(self) -> frozenset[str]:
        """Provenance strings a record must carry to be stored here.

        The default is the run-state sentinel, which is what makes
        :class:`RunState` the safe default and the archive the one that has to
        opt in to real source paths. The archive subclass overrides this with
        the course's actual source files, so a record claiming to come from
        ``data/seed/dbms/notes.json`` is admissible for ``DBMS402`` and not for
        ``CS201``.
        """
        return frozenset({RUN_STATE_PROVENANCE})

    def _check_appendable(self, record: MemoryRecord) -> None:
        """Raise unless this record may live in this store.

        **Both** boundary invariants live in this one method, deliberately, so
        that there is a single place to look for them and a single place a
        deliberate bypass has to defeat. Two checks run here, and both must
        pass:

        1. the record's ``layer`` must be this store's layer;
        2. its ``provenance`` must be a source this store reads from.

        They are independent on purpose. Check 1 alone can be defeated by
        ``model_copy(update={"layer": ...})``; check 2 cannot, because a caller
        cannot make its own draft claim a file it never opened.

        Args:
            record: The record about to be stored.

        Raises:
            MemoryBoundaryError: If the record belongs to the other layer, or
                its provenance is not a source this store reads.
        """
        if record.layer is not self.layer:
            raise MemoryBoundaryError(
                f"cannot store a {record.layer.value} record in a store holding "
                f"{self.layer.value} records. "
                f"{LAYER_PURPOSE[record.layer]} and "
                f"{LAYER_PURPOSE[self.layer]} are separate memories: "
                f"{LAYER_PURPOSE[record.layer]} must not reach "
                f"{LAYER_PURPOSE[self.layer]}."
            )
        if record.provenance not in self.allowed_provenance:
            raise MemoryBoundaryError(
                f"record {record.record_id!r} claims provenance "
                f"{record.provenance!r}, which is not a source this "
                f"{self.layer.value} store reads from. Allowed: "
                f"{sorted(self.allowed_provenance) or '(none)'}"
            )

    # -- the one way in ----------------------------------------------------

    def append(self, record: MemoryRecord) -> MemoryRecord:
        """Add a record, if this store will have it.

        Args:
            record: The record to store.

        Returns:
            The record, so a call can be used inline.

        Raises:
            MemoryBoundaryError: If either boundary invariant fails. See
                :meth:`_check_appendable` for both.
            MemoryError: If the id is already present. A duplicate would make
                :meth:`records` order-dependent and :meth:`fingerprint`
                meaningless.
        """
        self._check_appendable(record)
        if record.record_id in self._records:
            raise MemoryError(
                f"record {record.record_id!r} is already in this "
                f"{self.layer.value} store; ids must be unique so the "
                "fingerprint is stable"
            )
        self._records[record.record_id] = record
        log_event(
            logger,
            "memory_record_stored",
            layer=self.layer.value,
            record_id=record.record_id,
            kind=record.kind,
        )
        return record

    # -- read-only access --------------------------------------------------

    def records(self) -> list[MemoryRecord]:
        """Every record, sorted by id. The order is stable and id-based."""
        return [self._records[key] for key in sorted(self._records)]

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """One record by id, or None when absent."""
        return self._records.get(record_id)

    def fingerprint(self) -> str:
        """A content hash of the whole layer.

        This is the measurable form of the document's warning that mixing run
        state into the archive "corrupts the baseline". A run takes the value
        before and after its work and can then state as a fact that it left
        the archive unchanged.

        Returns:
            A 16-hex-character digest over each record's fingerprint, in id
            order. An empty layer returns the literal ``"empty"``, so "this
            layer is empty" is distinguishable in an audit from "nobody
            computed the baseline".
        """
        if not self._records:
            return "empty"
        digest = hashlib.sha256()
        for record in self.records():
            digest.update(record.fingerprint().encode("ascii"))
        return digest.hexdigest()[:16]

    def contains_foreign_layer(self) -> bool:
        """Does this store hold any record labelled with the *other* layer?

        Should always be False. It exists because it is the cheapest possible
        check to run in CI: a True here means a record was appended with a
        layer that did not match, which means the boundary has a hole.
        """
        return any(r.layer is not self.layer for r in self._records.values())

    def __len__(self) -> int:
        """How many records this store holds."""
        return len(self._records)

    def __iter__(self) -> Iterator[MemoryRecord]:
        """Iterate records in id order."""
        return iter(self.records())

    def __repr__(self) -> str:
        """A short, unambiguous repr. Which layer is the whole point."""
        return (
            f"{type(self).__name__}(layer={self.layer.value!r}, "
            f"course={self.course_code!r}, records={len(self._records)})"
        )


__all__ = [
    "LAYER_PURPOSE",
    "RUN_STATE_PROVENANCE",
    "MemoryBoundaryError",
    "MemoryError",
    "MemoryLayer",
    "MemoryRecord",
    "MemoryRegistry",
    "MemoryStore",
    "RetrievalCall",
    "RetrievalLedger",
    "RetrievalResult",
    "RetrieverSpec",
]
