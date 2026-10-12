"""The memory layer: short-term run state, long-term archive, and retrieval.

Document 2.10, Lab 4, gives the goal in one sentence:

    "Separate short-term run state from long-term stored knowledge."

This package is that sentence, made structural.

    run (RunState)                       archive (ArchiveStore)
    ---------------                      ----------------------
    state.draft = candidate    ----->    hits  = retrieve(store, tags)
    state.draft = candidate    ----->    prior = retrieve(history,
                                     embed(candidate), k=5)

The arrows only go right. A run-state record cannot enter the archive, a
retriever declared for the archive cannot be handed the run state, and the
archive's content fingerprint is taken before and after every run so the claim
"this run did not touch long-term memory" is a measurement rather than a
promise.

Why the ledger is here, not in Lab 6
-----------------------------------
Lab 2 logs tool calls, Lab 3 logs skill calls, and this logs retrievals. Three
governed action kinds, three append-only ledgers, and Lab 6's job is to make
all three durable and to wrap them in a budget. Building the third ledger now
is what makes that a wrapping job rather than a rewrite.

| Module | Role |
|--------|------|
| `base` | `MemoryLayer`, `MemoryRecord`, `MemoryStore` (the boundary), `MemoryRegistry`, `RetrievalLedger` |
| `stores` | `RunState` and `ArchiveStore` -- and the only place a `MemoryRecord` is built |
| `embeddings` | `HashingEmbedder` (default, offline) and `FastEmbedEmbedder` (`BAAI/bge-small-en-v1.5`), plus `cosine_similarity` |
| `retrieval` | `retrieve_by_tags`, `retrieve_similar_prior`, and `SimilarityFlag` |

The honesty constraint, in one sentence
--------------------------------------
`SimilarityFlag` can rank and score. It cannot call anything plagiarism and it
cannot adjust a mark, because the type has no field for either -- see
:data:`~course_organizer.memory.retrieval.PLAGIARISM_FORBIDDEN_FIELDS` and
doc 2.4 node 4 / 2.12. Lab 4 *seeds* the similarity check; the retrieval layer
is not where the determination is made, and it is built so that it cannot make
one by accident.
"""

from course_organizer.memory.base import (
    LAYER_PURPOSE,
    RUN_STATE_PROVENANCE,
    MemoryBoundaryError,
    MemoryError,
    MemoryLayer,
    MemoryRecord,
    MemoryRegistry,
    MemoryStore,
    RetrievalCall,
    RetrievalLedger,
    RetrievalResult,
    RetrieverSpec,
)
from course_organizer.memory.embeddings import (
    FASTEMBED_MODEL,
    HASHING_DIM,
    NGRAM_RANGE,
    TRIAGE_THRESHOLD_FASTEMBED,
    TRIAGE_THRESHOLD_HASHING,
    Embedder,
    EmbeddingError,
    FastEmbedEmbedder,
    HashingEmbedder,
    build_embedder,
    cosine_similarity,
)
from course_organizer.memory.retrieval import (
    PLAGIARISM_FORBIDDEN_FIELDS,
    Disposition,
    RetrievalHit,
    SimilarityFlag,
    SimilarityQuery,
    SimilarityResult,
    TagQuery,
    TagResult,
    flag_field_names,
    raise_flag,
    register_retrieve_by_tags,
    register_retrieve_similar_prior,
)
from course_organizer.memory.stores import (
    KIND_CANDIDATE_DRAFT,
    KIND_NOTE_MATERIAL,
    KIND_SUBMISSION,
    ArchiveStore,
    RunState,
    draft_to_record,
)

#: The retrievers this package registers, in the order the document's
#: pseudocode reaches for them. Used by Lab 4's CLI and by the test that
#: asserts no undeclared retriever was ever invoked.
RETRIEVER_NAMES: tuple[str, ...] = (
    "retrieve_by_tags",
    "retrieve_similar_prior",
)

#: The two layers, named in the order the document introduces them.
LAYERS: tuple[MemoryLayer, ...] = (MemoryLayer.RUN_STATE, MemoryLayer.ARCHIVE)


def build_default_memory_registry(
    *,
    run_id: str,
    course_code: str,
    archive: ArchiveStore,
    embedder: Embedder | None = None,
) -> MemoryRegistry:
    """Build the Lab 4 registry: the two retrievers, both archive-bound.

    Only the two long-term retrievers are registered. Run state has no
    retriever of its own on purpose -- a "search my own draft" operation is
    not one the document asks for, and adding one would put a retriever on the
    short-term side of a boundary whose whole point is that the short-term
    side is not searched.

    Args:
        run_id: Correlates retrievals with the logging run id and ledger.
        course_code: Which course this run is about.
        archive: The long-term store. Must already be loaded, or the first
            retrieval will say so with an actionable message.
        embedder: The embedding backend for the similarity retriever. Omit to
            use the one `EMBEDDING_BACKEND` selects.

    Returns:
        A `MemoryRegistry` covering only the `ARCHIVE` layer.

    Raises:
        MemoryBoundaryError: If `archive` is not an `ArchiveStore`.
    """
    if not isinstance(archive, ArchiveStore):
        raise MemoryBoundaryError(
            f"build_default_memory_registry needs an ArchiveStore, got "
            f"{type(archive).__name__} on layer {archive.layer.value!r}. "
            f"{LAYER_PURPOSE[archive.layer]} is not "
            f"{LAYER_PURPOSE[MemoryLayer.ARCHIVE]}."
        )

    registry = MemoryRegistry(run_id=run_id)
    register_retrieve_by_tags(registry, archive)
    register_retrieve_similar_prior(registry, archive, embedder)
    return registry


__all__ = [
    "FASTEMBED_MODEL",
    "HASHING_DIM",
    "KIND_CANDIDATE_DRAFT",
    "KIND_NOTE_MATERIAL",
    "KIND_SUBMISSION",
    "LAYER_PURPOSE",
    "LAYERS",
    "NGRAM_RANGE",
    "PLAGIARISM_FORBIDDEN_FIELDS",
    "RETRIEVER_NAMES",
    "RUN_STATE_PROVENANCE",
    "TRIAGE_THRESHOLD_FASTEMBED",
    "TRIAGE_THRESHOLD_HASHING",
    "ArchiveStore",
    "Disposition",
    "Embedder",
    "EmbeddingError",
    "FastEmbedEmbedder",
    "HashingEmbedder",
    "MemoryBoundaryError",
    "MemoryError",
    "MemoryLayer",
    "MemoryRecord",
    "MemoryRegistry",
    "MemoryStore",
    "RetrievalCall",
    "RetrievalHit",
    "RetrievalLedger",
    "RetrievalResult",
    "RetrieverSpec",
    "RunState",
    "SimilarityFlag",
    "SimilarityQuery",
    "SimilarityResult",
    "TagQuery",
    "TagResult",
    "build_default_memory_registry",
    "build_embedder",
    "cosine_similarity",
    "draft_to_record",
    "flag_field_names",
    "raise_flag",
    "register_retrieve_by_tags",
    "register_retrieve_similar_prior",
]
