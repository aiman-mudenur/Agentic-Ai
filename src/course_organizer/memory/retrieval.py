"""The two retrievers the document's pseudocode names, and the flag they seed.

Document 2.10, Lab 4, pseudocode::

    state.draft = candidate                           # short-term
    hits  = retrieve(store, tags)                     # long-term
    prior = retrieve(history, embed(candidate), k=5)  # similarity

Two retrievers, one per long-term line, both registered against the archive
and both refused the run state. Names mirror the document's own, so a plan
written against Chapter 2 calls the same things this module implements:

=====================================  ==========================================
Document                               Implementation
=====================================  ==========================================
``retrieve(store, tags)``              :class:`TagRetriever` / ``retrieve_by_tags``
``retrieve(history, embed(c), k=5)``   :class:`SimilarityRetriever` / ``retrieve_similar_prior``
=====================================  ==========================================

What "seeds validation/no-repeat" means here
--------------------------------------------
"Nodes activated: Retrieval node; seeds validation/no-repeat" is the sentence
that makes this lab more than a search box. Concretely:

* **No-repeat seed.** :class:`SimilarityFlag` is the input to "has this student
  submitted this before?". It answers with a *ranking and a number*.
* **Validation seed.** :class:`SimilarityFlag` is also the input to doc 2.7's
  check 2, "a plagiarism/similarity score is attached". A score is attached
  here; what that score *means* is not decided here.

:data:`PLAGIARISM_FORBIDDEN_FIELDS` is the set of field names that would let
this module express a determination -- ``is_plagiaristic``, ``verdict``,
``penalty``, ``plagiarism_score`` and their relatives -- and a test asserts
none of them exists on :class:`SimilarityFlag`. Doc 2.4 node 4 and doc 2.12
both say the same thing in different words: flag for human review, never make a
final determination automatically, never auto-penalise. A forbidden field is
the only way to make "never" enforceable rather than aspirational.

The threshold is a routing number
---------------------------------
``SimilarityResult.threshold_used`` comes from the *embedder*, not from a
module constant, because the same score means different things to a learned
model and to character-n-gram hashing. See
:mod:`course_organizer.memory.embeddings` for the measured distributions the
two constants were read off. What decides *what gets looked at by a human*; it
does not decide what happened. Two consequences follow, and both are visible in
the result type:

* ``exceeds_threshold`` is a fact about a comparison, and the field is named
  for that. It is not a finding about a student.
* the disposition values are routing decisions -- ``review_required`` or
  ``within_threshold`` -- and there is no third value meaning "plagiaristic".

:func:`course_organizer.labs.lab04_memory_retrieval.lab_threshold_report`
prints the distribution rather than asking a reader to take the number on
trust. A threshold nobody examined is a threshold nobody should rely on.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.logging_setup import log_event
from course_organizer.memory.base import (
    MemoryBoundaryError,
    MemoryError,
    MemoryLayer,
    MemoryRecord,
    MemoryRegistry,
    MemoryStore,
    RetrievalLedger,
)
from course_organizer.memory.embeddings import (
    TRIAGE_THRESHOLD_FASTEMBED,
    Embedder,
    EmbeddingError,
    build_embedder,
    cosine_similarity,
)
from course_organizer.schemas import _Base
from course_organizer.memory.stores import KIND_CANDIDATE_DRAFT, KIND_SUBMISSION

logger = logging.getLogger(__name__)

DOC_REF = "2.10 Lab 4"

#: Field names that would let this module state a plagiarism determination.
#: Asserted absent from `SimilarityFlag` by a test. Doc 2.4 node 4: "Flag for
#: human review; never make a final plagiarism determination automatically."
#: Doc 2.12: "Flag high-similarity submissions for review only; never
#: auto-penalize."
PLAGIARISM_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "is_plagiaristic",
        "is_plagiarism",
        "plagiarised",
        "plagiarized",
        "verdict",
        "determination",
        "decision",
        "finding",
        "guilt",
        "penalty",
        "penalty_applied",
        "mark_deduction",
        "deducted_marks",
        "score_applied",
        "plagiarism_score",
        "similarity_percentage_match",
        "rejected",
        "auto_rejected",
        "confirm_plagiarism",
    }
)

#: The two routing outcomes. A third value meaning "plagiaristic" is what the
#: document forbids, so there are exactly two and the type says so.
Disposition = Literal["review_required", "within_threshold"]


# ==========================================================================
# Hit -- one retrieved record
# ==========================================================================


class RetrievalHit(_Base):
    """One retrieved record, with the reason it was retrieved.

    `why` is not decoration. A retrieval result a human cannot interrogate is
    indistinguishable from a guess, and doc 2.12 asks for an audit log of what
    the system did and why. Every hit says which of the two retrievers found
    it and on what.

    Attributes:
        record_id: The archive record's id, traceable to a note id or a
            submission id.
        layer: The layer it was read from. Always `ARCHIVE` for a real hit; it
            is carried so a caller can see the boundary held rather than
            assume it.
        kind: The record's kind (``note_material``, ``submission``).
        title: The record's title.
        score: Rank score. For a tag hit this is the fraction of requested tags
            the record carries, in [0, 1]. For a similarity hit it is the
            cosine similarity, in [-1, 1]. The two are not comparable and are
            never mixed in one result.
        rank: 1-based position in the result.
        matched_on: ``"tag"`` or ``"embedding"``.
        matched_tags: For a tag hit, the requested tags this record actually
            carries. For a similarity hit, empty -- tags did not participate.
        why: One human-readable line naming the evidence.
    """

    record_id: str
    layer: MemoryLayer
    kind: str
    title: str
    score: float
    rank: int
    matched_on: Literal["tag", "embedding"]
    matched_tags: list[str] = Field(default_factory=list)
    why: str


def _hit_from_record(
    record: MemoryRecord, *, score: float, rank: int, matched_on: str
) -> RetrievalHit:
    """Build a `RetrievalHit` from a record, with a one-line reason.

    Args:
        record: The archive record that matched.
        score: Rank score, interpreted per `matched_on`.
        rank: 1-based position.
        matched_on: ``"tag"`` or ``"embedding"``.

    Returns:
        The hit. `matched_tags` is left empty here; the tag retriever fills it
        in, because only it knows which tags were requested.
    """
    if matched_on == "tag":
        why = f"carries tag(s) {sorted(record.tags)}"
    else:
        why = f"embedding cosine {score:.4f} against the candidate draft"
    return RetrievalHit(
        record_id=record.record_id,
        layer=record.layer,
        kind=record.kind,
        title=record.title,
        score=round(score, 6),
        rank=rank,
        matched_on=matched_on,  # type: ignore[arg-type]
        why=why,
    )


# ==========================================================================
# Retriever 1: retrieve(store, tags)
# ==========================================================================


class TagQuery(_Base):
    """Arguments for ``retrieve_by_tags``.

    Doc 2.6: notes are "tagged by unit/topic and version", and Lab 2's
    acceptance criterion already requires "correctly-tagged candidates". So the
    filter is exact and total, not fuzzy: a record either carries a tag or it
    does not, and a query asking for four tags either returns records carrying
    all four (`match="all"`) or records carrying at least one (`match="any"`).

    Attributes:
        course_code: Which course's archive to search. Required, because every
            other layer of this worklet is course-scoped.
        tags: The tags to look for. At least one; a tag query with no tags is
            a request for the whole archive, which is what
            :class:`NotesQuery` already is.
        match: ``"all"`` (default) or ``"any"``. ``"all"`` is the default
            because a plan that lists four topics means all four, and
            quietly returning records with one of them is how a coverage claim
            gets overstated.
        kinds: Restrict to these record kinds. Empty means both notes and
            submissions. Worth setting: a tag like ``late`` exists only on
            submissions, and a caller asking for it will be told so by
            ``kinds_present`` rather than by an empty result.
        current_only: Restrict to the authoritative version of each note. The
            document's guardrail is a clear version history, which means old
            versions are *kept*; this controls what is returned, not what is
            stored.
    """

    course_code: str = Field(min_length=1, examples=["CS201"])
    tags: list[str] = Field(min_length=1, examples=[["tree", "unit:3"]])
    match: Literal["all", "any"] = "all"
    kinds: list[str] = Field(default_factory=list)
    current_only: bool = False


class TagResult(_Base):
    """Verified output of the tag retrieval.

    Lab 4's acceptance criterion is "For a given task, retrieval returns
    correctly-tagged candidates". :attr:`satisfied` is the direct read of that,
    computed by re-checking every returned record's tags rather than by
    trusting the filter that produced it.

    Attributes:
        course_code: Echo of the query.
        tags_requested: The normalised tags looked for.
        match: ``"all"`` or ``"any"``, as applied.
        kinds_requested: Kind filter, empty meaning all.
        current_only: Whether the version filter was applied.
        hits: The matching records, best first.
        scanned: How many archive records were examined. Reported so a zero-hit
            result can be told apart from a search over nothing.
        tag_union: Every tag present in the scanned corpus. Lets a caller see
            the vocabulary before guessing at a tag that does not exist.
        kinds_present: Which kinds the hits came from.
        layer_read: The layer this result was read from. Always `ARCHIVE`.
        satisfied: True when every hit genuinely carries the requested tags
            under the requested match rule. Recomputed from the records, not
            assumed from the filter.
        unsatisfied_hit_ids: Ids of any hit that failed that re-check. Empty
            when `satisfied`; non-empty is a bug report, not a ranking.
    """

    course_code: str
    tags_requested: list[str] = Field(default_factory=list)
    match: Literal["all", "any"] = "all"
    kinds_requested: list[str] = Field(default_factory=list)
    current_only: bool = False
    hits: list[RetrievalHit] = Field(default_factory=list)
    scanned: int = 0
    tag_union: list[str] = Field(default_factory=list)
    kinds_present: list[str] = Field(default_factory=list)
    layer_read: MemoryLayer = MemoryLayer.ARCHIVE
    satisfied: bool = True
    unsatisfied_hit_ids: list[str] = Field(default_factory=list)
    embedder: Optional[str] = None

    @property
    def hit_count(self) -> int:
        """How many records matched."""
        return len(self.hits)

    def top_ids(self, limit: int = 5) -> list[str]:
        """The first `limit` hit ids, in rank order.

        Args:
            limit: How many to return. Negative or zero yields an empty list.

        Returns:
            Ids, best-ranked first.
        """
        return [hit.record_id for hit in self.hits[: max(0, limit)]]

    def check_against(self, store: MemoryStore) -> "TagResult":
        """Re-derive this result from the store and report any disagreement.

        The check is independent of the filter that produced the result: it
        pulls the records back out of the archive by id and re-tests their
        tags. A retriever that returned a record without the tag asked for
        would be caught here even if its own bookkeeping said otherwise.

        Args:
            store: The archive the result was read from.

        Returns:
            A copy of this result with `satisfied` and `unsatisfied_hit_ids`
            recomputed. `unchanged` says whether the recomputation agreed with
            what the retriever reported.

        Raises:
            MemoryError: If a hit id is not in the store, which would mean the
                retriever invented a record.
        """
        unsatisfied: list[str] = []
        wanted = set(self.tags_requested)
        for hit in self.hits:
            record = store.get(hit.record_id)
            if record is None:
                raise MemoryError(
                    f"tag retrieval returned {hit.record_id!r}, which is not in "
                    "the archive; the retriever reported a record that does "
                    "not exist"
                )
            if not _tags_match(record.tags, wanted, self.match):
                unsatisfied.append(hit.record_id)
        return self.model_copy(
            update={
                "satisfied": not unsatisfied,
                "unsatisfied_hit_ids": unsatisfied,
            }
        )

    @property
    def unchanged(self) -> bool:
        """Alias kept for readability at call sites reading a check result."""
        return self.satisfied


def _tags_match(
    record_tags: list[str], wanted: set[str], match: str
) -> bool:
    """Does a record satisfy a tag filter?

    Args:
        record_tags: The record's tags.
        wanted: The requested tags.
        match: ``"all"`` or ``"any"``.

    Returns:
        True when the record satisfies the rule.
    """
    present = set(record_tags)
    if match == "all":
        return wanted.issubset(present)
    return bool(wanted & present)


def register_retrieve_by_tags(
    registry: MemoryRegistry, store: MemoryStore
) -> MemoryRegistry:
    """Register ``retrieve_by_tags`` against the archive.

    Args:
        registry: The registry to add to.
        store: The archive. Must be an `ARCHIVE`-layer store; the registry
            refuses anything else, which is the boundary doing its job.

    Returns:
        The same registry, for chaining.

    Raises:
        MemoryBoundaryError: If `store` is not an archive.
    """

    @registry.retriever(
        "retrieve_by_tags",
        description=(
            "Find archived material by exact tag. Tags are the store's own: a "
            "note carries its topics plus 'unit:<n>' and its artifact kind; a "
            "submission carries 'assignment:<id>', 'status:<s>' and late/on_time. "
            "Use this for a coverage question ('do we have material tagged X?'). "
            "Do not use it for 'what is similar to this text' -- that is "
            "retrieve_similar_prior."
        ),
        query_model=TagQuery,
        result_model=TagResult,
        doc_ref=f"{DOC_REF} pseudocode hits = retrieve(store, tags)",
        reads=MemoryLayer.ARCHIVE,
        store=store,
    )
    def retrieve_by_tags(
        query: TagQuery, store: MemoryStore, ledger: RetrievalLedger
    ) -> TagResult:
        """Filter the archive by tag.

        Reads only. Ranking is a stable secondary key rather than a score, so
        that the same query twice gives the same order and a report can cite
        "the three notes tagged traversal" without the set changing between
        two mentions.

        Args:
            query: Which tags, on which course.
            store: The archive to search.
            ledger: Unused here -- the registry records the call -- but present
                so a composite retriever can consult it.

        Returns:
            A `TagResult` whose `satisfied` was computed by re-checking each
            hit, not assumed from the filter.
        """
        wanted = {t.strip().lower() for t in query.tags if t.strip()}
        corpus = store.records()
        candidates = corpus
        if query.kinds:
            wanted_kinds = set(query.kinds)
            candidates = [r for r in candidates if r.kind in wanted_kinds]
        if query.current_only:
            candidates = [r for r in candidates if r.is_current]

        matched_records = [
            record for record in candidates if _tags_match(record.tags, wanted, query.match)
        ]
        # Score is the fraction of the *requested* tags present, so an
        # `all` query gives 1.0 for every hit and the number is honest rather
        # than decorative. Ties break on record_id for a stable order.
        def sort_key(record: MemoryRecord) -> tuple[float, str]:
            """Rank by tag coverage, then id, for a deterministic order."""
            present = set(record.tags)
            covered = len(wanted & present)
            return (-(covered / len(wanted)), record.record_id)

        matched_records.sort(key=sort_key)

        hits: list[RetrievalHit] = []
        for index, record in enumerate(matched_records, 1):
            hit = _hit_from_record(
                record,
                score=round(len(wanted & set(record.tags)) / len(wanted), 6),
                rank=index,
                matched_on="tag",
            )
            hit = hit.model_copy(
                update={
                    "matched_tags": sorted(wanted & set(record.tags)),
                    "why": (
                        f"carries {len(wanted & set(record.tags))}/{len(wanted)} "
                        f"requested tag(s): {sorted(wanted & set(record.tags))}"
                    ),
                }
            )
            hits.append(hit)

        # The acceptance criterion, computed from the records themselves.
        unsatisfied = [
            hit.record_id
            for hit, record in zip(hits, matched_records)
            if not _tags_match(record.tags, wanted, query.match)
        ]

        log_event(
            logger,
            "tag_retrieval",
            course_code=query.course_code,
            tags=sorted(wanted),
            match=query.match,
            scanned=len(corpus),
            hits=len(hits),
            satisfied=not unsatisfied,
        )
        return TagResult(
            course_code=query.course_code,
            tags_requested=sorted(wanted),
            match=query.match,
            kinds_requested=sorted(query.kinds),
            current_only=query.current_only,
            hits=hits,
            scanned=len(corpus),
            tag_union=sorted({t for r in corpus for t in r.tags}),
            kinds_present=sorted({r.kind for r in matched_records}),
            layer_read=store.layer,
            satisfied=not unsatisfied,
            unsatisfied_hit_ids=unsatisfied,
        )

    return registry


# ==========================================================================
# Retriever 2: retrieve(history, embed(candidate), k=5)
# ==========================================================================


class SimilarityQuery(_Base):
    """Arguments for ``retrieve_similar_prior``.

    The query text is the *draft* -- doc 2.10 Lab 4 writes
    ``embed(candidate)`` -- so the retriever takes a text rather than a record
    id, and a caller can hand it a `MemoryRecord.content` without a lookup.

    Attributes:
        course_code: Which course's history to search.
        text: The candidate to compare against the archive. Must be
            non-blank: a blank candidate has no direction, and returning the
            archive's own order would look like a confident answer.
        k: How many similar items to return. The document's pseudocode says
            ``k=5``, so that is the default.
        kinds: Restrict to these record kinds. **Defaults to
            ``["submission"]``**, because the document's word for what this
            retriever searches is *history*: "the history of past assignment
            submissions", and "returns ... similar prior items".

            That default is a correction, not a preference. The empty-list
            default this used to have meant "no filter", and on the seed corpus
            that returned the linked-lists **lecture notes** as the nearest
            prior item for a linked-list draft, ahead of every student's actual
            submission -- because a note on the topic genuinely is closer to a
            draft on the topic than a classmate's differently-worded answer is.
            A similarity screen that answers "have you seen this before?" with
            the course's own textbook is worse than one that returns nothing: it
            looks like evidence and is not.

            Pass ``["note_material"]`` or a wider list deliberately to search
            the notes as well, which is a legitimate different question.
        tag_filter: Optional extra tags a candidate must also carry, same
            exact-match rule as the tag retriever. This is how a caller keeps
            the comparison inside one assignment: a linked-list answer compared
            against a normalisation answer produces a number that means
            nothing.
        current_only: Restrict to authoritative note versions.
    """

    course_code: str = Field(min_length=1, examples=["CS201"])
    text: str = Field(min_length=1, examples=["A linked list stores nodes joined by pointers."])
    k: int = Field(default=5, ge=1, le=50, examples=[5])
    kinds: list[str] = Field(default_factory=lambda: [KIND_SUBMISSION])
    tag_filter: list[str] = Field(default_factory=list)
    current_only: bool = False


class SimilarityResult(_Base):
    """Verified output of the similarity retrieval.

    Lab 4's other half of the acceptance criterion is "and similar prior
    items". :attr:`top_match_id` is the direct read of that.

    Attributes:
        course_code: Echo of the query.
        k: How many were asked for.
        hits: The nearest records, best first. Scores are cosine similarities
            and are comparable with each other, not with a tag result's score.
        corpus_size: How many archive records were compared. A one-hit result
            over a fourteen-record corpus is a real result; a one-hit result
            over one record is an echo, and the two must be distinguishable.
        excluded_by_kind: How many records the kind filter removed.
        excluded_by_tag: How many the tag filter removed.
        embedder: Which backend produced the vectors. A score without it is not
            reproducible, which is why it travels with every result.
        embedder_dim: That backend's vector width.
        layer_read: The layer read. Always `ARCHIVE`.
        threshold_used: The triage threshold in force, taken from the embedder
            that produced the scores, so a reader can see which number a flag
            will be judged against and against which model.
        top_match_id: The highest-scoring record's id, or None when there was
            nothing to compare.
        top_score: That record's score, or 0.0 when there is no hit.
    """

    course_code: str
    k: int = 5
    hits: list[RetrievalHit] = Field(default_factory=list)
    corpus_size: int = 0
    excluded_by_kind: int = 0
    excluded_by_tag: int = 0
    embedder: Optional[str] = None
    embedder_dim: int = 0
    layer_read: MemoryLayer = MemoryLayer.ARCHIVE
    threshold_used: float = TRIAGE_THRESHOLD_FASTEMBED
    top_match_id: Optional[str] = None
    top_score: float = 0.0

    @property
    def hit_count(self) -> int:
        """How many similar items came back."""
        return len(self.hits)

    def top_ids(self, limit: int = 5) -> list[str]:
        """The first `limit` hit ids, best-scoring first."""
        return [hit.record_id for hit in self.hits[: max(0, limit)]]


def register_retrieve_similar_prior(
    registry: MemoryRegistry,
    store: MemoryStore,
    embedder: Optional[Embedder] = None,
) -> MemoryRegistry:
    """Register ``retrieve_similar_prior`` against the archive.

    Args:
        registry: The registry to add to.
        store: The archive. Must be an `ARCHIVE`-layer store.
        embedder: The embedding backend. Omit to build the configured one via
            :func:`~course_organizer.memory.embeddings.build_embedder`, which
            reads `EMBEDDING_BACKEND`.

    Returns:
        The same registry, for chaining.

    Raises:
        MemoryBoundaryError: If `store` is not an archive.
    """

    @registry.retriever(
        "retrieve_similar_prior",
        description=(
            "Find archived items similar to a candidate text by embedding "
            "cosine, and return them ranked. Use this to seed a no-repeat or "
            "similarity check: it returns a score and a ranking, and it does "
            "NOT return a judgement. Never describe a result of this tool as "
            "evidence of plagiarism -- routing to a human is its only decision."
        ),
        query_model=SimilarityQuery,
        result_model=SimilarityResult,
        doc_ref=f"{DOC_REF} pseudocode prior = retrieve(history, embed(candidate), k=5)",
        reads=MemoryLayer.ARCHIVE,
        store=store,
    )
    def retrieve_similar_prior(
        query: SimilarityQuery, store: MemoryStore, ledger: RetrievalLedger
    ) -> SimilarityResult:
        """Embed the candidate and rank the archive against it.

        Embedding is done once for the query and once for the corpus, then
        compared pairwise. The corpus vectors are recomputed per call rather
        than cached, because the archive is read-only for the life of a run --
        so caching would add a stale-vector failure mode to buy nothing.

        Args:
            query: The candidate, its `k`, and any filters.
            store: The archive to search.
            ledger: Unused directly; the registry records the call.

        Returns:
            A `SimilarityResult`, or a failed `RetrievalResult` if the backend
            cannot embed (the registry turns that into ``ok=False``).

        Raises:
            EmbeddingError: Propagated from the embedder. The registry records
                it as a failed call rather than propagating, so a missing model
                shows up in the ledger instead of aborting the run.
        """
        engine = embedder if embedder is not None else build_embedder()
        threshold = engine.triage_threshold

        candidates = store.records()
        excluded_kind = 0
        excluded_tag = 0
        if query.kinds:
            wanted_kinds = set(query.kinds)
            before = len(candidates)
            candidates = [r for r in candidates if r.kind in wanted_kinds]
            excluded_kind = before - len(candidates)
        if query.current_only:
            candidates = [r for r in candidates if r.is_current]
        if query.tag_filter:
            wanted_tags = {t.strip().lower() for t in query.tag_filter if t.strip()}
            before = len(candidates)
            candidates = [r for r in candidates if _tags_match(r.tags, wanted_tags, "all")]
            excluded_tag = before - len(candidates)

        if not candidates:
            log_event(
                logger,
                "similarity_retrieval",
                course_code=query.course_code,
                corpus_size=0,
                hits=0,
                reason="filters removed every candidate",
            )
            return SimilarityResult(
                course_code=query.course_code,
                k=query.k,
                hits=[],
                corpus_size=0,
                excluded_by_kind=excluded_kind,
                excluded_by_tag=excluded_tag,
                embedder=engine.name,
                embedder_dim=engine.dim,
                layer_read=store.layer,
                threshold_used=threshold,
                top_match_id=None,
                top_score=0.0,
            )

        query_vector = engine.embed([query.text])[0]
        corpus_vectors = engine.embed([r.content_payload() for r in candidates])
        if len(corpus_vectors[0]) != len(query_vector):
            raise EmbeddingError(
                f"embedder {engine.name!r} returned {len(corpus_vectors[0])}-dim "
                f"vectors for the corpus but {len(query_vector)}-dim for the "
                "query; a score between them would be meaningless"
            )

        scored: list[tuple[float, MemoryRecord]] = []
        for record, vector in zip(candidates, corpus_vectors):
            scored.append((cosine_similarity(query_vector, vector), record))
        # Sort by score descending, then id ascending so equal scores -- which
        # happen whenever the corpus has one record, or when every score is
        # tied -- still produce a deterministic, citable order.
        scored.sort(key=lambda pair: (-pair[0], pair[1].record_id))

        top_k = scored[: query.k]
        hits = [
            _hit_from_record(record, score=score, rank=index, matched_on="embedding")
            for index, (score, record) in enumerate(top_k, 1)
        ]

        log_event(
            logger,
            "similarity_retrieval",
            course_code=query.course_code,
            corpus_size=len(candidates),
            hits=len(hits),
            embedder=engine.name,
            top_score=round(scored[0][0], 4) if scored else 0.0,
        )
        return SimilarityResult(
            course_code=query.course_code,
            k=query.k,
            hits=hits,
            corpus_size=len(candidates),
            excluded_by_kind=excluded_kind,
            excluded_by_tag=excluded_tag,
            embedder=engine.name,
            embedder_dim=engine.dim,
            layer_read=store.layer,
            threshold_used=threshold,
            top_match_id=hits[0].record_id if hits else None,
            top_score=hits[0].score if hits else 0.0,
        )

    return registry


# ==========================================================================
# The flag: a routing signal, not a verdict
# ==========================================================================


class SimilarityFlag(_Base):
    """A similarity finding that routes to a human and decides nothing.

    This is the type the document's guardrails actually constrain, so it is
    worth being precise about what it can and cannot say.

    **It can say** that a candidate's embedding is closer to some archived
    submission than a stated threshold, and by how much. That is a comparison,
    and comparisons are what a computer is for.

    **It cannot say** that anything is plagiarism, that a student did anything,
    or that a mark should be reduced. Doc 2.4 node 4: "Flag for human review;
    never make a final plagiarism determination automatically." Doc 2.12:
    "Flag high-similarity submissions for review only; never auto-penalize."

    That is enforced, not promised. There is no ``is_plagiaristic`` field, no
    verdict field, no penalty field, and no
    :attr:`PLAGIARISM_FORBIDDEN_FIELDS`-named member; a test asserts the field
    set is disjoint from
    :data:`~course_organizer.memory.retrieval.PLAGIARISM_FORBIDDEN_FIELDS`.
    A type that cannot express a decision cannot quietly make one.

    Attributes:
        candidate_id: The draft's id. It is a *draft* id -- it starts with
            ``draft-`` -- and it is in the `RUN_STATE` layer. The flag is
            therefore visibly about something that has not been submitted, and
            the archive it was compared against is visibly a different layer.
        candidate_layer: Always `RUN_STATE`, and carried so the boundary is
            visible in the audit rather than assumed.
        compared_against: Archive record ids the candidate was ranked against.
            The full list, not just the top one, so a human reviewing the flag
            can see what else was in the running.
        top_match_id: The nearest archived record's id, or None.
        top_score: Its cosine similarity.
        threshold: The triage threshold in force, copied from the result so a
            reader sees the number and the model that produced it together.
        exceeds_threshold: True when ``top_score > threshold``. This is a fact
            about two numbers, and the name says so -- it does not say what the
            fact means.
        disposition: ``review_required`` when the threshold is exceeded, else
            ``within_threshold``. Routing only. There is no third value.
        requires_human_review: True exactly when `disposition` is
            ``review_required``. Named for the document's instruction, not for
            a machine's convenience.
        reviewed_by: Always empty. Doc 2.12 requires an audit of "every grade
            published and by whom approved", so the field a reviewer will
            eventually fill in has to exist from the start; it starts empty
            because nobody has reviewed anything yet.
        decided_by: The component that raised the flag, for the audit trail.
        authority: The only party that can turn a flag into a finding.
            Doc 2.4: faculty approve. Constant, not a parameter, so a caller
            cannot redirect the authority to an agent.
        score_source: The embedder that produced `top_score`, so a reader knows
            which model's opinion this is.
        rationale: One line, stating what the flag is and is not.
    """

    candidate_id: str
    candidate_layer: MemoryLayer = MemoryLayer.RUN_STATE
    compared_against: list[str] = Field(default_factory=list)
    top_match_id: Optional[str] = None
    top_score: float = 0.0
    threshold: float = TRIAGE_THRESHOLD_FASTEMBED
    exceeds_threshold: bool = False
    disposition: Disposition = "within_threshold"
    requires_human_review: bool = False
    reviewed_by: str = ""
    decided_by: str = "similarity_retriever"
    authority: str = HUMAN_AUTHORITY
    score_source: Optional[str] = None
    rationale: str = (
        "A cosine similarity between embeddings, compared against a triage "
        "threshold. This is a routing signal for human review. It is not a "
        "determination that any plagiarism occurred, and it carries no mark "
        "adjustment."
    )

    def summary(self) -> str:
        """One line, for CLI output and audit records."""
        target = self.top_match_id or "(no match)"
        return (
            f"{self.disposition}: candidate {self.candidate_id} vs {target} "
            f"score={self.top_score:.4f} threshold={self.threshold:.4f} "
            f"review={self.requires_human_review} by={self.decided_by}"
        )

    def is_determination(self) -> bool:
        """Always False.

        Exists so a caller that wants a determination has to write
        ``if flag.is_determination(): ...``, see the constant False, and go
        and find the human. A method that cannot return True is a small,
        permanent piece of friction in the path the document forbids.
        """
        return False


def raise_flag(
    candidate: MemoryRecord,
    result: SimilarityResult,
    *,
    threshold: Optional[float] = None,
) -> SimilarityFlag:
    """Turn a similarity result into a routing flag.

    This is the "seeds validation/no-repeat" step: retrieval produced a ranking
    and a number, and this is the only thing done with it.

    Args:
        candidate: The draft that was compared. Must be a `RUN_STATE` record;
            a caller passing an archive record is asking the question the
            document forbids an agent to answer, and is refused.
        result: The retrieval output.
        threshold: Override the triage threshold. Omit to use
            `result.threshold_used`, which is the embedder's own measured
            operating point. Pass a value only to explore a different one --
            the threshold travels on the flag, so an override shows up in the
            audit rather than disappearing into it.

    Returns:
        A `SimilarityFlag`. `exceeds_threshold` and `disposition` are derived
        from `result.top_score`; nothing else is decided.

    Raises:
        MemoryBoundaryError: If `candidate` is not in the `RUN_STATE` layer.
            A flag about an archived submission would be a flag the archive
            raises about itself, which is the "mixing them corrupts the
            baseline" failure arriving through the front door.
    """
    if candidate.layer is not MemoryLayer.RUN_STATE:
        raise MemoryBoundaryError(
            f"cannot raise a similarity flag about {candidate.record_id!r}: it "
            f"is in the {candidate.layer.value!r} layer, not run state, so it "
            f"is not a draft. "
            f"{LAYER_NOTE}"
        )

    limit = result.threshold_used if threshold is None else threshold
    exceeded = result.top_match_id is not None and result.top_score > limit
    return SimilarityFlag(
        candidate_id=candidate.record_id,
        candidate_layer=candidate.layer,
        compared_against=list(result.top_ids(limit=result.corpus_size)),
        top_match_id=result.top_match_id,
        top_score=result.top_score,
        threshold=limit,
        exceeds_threshold=exceeded,
        disposition="review_required" if exceeded else "within_threshold",
        requires_human_review=exceeded,
        score_source=result.embedder,
    )


LAYER_NOTE = (
    "the similarity flag exists to compare an in-flight draft against the "
    "archive, not to compare the archive against itself; the no-repeat and "
    "validation questions both start from a candidate"
)


def flag_field_names() -> frozenset[str]:
    """Every field name on :class:`SimilarityFlag`.

    Exists so the honesty test can read the real field set rather than a
    hand-maintained list that could fall out of step with the class.
    """
    return frozenset(SimilarityFlag.model_fields)


__all__ = [
    "DOC_REF",
    "KIND_CANDIDATE_DRAFT",
    "KIND_SUBMISSION",
    "PLAGIARISM_FORBIDDEN_FIELDS",
    "TRIAGE_THRESHOLD_FASTEMBED",
    "Disposition",
    "MemoryError",
    "MemoryRegistry",
    "RetrievalHit",
    "SimilarityFlag",
    "SimilarityQuery",
    "SimilarityResult",
    "TagQuery",
    "TagResult",
    "flag_field_names",
    "raise_flag",
    "register_retrieve_by_tags",
    "register_retrieve_similar_prior",
]
