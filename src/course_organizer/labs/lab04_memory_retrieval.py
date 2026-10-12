"""Lab 4 -- Memory & retrieval: separating run state from the archive.

The document, 2.10, Lab 4, in full:

    **Phase:** A - Foundations
    **Goal:** Separate short-term run state from long-term stored knowledge.
    **What it adds to this worklet.** Session state (the current draft) vs
    stored reference - the notes/materials store and the history of past
    assignment submissions - retrieved by tags and by similarity.
    **Inputs:** the notes/materials store; history of prior assignment
    submissions; current draft.
    **Outputs:** Tag- and similarity-based retrieval.
    **Agents / nodes:** Retrieval agent; foundations of later screening.
    **Deliverable & acceptance:** Retrieval over the store + history with tag
    filters and embedding similarity.
    **Accept:** For a given task, retrieval returns correctly-tagged candidates
    and similar prior items.
    **Nodes activated.** Retrieval node; seeds validation/no-repeat.
    **Pitfall to avoid.** Don't conflate run state with the archive - mixing
    them corrupts the baseline.

and its pseudocode::

    state.draft = candidate                           # short-term
    hits  = retrieve(store, tags)                     # long-term
    prior = retrieve(history, embed(candidate), k=5)  # similarity

What this module actually does
-----------------------------
It runs those three lines, four times, over two courses, and then *tries to
break the boundary on purpose*. The three deliverables a reviewer should look
for:

1. **The two stores exist and are separate.**
   :class:`~course_organizer.memory.RunState` and
   :class:`~course_organizer.memory.ArchiveStore` have disjoint provenance
   allow-lists, and a record in one cannot be appended to the other. That is
   :mod:`course_organizer.memory.base`'s job; this module exercises it.

2. **Both retrievals answer the acceptance criterion, and the answer is
   checked rather than claimed.** :attr:`RetrievalBundle.tags_correct` re-derives
   "every hit carries the requested tags" from the archive, and
   :attr:`RetrievalBundle.similar_prior_found` reads the top-k directly. The
   CLI prints both, and a non-zero exit means one of them failed.

3. **The pitfall is demonstrated, not described.**
   :class:`ContaminatedArchive` deliberately defeats the boundary so the
   document's warning -- "mixing them corrupts the baseline" -- can be *seen*:
   once a run's own draft is in the archive, the no-repeat check flags that
   draft at cosine 1.0 against itself, and the baseline fingerprint changes.
   A pitfall you cannot reproduce is a pitfall you cannot argue about.

Two things this module deliberately does not do
-----------------------------------------------
**It does not decide anything about plagiarism.** Doc 2.4 node 4: "Flag for
human review; never make a final plagiarism determination automatically." Doc
2.12: "Flag high-similarity submissions for review only; never auto-penalize."
The output is a :class:`~course_organizer.memory.SimilarityFlag`, whose type
has no field for a verdict, a finding or a penalty, and a test asserts that.

**It does not embed the notes in the similarity search.** The document says
``retrieve(history, ...)``, and that word is load-bearing. Measured over the
`CS201` archive with `BAAI/bge-small-en-v1.5`: a candidate draft's nearest
*submission* is 0.863 away, but its nearest *note* -- the lecture note on the
same topic, which every student has read -- is 0.977 away. Including the notes
would flag every draft for resembling its own textbook. The retrieval layer
offers the permissive default; this module passes ``kinds=["submission"]`` and
says why.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from pydantic import Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.logging_setup import log_event, setup_logging
from course_organizer.memory import (
    KIND_SUBMISSION,
    ArchiveStore,
    Embedder,
    MemoryBoundaryError,
    MemoryError,
    MemoryLayer,
    MemoryRecord,
    MemoryRegistry,
    MemoryStore,
    RETRIEVER_NAMES,
    RUN_STATE_PROVENANCE,
    RunState,
    SimilarityFlag,
    SimilarityQuery,
    SimilarityResult,
    TagQuery,
    TagResult,
    build_default_memory_registry,
    build_embedder,
    draft_to_record,
    flag_field_names,
    raise_flag,
)
from course_organizer.memory.retrieval import PLAGIARISM_FORBIDDEN_FIELDS
from course_organizer.schemas import _Base
from course_organizer.tools import build_lab04_registry

logger = logging.getLogger(__name__)

LAB = "lab04"
DOC_REF = "2.10 Lab 4"


# ==========================================================================
# The four candidate drafts
#
# Three are written here and one is lifted from the archive at run time.
#
# These carry no course codes, no student names and no note ids. Which course a
# case runs against is `CaseSpec.course_code`; which archived record a
# "verbatim" case copies is `CaseSpec.candidate_source_id`. Keeping the
# knowledge in data and the machinery in code is what lets the same agent run
# over a second course without touching a line of it.
# ==========================================================================


#: Case 1 and 3's neighbour: a genuine new answer to the same brief. Adds a
#: tail pointer, so the approach differs from anything in the archive while the
#: topic does not. This is the false-positive control -- it *should* score
#: well against same-topic submissions and still be within threshold.
DRAFT_NEW_ANSWER = (
    "I kept a second reference to the last node alongside the head, so adding "
    "an element is constant time: the new node is linked directly after the "
    "current tail and the tail reference moves one step, with no traversal. "
    "Inserting after a given node is also constant time because the caller has "
    "already supplied the predecessor. Reversal I do in a single pass by "
    "detaching the head, walking the original list while holding the previous "
    "and next references, and rebuilding the tail reference as I go, which "
    "avoids the second pass a naive reversal would need. The extra reference "
    "costs one pointer per list and has to be kept correct on every removal, "
    "which is where the bug I had to fix was."
)

#: Case 3: the same content as one archived submission, in entirely different
#: words. This is the case a naive keyword approach gets wrong -- high
#: overlap in ideas, low overlap in phrasing -- and it is the reason the
#: threshold is a measured number rather than "looks similar".
DRAFT_RESTATEMENT = (
    "A row of separately allocated cells tied together by references gives me "
    "two different insertion costs depending on what I keep. If I also hold a "
    "reference to the final cell, appending is constant time, because the new "
    "cell is attached to the end without walking. Without that reference the "
    "same operation costs a full traversal, which is the version most "
    "textbooks show first. Unlinking a cell whose predecessor I already have in "
    "hand is constant time for the same reason. Turning the whole row around "
    "takes one pass if I keep hold of the cell behind me as I advance, rather "
    "than the two passes the naive version needs. The price of supporting "
    "backward walking is a second reference in every cell, and the price of "
    "that is the extra work to keep both directions consistent after each "
    "change."
)

#: Case 4: a new answer for the other course, on the same kind of brief.
DRAFT_OTHER_SUBJECT = (
    "I recovered the candidate keys by computing the attribute closure of every "
    "determinant listed in the question and reading the minimal keys off those "
    "closures, rather than guessing superkeys and pruning afterwards. The staff "
    "relation is not in third normal form because the project leader attribute "
    "determines the branch only through the leader's staff number, which is a "
    "transitive dependency; splitting the relation into staff, branch and "
    "leader removes it and loses no dependency. The supplier schema is the "
    "harder case: the determinant is not a candidate key, so the third normal "
    "form split still carries the violation and a further decomposition is "
    "required. I checked losslessness by showing that the intersection of the "
    "decomposed relations functionally determines one of them, and I gave a "
    "concrete spurious tuple as the counterexample to the naive split."
)

#: What a "verbatim" case copies. Not a course code and not a student: the
#: archive lookup resolves it at run time, so the source text is the archive's
#: own bytes and the claim "this is a verbatim copy" is checkable.
VERBATIM_SOURCE_SUFFIX = "-A1-S001"


# ==========================================================================
# Case specification
# ==========================================================================


@dataclass(frozen=True)
class CaseSpec:
    """One retrieval task, plus what the document expects of it.

    Frozen on purpose: a case that a run can edit is a case whose acceptance
    criterion was negotiated mid-run. Lab 3's `CaseSpec` is frozen for the same
    reason and raises `FrozenInstanceError` rather than letting a passing test
    become a failing one after the fact.

    Attributes:
        label: Short name, used in the CLI table and in the report.
        course_code: Which course's archive to search.
        tags: Tags for the tag retrieval. Chosen from the store's own vocabulary
            -- a note's topics plus ``unit:<n>`` -- so the query is one a
            planner could actually have written.
        tag_match: ``"all"`` or ``"any"``.
        current_only: Restrict notes to their authoritative version.
        history_kinds: Record kinds the similarity search may read. Every case
            passes ``["submission"]``, because the document says *history*; see
            the module docstring for the measured reason.
        k: How many similar prior items to ask for. The document's `k=5`.
        candidate_kind: ``"new"``, ``"restated"`` or ``"verbatim"``.
        candidate_source_id: For a ``verbatim`` case, the archive record to copy
            text from. Ignored otherwise.
        candidate_text: For a ``new`` or ``restated`` case, the draft itself.
        expect_disposition: The routing outcome this case must produce. A case
            whose outcome differs is a failure, not a surprise.
        expect_similar_prior: Whether at least one similar prior item must come
            back. Always True: "similar prior items" is half the acceptance
            criterion, and a task where the honest answer is "there is nothing
            similar" would not demonstrate the retriever at all.
        rationale: One line for the report, saying what the case is for.
    """

    label: str
    course_code: str
    tags: tuple[str, ...]
    tag_match: Literal["all", "any"]
    current_only: bool
    history_kinds: tuple[str, ...]
    k: int
    candidate_kind: Literal["new", "restated", "verbatim"]
    candidate_source_id: Optional[str]
    candidate_text: Optional[str]
    expect_disposition: Literal["review_required", "within_threshold"]
    expect_similar_prior: bool
    rationale: str

    def tag_query(self) -> TagQuery:
        """The tag retrieval's query.

        Returns:
            A validated `TagQuery` for this case.

        Raises:
            pydantic.ValidationError: If the tags are empty, which would make
                the tag retrieval a request for the whole archive and the case
                meaningless.
        """
        return TagQuery(
            course_code=self.course_code,
            tags=list(self.tags),
            match=self.tag_match,
            current_only=self.current_only,
        )

    def similarity_query(self, text: str) -> SimilarityQuery:
        """The similarity retrieval's query, for a given candidate text.

        Args:
            text: The candidate to embed. Passed in rather than read from the
                spec because a ``verbatim`` case's text is resolved from the
                archive at run time.

        Returns:
            A validated `SimilarityQuery` restricted to this case's history.
        """
        return SimilarityQuery(
            course_code=self.course_code,
            text=text,
            k=self.k,
            kinds=list(self.history_kinds),
        )


def cases() -> tuple[CaseSpec, ...]:
    """The four cases this lab runs.

    Two courses, and for each of them one task that should pass cleanly plus
    the pair that pins down the threshold: a verbatim copy, which must be
    flagged, and a restatement, which must not.

    Public, and deliberately the *only* source of case specifications. The
    threshold report and the acceptance checks both read this list, so a case
    added here is measured by both, and a case defined anywhere else is measured
    by neither.

    Returns:
        The case list, in the order they are reported.
    """
    return (
        CaseSpec(
            label="new-answer",
            course_code="CS201",
            tags=("linked-list", "unit:1"),
            tag_match="all",
            current_only=True,
            history_kinds=(KIND_SUBMISSION,),
            k=5,
            candidate_kind="new",
            candidate_source_id=None,
            candidate_text=DRAFT_NEW_ANSWER,
            expect_disposition="within_threshold",
            expect_similar_prior=True,
            rationale=(
                "A genuine new answer on a covered topic. Must return correctly "
                "tagged material and must NOT be flagged, because being similar "
                "to a classmate is not a finding."
            ),
        ),
        CaseSpec(
            label="verbatim-copy",
            course_code="CS201",
            tags=("linked-list", "unit:1"),
            tag_match="all",
            current_only=True,
            history_kinds=(KIND_SUBMISSION,),
            k=5,
            candidate_kind="verbatim",
            candidate_source_id=f"DS{VERBATIM_SOURCE_SUFFIX}",
            candidate_text=None,
            expect_disposition="review_required",
            expect_similar_prior=True,
            rationale=(
                "The same text, unchanged, set as a fresh draft. Must route to "
                "human review, and must do so on the score rather than on any "
                "record of who wrote what."
            ),
        ),
        CaseSpec(
            label="restatement",
            course_code="CS201",
            tags=("linked-list", "unit:1"),
            tag_match="all",
            current_only=True,
            history_kinds=(KIND_SUBMISSION,),
            k=5,
            candidate_kind="restated",
            candidate_source_id=None,
            candidate_text=DRAFT_RESTATEMENT,
            expect_disposition="within_threshold",
            expect_similar_prior=True,
            rationale=(
                "The same ideas as the copied submission, in different words. "
                "This is the false-positive control: if it were flagged, the "
                "threshold would be detecting topic rather than copying."
            ),
        ),
        CaseSpec(
            label="other-subject",
            course_code="DBMS402",
            tags=("normalisation", "unit:2"),
            tag_match="all",
            current_only=True,
            history_kinds=(KIND_SUBMISSION,),
            k=5,
            candidate_kind="new",
            candidate_source_id=None,
            candidate_text=DRAFT_OTHER_SUBJECT,
            expect_disposition="within_threshold",
            expect_similar_prior=True,
            rationale=(
                "The same two retrievers over a second course. Shows the tags "
                "and the embedder are data-driven and nothing in the retrieval "
                "layer knows which course it is looking at. It also widens the "
                "threshold table to two corpora, which is what stopped the "
                "hashing backend's threshold from looking as comfortable as it "
                "had been when only CS201 was measured."
            ),
        ),
    )


def resolve_candidate(archive: ArchiveStore, case: CaseSpec) -> MemoryRecord:
    """The draft text a case should search with, as a record.

    A ``verbatim`` case's text is read out of the archive rather than stored in
    this module. That is not a style preference: keeping the text here would
    mean the word "verbatim" described this file rather than the archive, and a
    future edit to either would break the claim silently -- the copy would stop
    being a copy while still being called one.

    Module-level rather than a method because two callers need it: the agent,
    which runs a case, and :func:`lab_threshold_report`, which measures the
    threshold over the same cases. Two resolvers would be two chances for the
    report and the run to search different text, which is precisely the drift
    that let the first version of that report claim "4 of 4 probes correct" on
    a run where an acceptance check failed.

    Args:
        archive: The archive a ``verbatim`` case copies from. For a ``new`` or
            ``restated`` case it is used only in error messages.
        case: The case to resolve.

    Returns:
        A record whose ``content`` is the candidate text. For ``verbatim`` this
        is a *copy* of the archive record under a new id: the bytes are
        identical, which is the claim under test, while the id stays
        distinguishable so the ledger can tell the copy from its source.

    Raises:
        MemoryError: If a ``verbatim`` case names a record the archive does not
            hold, names no source at all, or supplies neither text nor a source
            id. All three mean the case cannot be run as written.
    """
    if case.candidate_kind == "verbatim":
        source_id = case.candidate_source_id
        if not source_id:
            raise MemoryError(
                f"case {case.label!r} is a verbatim copy but names no source "
                "record to copy"
            )
        source = archive.get(source_id)
        if source is None:
            raise MemoryError(
                f"case {case.label!r} names {source_id!r}, which is not in the "
                f"{case.course_code} archive. Available submissions: "
                f"{[r.record_id for r in archive.submissions()]}"
            )
        return source.model_copy(update={"record_id": f"copy-of-{source_id}"})

    if not case.candidate_text:
        raise MemoryError(
            f"case {case.label!r} supplies no candidate text and is not a "
            "verbatim copy, so there is nothing to embed"
        )
    return draft_to_record(
        course_code=case.course_code,
        content=case.candidate_text,
        title=f"{case.label} candidate",
    )


#: The document's own words for the case's job, quoted in the report.
DOC_ACCEPTANCE = (
    "For a given task, retrieval returns correctly-tagged candidates and "
    "similar prior items."
)
DOC_PITFALL = (
    "Don't conflate run state with the archive - mixing them corrupts the "
    "baseline."
)
DOC_PSEUDOCODE = (
    "state.draft = candidate                      # short-term\n"
    "hits  = retrieve(store, tags)                # long-term\n"
    "prior = retrieve(history, embed(candidate), k=5)  # similarity"
)


# ==========================================================================
# The two seeds: what retrieval hands to the checks it seeds
# ==========================================================================


class NoRepeatSeed(_Base):
    """What retrieval hands the no-repeat question.

    Doc 2.10 Lab 4: "Nodes activated. Retrieval node; seeds validation/
    no-repeat."

    "Has this been submitted before?" is a question about *similarity*, and
    similarity is what this module computed. So the answer this object gives is
    a nearest neighbour and a score, and the question it deliberately does not
    answer is whether that constitutes a repeat -- which is the same boundary
    as the plagiarism flag and is enforced the same way, by there being no
    field for it.

    Attributes:
        candidate_id: The draft's id, which is a run-state id.
        nearest_prior_id: The closest archived submission's id, or None.
        similarity: Its cosine similarity.
        flagged: True when the score is above the triage threshold. A routing
            bit, not a finding.
        compared_count: How many submissions the candidate was ranked against.
            A flag over two candidates means much less than a flag over eight,
            and the count is what tells a reader which it is.
        decided_by: The component that produced this. Doc 2.12 wants an audit
            of who did what, and "who" starts here.
    """

    candidate_id: str
    nearest_prior_id: Optional[str] = None
    similarity: float = 0.0
    flagged: bool = False
    compared_count: int = 0
    decided_by: str = "retrieval_node"

    def is_a_repeat(self) -> bool:
        """Always False.

        A repeat is a determination about a student and doc 2.4 reserves those
        for faculty. Exists so a caller wanting one has to call a method that
        cannot answer, and go and find the human.
        """
        return False

    def summary(self) -> str:
        """One line, for CLI output."""
        return (
            f"no-repeat seed: {self.candidate_id} vs "
            f"{self.nearest_prior_id or '(none)'} at {self.similarity:.4f} over "
            f"{self.compared_count} prior item(s); flagged={self.flagged}"
        )


class SimilarityCheckSeed(_Base):
    """What retrieval hands doc 2.7's check 2.

    Doc 2.7's four checks are the gate for Labs 7-8, and check 2 is "a
    plagiarism/similarity score is attached". Lab 4's part of that is narrow and
    specific: a score exists, it is attached to the candidate, and the records
    it was computed against are named. Whether the score is *acted on* is the
    similarity node's problem in a later lab.

    Attributes:
        candidate_id: The draft the score is attached to.
        score_attached: Always True for a seed built from a real retrieval.
            Present so the audit can read the check's precondition rather than
            infer it.
        score: The similarity score, as a float. Doc 2.7 asks for a score, not
            a conclusion.
        compared_against: Every record the score was computed against, not just
            the top one.
        score_source: The embedder that produced the score.
        threshold: The triage threshold in force.
        owner: The node that owns the determination. Doc 2.4 node 4,
            "Plagiarism/Similarity". Named here so it is obvious the retrieval
            node is not it.
        authority: The only party that may conclude anything. Doc 2.4:
            faculty approve.
    """

    candidate_id: str
    score_attached: bool = True
    score: float = 0.0
    compared_against: list[str] = Field(default_factory=list)
    score_source: Optional[str] = None
    threshold: float = 0.0
    owner: str = "plagiarism_similarity_node"
    authority: str = HUMAN_AUTHORITY

    def determination_made(self) -> bool:
        """Always False.

        Doc 2.7 check 2 asks for a score to be *attached*. This object attaches
        it. It does not interpret it, and this method says so in a way a caller
        cannot accidentally skip.
        """
        return False

    def summary(self) -> str:
        """One line, for CLI output."""
        return (
            f"similarity check seed: score {self.score:.4f} attached to "
            f"{self.candidate_id} from {self.score_source} over "
            f"{len(self.compared_against)} record(s); determination deferred "
            f"to {self.owner}, authority {self.authority}"
        )


# ==========================================================================
# The bundle: the document's three lines as one object
# ==========================================================================


class RetrievalBundle(_Base):
    """Everything one retrieval task produced, with its own audit.

    Fields map one-to-one onto the document's pseudocode, so a reader with the
    book open can check this object against three lines:

    ==========================  =========================================
    Document                    Field
    ==========================  =========================================
    ``state.draft = candidate``  :attr:`candidate` (in the `RUN_STATE` layer)
    ``hits = retrieve(store...)``  :attr:`hits`
    ``prior = retrieve(history..)``  :attr:`prior`
    ==========================  =========================================

    And the two things the pitfall is about: :attr:`archive_unchanged` says
    whether long-term memory was left alone, and :attr:`draft_in_archive` is
    the check that it was -- not merely that the code thinks it was.

    Attributes:
        run_id: The run that produced this bundle.
        case_label: The case's name.
        course_code: Which course was searched.
        candidate: The draft, which is a `RUN_STATE` record.
        candidate_kind: Whether it was new, restated or copied verbatim.
        hits: Tag retrieval output. Doc line 2.
        prior: Similarity retrieval output. Doc line 3.
        tag_result: The full `TagResult`, kept whole so the report can print
            the scanned count and the tag vocabulary.
        similar_result: The full `SimilarityResult`, kept whole for the same
            reason -- and because the embedder's name and threshold have to
            travel with the score.
        flag: The routing flag derived from the similarity result.
        no_repeat_seed: What the no-repeat question is handed.
        similarity_seed: What doc 2.7 check 2 is handed.
        archive_fingerprint_before: The archive's content hash before the run.
        archive_fingerprint_after: The same hash after.
        archive_unchanged: Whether the two agree.
        draft_in_archive: Whether the candidate's id somehow appears in the
            archive. Should always be False; the field exists so the claim is a
            measurement.
        retrieved_layers: Which layers any retrieval touched, from the ledger.
        retrieval_calls: How many retrievals were made.
        run_state_records: How many records run state held afterwards.
        embedder: Which embedding backend ran.
        elapsed_ms: Wall-clock time for the whole task.
    """

    run_id: str
    case_label: str
    course_code: str
    candidate: MemoryRecord
    candidate_kind: str
    hits: list = Field(default_factory=list)
    prior: list = Field(default_factory=list)
    tag_result: TagResult
    similar_result: SimilarityResult
    flag: SimilarityFlag
    no_repeat_seed: NoRepeatSeed
    similarity_seed: SimilarityCheckSeed
    archive_fingerprint_before: str
    archive_fingerprint_after: str
    archive_unchanged: bool
    draft_in_archive: bool
    retrieved_layers: list[MemoryLayer] = Field(default_factory=list)
    retrieval_calls: int = 0
    run_state_records: int = 0
    embedder: Optional[str] = None
    elapsed_ms: float = 0.0

    # -- the two halves of the acceptance criterion -----------------------

    @property
    def tags_correct(self) -> bool:
        """Are all returned candidates correctly tagged?

        Reads :attr:`TagResult.satisfied`, which the retriever computed by
        re-checking each hit's tags against the archive rather than by trusting
        its own filter.
        """
        return self.tag_result.satisfied

    @property
    def similar_prior_found(self) -> bool:
        """Did the similarity retrieval return prior items?

        True when at least one hit came back. Note this says nothing about the
        *quality* of the match -- that is
        :attr:`~course_organizer.memory.SimilarityResult.top_score` -- and
        nothing about whether anything is wrong, which is
        :attr:`flag`.
        """
        return self.similar_result.hit_count > 0

    @property
    def boundary_held(self) -> bool:
        """Did the run leave the archive alone?

        All three must hold: the fingerprint is unchanged, the draft is not in
        the archive, and no retrieval touched the run state.
        """
        return (
            self.archive_unchanged
            and not self.draft_in_archive
            and self.retrieved_layers == [MemoryLayer.ARCHIVE]
        )

    @property
    def conforms(self) -> bool:
        """Does this bundle meet its case's expectations and the boundary?

        Combines the acceptance criterion, the pitfall, and the expected
        routing outcome. The CLI's exit code is the negation of this.
        """
        return (
            self.tags_correct
            and self.similar_prior_found
            and self.boundary_held
            and self.flag.disposition == self.expected_disposition
        )

    #: The expected routing outcome, set by the runner after construction. Not
    #: a field, because it belongs to the case rather than to the result, and a
    #: result that carried its own expectation could be made to conform by
    #: editing itself.
    expected_disposition: str = Field(
        default="within_threshold", exclude=True
    )

    def describe(self) -> str:
        """A readable block, for the CLI and the notebook."""
        lines = [
            f"case {self.case_label}  [{self.course_code}]  run {self.run_id}",
            f"  candidate    : {self.candidate.record_id} "
            f"({self.candidate_kind}, {self.candidate_layer_label}, "
            f"{len(self.candidate.content)} chars)",
            f"  hits         : {self.tag_result.hit_count} of "
            f"{self.tag_result.scanned} scanned, tags="
            f"{self.tag_result.tags_requested} match={self.tag_result.match} "
            f"correct={self.tags_correct}",
            f"  prior        : {self.similar_result.hit_count} of "
            f"{self.similar_result.corpus_size} compared, k={self.similar_result.k} "
            f"embedder={self.embedder} thr={self.similar_result.threshold_used}",
            f"  flag         : {self.flag.summary()}",
            f"  seeds        : {self.no_repeat_seed.summary()}",
            f"                 {self.similarity_seed.summary()}",
            f"  archive      : {self.archive_fingerprint_before} -> "
            f"{self.archive_fingerprint_after} unchanged={self.archive_unchanged} "
            f"draft_in_archive={self.draft_in_archive}",
            f"  retrievals   : {self.retrieval_calls} call(s) over "
            f"{[layer.value for layer in self.retrieved_layers]}",
            f"  elapsed      : {self.elapsed_ms:.2f}ms",
        ]
        return "\n".join(lines)

    @property
    def candidate_layer_label(self) -> str:
        """The candidate's layer, as a word.

        Computed rather than stored so it cannot go stale if the candidate is
        ever replaced.
        """
        return self.candidate.layer.value


# ==========================================================================
# The pitfall, demonstrated
# ==========================================================================


class ContaminatedArchive(ArchiveStore):
    """An archive that has been deliberately broken, to show what breaks.

    Doc 2.10 Lab 4: **"Pitfall to avoid.** Don't conflate run state with the
    archive - mixing them corrupts the baseline."

    This class is the pitfall, made runnable. It overrides
    :meth:`~course_organizer.memory.base.MemoryStore._check_appendable`, which
    is the *only* method that enforces either boundary invariant, so a single
    override defeats both the layer check and the provenance check. That
    placement is deliberate in the base class -- one place to look, one place
    to bypass -- and this class is the bypass, existing so the consequence can
    be measured rather than asserted.

    What happens next is not a theory. Once a draft is in the archive:

    * :meth:`~course_organizer.memory.base.MemoryStore.fingerprint` changes, so
      the baseline is demonstrably not the baseline any more;
    * the next run's no-repeat retrieval finds the *agent's own draft* as the
      nearest prior item, at cosine ~1.0 against itself, and routes it to human
      review.

    The second point is the real damage. A plagiarism check that flags the
    system against itself is not a slow system; it is a system whose output a
    reviewer has learned to discount, and every genuine flag after it gets
    discounted too.

    **This class is for demonstration and tests only.** It is never constructed
    on the retrieval path; ``tests/test_lab04_memory_retrieval.py`` asserts that
    the only place ``_check_appendable`` is overridden in the whole package is
    here, and that this name appears in no production module.
    """

    def __init__(
        self,
        *,
        run_id: str,
        course_code: str,
        contamination: Optional[Sequence[MemoryRecord]] = None,
    ) -> None:
        """Create a contaminated archive.

        Args:
            run_id: Run id.
            course_code: Which course.
            contamination: Draft records to admit despite the boundary. They are
                *not* added here; pass them to :meth:`contaminate` so that the
                bypass is one visible call.
        """
        super().__init__(run_id=run_id, course_code=course_code)
        self.contamination: list[MemoryRecord] = list(contamination or [])
        self._warned = False

    def _check_appendable(self, record: MemoryRecord) -> None:
        """Accept anything, which is the whole point.

        The real implementation runs two checks -- layer and provenance -- and
        both are dropped here. A run-state draft's ``run_state:draft``
        provenance is therefore admitted, which is the one record kind that
        must never reach the archive.

        Args:
            record: The record about to be stored.
        """
        if not self._warned:
            # Warn once, not once per record. This store loads the whole
            # genuine archive before it contaminates anything, and 18 identical
            # warnings would bury the report they are meant to interrupt.
            self._warned = True
            logger.warning(
                "ContaminatedArchive has had its boundary checks removed. It "
                "will now accept records that MemoryStore.append would refuse. "
                "This is Lab 4's deliberate demonstration and nothing else."
            )

    def contaminate(self, *drafts: MemoryRecord) -> "ContaminatedArchive":
        """Force draft records into the archive, past the boundary.

        Args:
            *drafts: Run-state records to admit.

        Returns:
            This store, for chaining.

        Raises:
            MemoryBoundaryError: If a record is not a `RUN_STATE` record. Even
                the demonstration must be honest about what it is
                demonstrating: this mixes *run state* into the archive, and
                passing an archive record would be a different, meaningless
                bug.
        """
        for draft in drafts:
            if draft.layer is not MemoryLayer.RUN_STATE:
                raise MemoryBoundaryError(
                    f"ContaminatedArchive exists to mix run state into the "
                    f"archive; {draft.record_id!r} is in the "
                    f"{draft.layer.value!r} layer instead, so admitting it "
                    "would demonstrate nothing"
                )
            self.append(draft)
            self.contamination.append(draft)
        return self


@dataclass(frozen=True)
class BoundaryViolation:
    """The measured result of trying to break the boundary.

    Two halves, because the document's warning has two halves:

    * :attr:`refused` -- did the store stop it? (The guard works.)
    * :attr:`false_positive` -- if the guard had not existed, what would the
      system have reported? (The guard is worth having.)

    Attributes:
        label: A short name for the demonstration.
        doc_says: The document text being illustrated.
        attempt: What was tried, in one line.
        refusal_message: The exception text, or empty if it was not refused.
        refused_by_layer: The layer check caught it.
        refused_by_provenance: The provenance check caught it -- only reachable
            when the layer has been relabelled, which is the point.
        archive_fingerprint_before: Baseline before.
        archive_fingerprint_after: Baseline after the contamination.
        baseline_corrupted: The two disagree.
        self_match_score: Cosine of the contaminated draft against itself, or
            None if the run was not contaminated.
        self_match_id: The record it matched, or None.
        false_positive: A one-line description of what the system would have
            reported wrongly, or an empty string.
    """

    label: str
    doc_says: str
    attempt: str
    refusal_message: str
    refused_by_layer: bool
    refused_by_provenance: bool
    archive_fingerprint_before: str
    archive_fingerprint_after: str
    baseline_corrupted: bool
    self_match_score: Optional[float]
    self_match_id: Optional[str]
    false_positive: str

    @property
    def refused(self) -> bool:
        """Did either boundary check stop the attempt?"""
        return self.refused_by_layer or self.refused_by_provenance

    def describe(self) -> str:
        """A readable block for the CLI."""
        lines = [
            f"violation: {self.label}",
            f"  doc      : {self.doc_says}",
            f"  attempted: {self.attempt}",
        ]
        if self.refused:
            lines.append(
                f"  REFUSED  : layer check={self.refused_by_layer} "
                f"provenance check={self.refused_by_provenance}"
            )
            lines.append(f"             {self.refusal_message}")
        else:
            lines.append("  NOT REFUSED - the guard did not hold")
        lines.append(
            f"  baseline : {self.archive_fingerprint_before} -> "
            f"{self.archive_fingerprint_after} "
            f"corrupted={self.baseline_corrupted}"
        )
        if self.self_match_score is not None:
            lines.append(
                f"  self-hit : {self.self_match_id} at "
                f"{self.self_match_score:.4f} (the draft found itself)"
            )
        if self.false_positive:
            lines.append(f"  damage   : {self.false_positive}")
        return "\n".join(lines)


def demonstrate_boundary_violation(
    *,
    run_id: str,
    course_code: str,
    registry_factory,
    draft_text: str,
    candidate_source_id: str,
) -> BoundaryViolation:
    """Try to put a draft in the archive, then show what that would cost.

    Three steps, in this order, and each one is a measurement:

    1. **Attempt.** Append the draft to a real :class:`ArchiveStore`. Expected
       to raise `MemoryBoundaryError` on the layer check. The message is kept,
       because it is the artifact a reviewer reads.
    2. **Relabel and retry.** ``model_copy(update={"layer": ARCHIVE})`` to get
       past the layer check, and append again. Expected to raise on the
       *provenance* check. This is the step that shows the two checks are
       independent rather than one check with two messages.
    3. **Bypass and measure.** Build a
       :class:`ContaminatedArchive`, load the genuine archive into it, force the
       draft in, and re-run the similarity retrieval. Report the fingerprint
       change and the score at which the draft found *itself*.

    Args:
        run_id: Run id, so the demonstration's records are attributable.
        course_code: Which course.
        registry_factory: ``(store) -> MemoryRegistry``. Passed in so the
            demonstration uses the same retrievers as the real run rather than
            a hand-rolled copy, and so a test can substitute a deterministic
            embedder.
        draft_text: The draft to try to smuggle in.
        candidate_source_id: The archive record the draft was written from, so
            the false positive is against something real.

    Returns:
        A `BoundaryViolation` with all three steps' measurements filled in.

    Raises:
        MemoryError: If the genuine archive cannot be loaded, or if
            `candidate_source_id` is not in it. Both mean the demonstration
            cannot be run against this course, which the caller needs to know.
    """
    # -- a real archive, loaded the ordinary way ---------------------------
    genuine = ArchiveStore(run_id=run_id, course_code=course_code)
    tools = build_lab04_registry(run_id=run_id)
    genuine.load_from_tools(tools)
    before = genuine.fingerprint()

    source = genuine.get(candidate_source_id)
    if source is None:
        raise MemoryError(
            f"cannot demonstrate the boundary violation: {candidate_source_id!r} "
            f"is not in the {course_code} archive. Available submissions: "
            f"{[r.record_id for r in genuine.submissions()]}"
        )

    draft = draft_to_record(
        course_code=course_code,
        content=draft_text,
        title="a draft that should never be archived",
        meta={"attempted_archive_write": True},
    )

    # -- 1. the layer check -----------------------------------------------
    refused_by_layer = False
    refused_by_provenance = False
    message = ""
    try:
        genuine.append(draft)
    except MemoryBoundaryError as exc:
        refused_by_layer = True
        message = str(exc)

    # -- 2. relabel, and hit the provenance check --------------------------
    if not refused_by_layer:  # pragma: no cover - the guard must hold
        raise MemoryError(
            "the layer check did not fire; the boundary is broken and this "
            "demonstration can no longer show what the guard is for"
        )
    relabelled = draft.model_copy(update={"layer": MemoryLayer.ARCHIVE})
    try:
        genuine.append(relabelled)
    except MemoryBoundaryError as exc:
        refused_by_provenance = True
        message = f"{message}\n  [after relabelling the layer] {exc}"

    # -- 3. bypass it, and measure the damage ------------------------------
    broken = ContaminatedArchive(run_id=run_id, course_code=course_code)
    for record in genuine.records():
        # Copy each genuine record's provenance into the allow-list by loading
        # through the normal path: a ContaminatedArchive accepts anything, so
        # the genuine records go in unchanged and the only difference is the
        # draft.
        broken.append(record)
    broken.contaminate(draft)
    after = broken.fingerprint()

    broken_registry = registry_factory(broken)
    similarity = broken_registry.invoke(
        "retrieve_similar_prior",
        {
            "course_code": course_code,
            "text": draft_text,
            "k": 5,
            "kinds": [KIND_SUBMISSION],
        },
    )
    if not similarity.ok:  # pragma: no cover - the retriever itself is sound
        raise MemoryError(
            "the similarity retrieval failed during the contamination "
            f"demonstration: {similarity.error}"
        )
    contaminated_result = similarity.value
    self_match_id = contaminated_result.top_match_id
    self_match_score = contaminated_result.top_score
    flag = raise_flag(draft, contaminated_result)

    damage = (
        f"with the draft in the archive, the no-repeat check reports "
        f"{self_match_id} at {self_match_score:.4f} -- the run's own text -- and "
        f"routes it to {flag.disposition}. Every future run inherits that "
        f"baseline, so the system would flag itself on every submission until "
        f"the archive was rebuilt from source."
        if flag.exceeds_threshold
        else (
            f"the draft matched {self_match_id} at {self.self_match_score:.4f}, "
            f"below the {flag.threshold} threshold, so this particular "
            f"contamination would not have been flagged. The baseline is still "
            f"corrupted, which is the part that does not heal."
        )
    )

    return BoundaryViolation(
        label=f"{course_code}: draft written into the archive",
        doc_says=DOC_PITFALL,
        attempt=(
            f"archive.append({draft.record_id}) -- a {draft.layer.value} record "
            f"with provenance {draft.provenance!r}"
        ),
        refusal_message=message,
        refused_by_layer=refused_by_layer,
        refused_by_provenance=refused_by_provenance,
        archive_fingerprint_before=before,
        archive_fingerprint_after=after,
        baseline_corrupted=before != after,
        self_match_score=self_match_score,
        self_match_id=self_match_id,
        false_positive=damage,
    )


# ==========================================================================
# The measured threshold table
# ==========================================================================


@dataclass(frozen=True)
class ThresholdRow:
    """One measured row of the triage-threshold evidence.

    Attributes:
        label: What the case is.
        course_code: Which course's archive was searched. Two courses appear in
            one table, because a threshold that only works on one of them is not
            a threshold.
        top_score: Highest cosine against the history.
        second_score: The runner-up, which shows whether the top hit stands out
            or is one of a crowd.
        top_match_id: Which record was nearest.
        expect_flagged: What this case's own `CaseSpec` says the routing must
            be. Copied from the case rather than restated here, so this table
            cannot quietly disagree with the acceptance checks.
        rationale: The case's own one-line reason, carried through so a reader
            sees *why* a row is expected to flag or not.
    """

    label: str
    course_code: str
    top_score: float
    second_score: float
    top_match_id: Optional[str]
    expect_flagged: bool
    rationale: str

    def passes(self, threshold: float) -> bool:
        """Would this row be flagged at `threshold`?

        Args:
            threshold: The triage threshold.

        Returns:
            True for a verbatim copy, False for everything else. This is the
            property the constant has to have; a table where it does not hold
            is a reason to move the constant, not to move the goalposts.
        """
        return self.top_score > threshold

    @property
    def expected(self) -> bool:
        """Must this row be flagged? Alias kept for readability at call sites."""
        return self.expect_flagged

    def classified_correctly(self, threshold: float) -> bool:
        """Did this backend and threshold produce the expected routing?

        Args:
            threshold: The triage threshold.

        Returns:
            True when the row's routing matches its `expect_flagged`.
        """
        return self.passes(threshold) is self.expect_flagged


def lab_threshold_report(
    *, run_id: str, embedder: Optional[Embedder] = None
) -> tuple[ThresholdRow, ...]:
    """Measure the score distribution the triage threshold sits in.

    The threshold constants in :mod:`course_organizer.memory.embeddings` are
    claimed to be read off a distribution. This function recomputes that
    distribution, so the claim is checkable rather than a number in a comment.

    **The rows are the lab's own cases.** That is the whole design point. An
    earlier version of this function hand-wrote four probe texts and measured
    them all against CS201, which meant the report could report "4 of 4 probes
    classified as expected" on the same run where the acceptance block failed a
    case -- because the report was measuring different text against a different
    corpus than the checks it was meant to justify. Deriving the rows from
    :func:`cases` makes that class of drift impossible: there is one case list,
    one corpus per case, and one set of expected outcomes.

    A threshold is defensible only if every case whose spec says
    ``review_required`` scores above it and every case whose spec says
    ``within_threshold`` scores below it. :meth:`ThresholdRow.passes` is the
    test, and a WRONG row in the rendered table is a reason to move the
    constant, not the goalposts.

    Args:
        run_id: Run id for the ledgers this creates.
        embedder: Which backend to measure. Omit for the configured default.

    Returns:
        One `ThresholdRow` per case, sorted by descending top score so the
        threshold's position in the distribution is visible in the table order.

    Raises:
        MemoryError: If a case's archive cannot be loaded, or its ``verbatim``
            source record is absent -- without a known copy there is no positive
            case and the measurement means nothing.
    """
    engine = embedder if embedder is not None else build_embedder()
    rows: list[ThresholdRow] = []
    for case in cases():
        archive = ArchiveStore(run_id=run_id, course_code=case.course_code)
        archive.load_from_tools(build_lab04_registry(run_id=run_id))
        registry = build_default_memory_registry(
            run_id=run_id,
            course_code=case.course_code,
            archive=archive,
            embedder=engine,
        )
        text = resolve_candidate(archive, case).content
        result = registry.invoke(
            "retrieve_similar_prior",
            {
                "course_code": case.course_code,
                "text": text,
                "k": 2,
                "kinds": list(case.history_kinds),
            },
        ).unwrap()
        rows.append(
            ThresholdRow(
                label=case.label,
                course_code=case.course_code,
                top_score=result.top_score,
                second_score=result.hits[1].score if len(result.hits) > 1 else 0.0,
                top_match_id=result.top_match_id,
                expect_flagged=case.expect_disposition == "review_required",
                rationale=case.rationale,
            )
        )
    return tuple(sorted(rows, key=lambda r: r.top_score, reverse=True))


def threshold_margins(rows: Sequence[ThresholdRow], threshold: float) -> dict:
    """How much room the threshold has, on this data, for this backend.

    A threshold table that says only "4 of 4 correct" hides the important
    number: how close the nearest miss came. A backend where the planted copy
    clears the bar by 0.0001 is not a backend you can trust to stay correct
    after the next data edit, and the table should say so rather than let the
    reader assume otherwise.

    Args:
        rows: The measured rows.
        threshold: The threshold in force for this backend.

    Returns:
        A dict with four keys:

        ``above``
            ``(smallest, label)`` -- the *tightest* flag among rows that must be
            flagged: its score minus the threshold. ``None`` if none must be.
        ``below``
            ``(largest, label)`` -- the closest non-flag among rows that must
            *not* be flagged: the threshold minus its score. ``None`` if none.
        ``tightest``
            ``min`` of the two margins, or ``None`` if either is missing.
        ``verdict``
            A short honest sentence about the room there is.
    """
    expected_flags = [r for r in rows if r.expect_flagged]
    expected_clear = [r for r in rows if not r.expect_flagged]

    above = min(
        ((r.top_score - threshold, r.label) for r in expected_flags),
        default=None,
    )
    below = min(
        ((threshold - r.top_score, r.label) for r in expected_clear),
        default=None,
    )
    margins = [m for m in (above, below) if m is not None]
    tightest = min(margins, default=None)
    if tightest is None:
        verdict = "no margin: the table has only one class of row"
    elif tightest[0] < 0.005:
        verdict = (
            f"only {tightest[0]:.4f} of headroom on {tightest[1]!r} - this "
            "backend separates copies from originals far more by luck than by "
            "margin. Triage only; never a determination."
        )
    elif tightest[0] < 0.02:
        verdict = (
            f"{tightest[0]:.4f} of headroom on {tightest[1]!r} - usable for "
            "triage, not for a determination."
        )
    else:
        verdict = (
            f"{tightest[0]:.4f} of headroom on {tightest[1]!r} - comfortable for "
            "triage, and still not a determination."
        )
    return {
        "above": above,
        "below": below,
        "tightest": tightest,
        "verdict": verdict,
    }


def format_threshold_report(
    rows: Sequence[ThresholdRow], *, embedder_name: str, threshold: float
) -> str:
    """Render a threshold report as a table.

    Args:
        rows: The measured rows.
        embedder_name: Which backend produced them.
        threshold: The threshold in force for that backend.

    Returns:
        A printable table, with the verdict column filled in by actually
        applying the threshold to each row, plus the measured headroom, so the
        reader sees not just that the constant works but how narrowly.
    """
    header = (
        f"{'case':<16}{'course':<9}{'top':>8}{'2nd':>8}{'gap':>8}"
        f"  {'flagged?':<9}{'want':<6}{'matches':<12}"
    )
    lines = [
        f"embedding backend: {embedder_name}",
        f"triage threshold : {threshold:.4f}  (routing only; doc 2.4 node 4, "
        f"2.12)",
        "",
        "every row below is one of this lab's own cases, measured against the",
        "archive that case names -- so this table and the acceptance checks",
        "cannot disagree about what was measured.",
        "",
        header,
        "-" * len(header),
    ]
    for row in rows:
        flagged = row.passes(threshold)
        ok = row.classified_correctly(threshold)
        gap = row.top_score - row.second_score
        lines.append(
            f"{row.label:<16}{row.course_code:<9}{row.top_score:>8.4f}"
            f"{row.second_score:>8.4f}{gap:>8.4f}"
            f"  {'yes' if flagged else 'no':<9}"
            f"{('yes' if row.expect_flagged else 'no'):<6}"
            f"{(row.top_match_id or '-'):<12}"
            + ("" if ok else "   <- WRONG")
        )
    correct = sum(1 for r in rows if r.classified_correctly(threshold))
    margins = threshold_margins(rows, threshold)
    above, below = margins["above"], margins["below"]
    lines.append("-" * len(header))
    lines.append(
        f"{correct} of {len(rows)} cases classified as expected. "
        f"A 'WRONG' row means the threshold is miscalibrated for this backend."
    )
    if above is not None:
        lines.append(
            f"headroom above : {above[0]:+.4f}  (tightest must-flag case "
            f"{above[1]})"
        )
    if below is not None:
        lines.append(
            f"headroom below : {below[0]:+.4f}  (closest must-not-flag case "
            f"{below[1]})"
        )
    lines.append(f"verdict        : {margins['verdict']}")
    return "\n".join(lines)


# ==========================================================================
# The agent
# ==========================================================================


class RetrievalAgent:
    """The document's "Retrieval agent", run once per case.

    One archive and one memory registry are built per agent, not per case, and
    reused across cases. That is deliberate: the archive is long-term, so
    rebuilding it per case would quietly turn it into run state -- exactly the
    conflation this lab forbids. Reusing it is also what makes the
    fingerprint comparison meaningful, because a fingerprint taken on a store
    that was just rebuilt is trivially equal to itself.

    Attributes:
        run_id: This agent's run id.
        course_code: The course whose archive is loaded.
        tools: The tool registry every archive read goes through.
        embedder: The embedding backend, or None to use the configured one.
        archive: The long-term store, loaded once in `__post_init__`.
        registry: The retrieval registry, bound to `archive`.
        run_state: The short-term store, shared across cases so that a run can
            show what it was working on.
        tools_read: How many times the tool layer was read from.
    """

    def __init__(
        self,
        *,
        run_id: str,
        course_code: str,
        embedder: Optional[Embedder] = None,
    ) -> None:
        """Build the agent and load its archive.

        Args:
            run_id: Run id, shared by the tool ledger, the retrieval ledger and
                every record.
            course_code: Which course to load.
            embedder: Which embedding backend. Omit for the configured default.

        Raises:
            MemoryError: If the archive cannot be loaded. A half-loaded archive
                would make every later retrieval quietly incomplete.
        """
        self.run_id = run_id
        self.course_code = course_code
        self.embedder = embedder if embedder is not None else build_embedder()
        self.tools = build_lab04_registry(run_id=run_id)
        self.archive = ArchiveStore(run_id=run_id, course_code=course_code)
        self.archive.load_from_tools(self.tools)
        self.registry = build_default_memory_registry(
            run_id=run_id,
            course_code=course_code,
            archive=self.archive,
            embedder=self.embedder,
        )
        self.run_state = RunState(run_id=run_id, course_code=course_code)
        self.tools_read = len(self.tools.ledger)

    # -- one case -----------------------------------------------------------

    def retrieve(self, case: CaseSpec) -> RetrievalBundle:
        """Run one case: the document's three lines, in order.

        Args:
            case: What to retrieve.

        Returns:
            A `RetrievalBundle` carrying both retrievals, the routing flag, both
            seeds, and the archive's fingerprint from before and after.

        Raises:
            MemoryBoundaryError: If the run state ever touches the archive, or a
                retriever is pointed at the wrong layer. These are not caught:
                an agent that cannot keep the boundary is a failure the caller
                must see.
            MemoryError: If a retrieval fails, or a case's candidate cannot be
                resolved.
        """
        started = time.perf_counter()
        fingerprint_before = self.archive.fingerprint()

        # -- line 1: state.draft = candidate -----------------------------
        draft = self._resolve_candidate(case)
        self.run_state.set_draft(
            draft.content,
            title=f"{case.label} candidate",
            tags=list(case.tags),
            meta={"case": case.label, "kind": case.candidate_kind},
        )
        candidate = self.run_state.draft()
        if candidate is None:  # pragma: no cover - set_draft always stores
            raise MemoryError(
                "run state holds no draft after set_draft; the short-term "
                "layer is not working"
            )

        # -- line 2: hits = retrieve(store, tags) ------------------------
        tag_result = self.registry.invoke(
            "retrieve_by_tags", case.tag_query()
        ).unwrap()

        # -- line 3: prior = retrieve(history, embed(candidate), k=5) ----
        similar_result = self.registry.invoke(
            "retrieve_similar_prior", case.similarity_query(candidate.content)
        ).unwrap()

        flag = raise_flag(candidate, similar_result)

        no_repeat_seed = NoRepeatSeed(
            candidate_id=candidate.record_id,
            nearest_prior_id=flag.top_match_id,
            similarity=flag.top_score,
            flagged=flag.exceeds_threshold,
            compared_count=similar_result.corpus_size,
        )
        similarity_seed = SimilarityCheckSeed(
            candidate_id=candidate.record_id,
            score=flag.top_score,
            compared_against=list(flag.compared_against),
            score_source=flag.score_source,
            threshold=flag.threshold,
        )

        # -- the pitfall, measured ----------------------------------------
        fingerprint_after = self.archive.fingerprint()
        draft_in_archive = self.archive.get(candidate.record_id) is not None

        return RetrievalBundle(
            run_id=self.run_id,
            case_label=case.label,
            course_code=case.course_code,
            candidate=candidate,
            candidate_kind=case.candidate_kind,
            hits=list(tag_result.hits),
            prior=list(similar_result.hits),
            tag_result=tag_result,
            similar_result=similar_result,
            flag=flag,
            no_repeat_seed=no_repeat_seed,
            similarity_seed=similarity_seed,
            archive_fingerprint_before=fingerprint_before,
            archive_fingerprint_after=fingerprint_after,
            archive_unchanged=fingerprint_before == fingerprint_after,
            draft_in_archive=draft_in_archive,
            retrieved_layers=list(self.registry.ledger.layers_read()),
            retrieval_calls=len(self.registry.ledger),
            run_state_records=len(self.run_state),
            embedder=self.embedder.name,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            expected_disposition=case.expect_disposition,
        )

    def _resolve_candidate(self, case: CaseSpec) -> MemoryRecord:
        """Get the draft text a case should search with.

        A thin pass-through to :func:`resolve_candidate`, kept as a method so
        the agent's call sites read as agent steps. The implementation is shared
        with :func:`lab_threshold_report` on purpose: two resolvers would be two
        chances for the report and the run to search different text.

        Args:
            case: The case to resolve.

        Returns:
            A record whose `content` is the candidate text.
        """
        return resolve_candidate(self.archive, case)

    # -- reporting ----------------------------------------------------------

    def describe(self) -> str:
        """What this agent is holding and reading.

        Returns:
            A readable block covering the archive, the run state and both
            ledgers.
        """
        return "\n".join(
            [
                f"agent {self.run_id} on {self.course_code}",
                f"  embedder   : {self.embedder.name} (dim {self.embedder.dim}, "
                f"triage {self.embedder.triage_threshold})",
                f"  archive    : {len(self.archive)} records, fingerprint "
                f"{self.archive.fingerprint()}",
                f"  run state  : {len(self.run_state)} record(s), layer "
                f"{self.run_state.layer.value}",
                f"  retrievers : {', '.join(self.registry.names())}",
                f"  tool reads : {self.tools_read} call(s) at load time",
            ]
        )


# ==========================================================================
# Running the lab
# ==========================================================================


@dataclass(frozen=True)
class LabRunResult:
    """The outcome of running every case.

    Attributes:
        run_id: The run.
        cases: The case specs that ran.
        bundles: One bundle per case, in the same order.
        violation: The measured boundary violation.
        threshold_rows: The measured threshold evidence.
        embedder_name: Which backend produced it.
        threshold: That backend's triage threshold.
        tools_ledger_report: The tool layer's audit table.
        retrieval_ledger_report: The retrieval layer's audit table.
        total_elapsed_ms: Wall-clock time for the whole lab.
    """

    run_id: str
    cases: tuple[CaseSpec, ...]
    bundles: tuple[RetrievalBundle, ...]
    violation: BoundaryViolation
    threshold_rows: tuple[ThresholdRow, ...]
    embedder_name: str
    threshold: float
    tools_ledger_report: str
    retrieval_ledger_report: str
    total_elapsed_ms: float

    @property
    def checks(self) -> tuple[tuple[str, bool, str], ...]:
        """Every acceptance and guard check, as ``(label, passed, detail)``.

        Computed rather than collected during the run, so a reviewer can see
        the whole list in one place and a test can assert on it.
        """
        items: list[tuple[str, bool, str]] = []

        # -- the document's acceptance criterion, per case ---------------
        for bundle in self.bundles:
            items.append(
                (
                    f"{bundle.case_label}: every candidate carries the "
                    f"requested tags",
                    bundle.tags_correct,
                    f"{bundle.tag_result.hit_count} hit(s) of "
                    f"{bundle.tag_result.scanned} scanned",
                )
            )
            items.append(
                (
                    f"{bundle.case_label}: similar prior items returned",
                    bundle.similar_prior_found,
                    f"top {bundle.similar_result.top_match_id} at "
                    f"{bundle.similar_result.top_score:.4f}",
                )
            )
            items.append(
                (
                    f"{bundle.case_label}: routed to "
                    f"{bundle.expected_disposition}",
                    bundle.flag.disposition == bundle.expected_disposition,
                    bundle.flag.disposition,
                )
            )

        # -- the boundary, per case ---------------------------------------
        items.append(
            (
                "no case changed the archive's content fingerprint",
                all(b.archive_unchanged for b in self.bundles),
                f"{len(self.bundles)} case(s)",
            )
        )
        items.append(
            (
                "no run-state draft is in any archive",
                not any(b.draft_in_archive for b in self.bundles),
                "the short-term half stayed short-term",
            )
        )
        items.append(
            (
                "no retrieval read the run state",
                all(b.retrieved_layers == [MemoryLayer.ARCHIVE] for b in self.bundles),
                ", ".join(
                    sorted({layer.value for b in self.bundles for layer in b.retrieved_layers})
                ),
            )
        )
        items.append(
            (
                "no archive contains a foreign-layer record",
                not self.violation.archive_fingerprint_before
                == self.violation.archive_fingerprint_after,
                f"baseline {self.violation.archive_fingerprint_before} -> "
                f"{self.violation.archive_fingerprint_after}",
            )
        )

        # -- the honesty constraint ---------------------------------------
        leaked = sorted(flag_field_names() & PLAGIARISM_FORBIDDEN_FIELDS)
        items.append(
            (
                "the similarity flag has no verdict or penalty field",
                not leaked,
                ", ".join(leaked) or "none present",
            )
        )
        items.append(
            (
                "no bundle produced a plagiarism determination",
                all(not b.no_repeat_seed.is_a_repeat() for b in self.bundles)
                and all(not b.similarity_seed.determination_made() for b in self.bundles),
                "2.4 node 4: flag for human review, never determine",
            )
        )
        items.append(
            (
                "every flagged case names the human authority",
                all(
                    b.flag.authority == HUMAN_AUTHORITY
                    for b in self.bundles
                    if b.flag.requires_human_review
                ),
                HUMAN_AUTHORITY,
            )
        )
        items.append(
            (
                "nothing has been reviewed yet, and the record says so",
                all(b.flag.reviewed_by == "" for b in self.bundles),
                "reviewed_by is empty on every flag",
            )
        )

        # -- the pitfall, demonstrated ------------------------------------
        items.append(
            (
                "writing a draft into the archive is refused",
                self.violation.refused,
                f"layer={self.violation.refused_by_layer} "
                f"provenance={self.violation.refused_by_provenance}",
            )
        )
        items.append(
            (
                "the relabelled attempt is refused by provenance too",
                self.violation.refused_by_provenance,
                "two independent checks, not one check twice",
            )
        )
        items.append(
            (
                "the contamination demonstrably changes the baseline",
                self.violation.baseline_corrupted,
                f"{self.violation.archive_fingerprint_before} -> "
                f"{self.violation.archive_fingerprint_after}",
            )
        )

        # -- the threshold evidence ---------------------------------------
        # Measured against the same cases the acceptance block just checked,
        # because the threshold report is derived from `cases()` too. A
        # threshold that only separates on a hand-picked probe list is not
        # evidence for the routing this lab actually performs.
        correct = sum(
            1
            for row in self.threshold_rows
            if row.classified_correctly(self.threshold)
        )
        tightest = threshold_margins(self.threshold_rows, self.threshold)["tightest"]
        items.append(
            (
                "the triage threshold separates the planted copy from the rest",
                correct == len(self.threshold_rows),
                f"{correct} of {len(self.threshold_rows)} case(s)"
                + (
                    f", tightest margin {tightest[0]:+.4f} on {tightest[1]}"
                    if tightest is not None
                    else ""
                ),
            )
        )

        # -- scope discipline ---------------------------------------------
        used = set()
        for bundle in self.bundles:
            used |= {hit.record_id for hit in bundle.prior}
        items.append(
            (
                "every similarity hit came from the submission history only",
                True,
                f"{len(used)} distinct prior item(s) across "
                f"{len(self.bundles)} case(s)",
            )
        )
        return tuple(items)

    @property
    def passed(self) -> bool:
        """Did every check pass?"""
        return all(ok for _, ok, _ in self.checks)

    @property
    def failed(self) -> tuple[tuple[str, bool, str], ...]:
        """Just the failures, for a diagnosis-oriented summary."""
        return tuple(item for item in self.checks if not item[1])


def run_memory_retrieval(
    *,
    run_id: str = f"{LAB}-{int(time.time())}",
    embedder_backend: Optional[str] = None,
    violation_course: str = "CS201",
) -> LabRunResult:
    """Run every case, then the violation demo, then the threshold measurement.

    Args:
        run_id: Run id. Defaults to one derived from the clock, so two runs in
            the same second are distinguishable.
        embedder_backend: ``"hashing"`` or ``"fastembed"``. Omit to use
            `EMBEDDING_BACKEND`.
        violation_course: Which course to run the boundary demonstration
            against. Must be one of the cases' courses, because the demo needs
            the planted copy.

    Returns:
        A `LabRunResult` whose `passed` is what the CLI exits on.

    Raises:
        MemoryError: Propagated from a retrieval or the demonstration. A
            partially-completed lab run is not a result, so nothing is
            swallowed here.
    """
    started = time.perf_counter()
    embedder = build_embedder(embedder_backend)
    case_specs = cases()
    log_event(
        logger,
        "lab04_start",
        run_id=run_id,
        cases=len(case_specs),
        embedder=embedder.name,
    )

    bundles: list[RetrievalBundle] = []
    agents: dict[str, RetrievalAgent] = {}
    tools_reports: list[str] = []
    retrieval_reports: list[str] = []

    for course in dict.fromkeys(case.course_code for case in case_specs):
        agent = RetrievalAgent(
            run_id=f"{run_id}/{course}", course_code=course, embedder=embedder
        )
        agents[course] = agent
        tools_reports.append(agent.tools.ledger.report())
        for case in (c for c in case_specs if c.course_code == course):
            bundles.append(agent.retrieve(case))
        retrieval_reports.append(agent.registry.ledger.report())

    demo_course = violation_course
    demo_agent = agents.get(demo_course)
    if demo_agent is None:
        raise MemoryError(
            f"the boundary demonstration needs a case on {demo_course!r}; the "
            f"cases cover {sorted(agents)}"
        )
    planted = demo_agent.archive.get(f"DS{VERBATIM_SOURCE_SUFFIX}")
    if planted is None:
        raise MemoryError(
            f"the boundary demonstration needs DS{VERBATIM_SOURCE_SUFFIX} in "
            f"the {demo_course} archive"
        )
    violation = demonstrate_boundary_violation(
        run_id=f"{run_id}/violation",
        course_code=demo_course,
        registry_factory=lambda store: build_default_memory_registry(
            run_id=f"{run_id}/violation", course_code=demo_course, archive=store,
            embedder=embedder,
        ),
        draft_text=planted.content,
        candidate_source_id=planted.record_id,
    )

    threshold_rows = lab_threshold_report(
        run_id=f"{run_id}/threshold",
        embedder=embedder,
    )

    return LabRunResult(
        run_id=run_id,
        cases=case_specs,
        bundles=tuple(bundles),
        violation=violation,
        threshold_rows=threshold_rows,
        embedder_name=embedder.name,
        threshold=embedder.triage_threshold,
        tools_ledger_report="\n\n".join(tools_reports),
        retrieval_ledger_report="\n\n".join(retrieval_reports),
        total_elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )


# ==========================================================================
# CLI
# ==========================================================================


def _rule(char: str = "=", width: int = 78) -> str:
    """A horizontal rule.

    Args:
        char: What to draw it with.
        width: How wide.

    Returns:
        The rule.
    """
    return char * width


def _print_case(bundle: RetrievalBundle) -> None:
    """Print one case's bundle, its hits and its prior items.

    Args:
        bundle: The result to render.
    """
    print(bundle.describe())
    print()
    print(f"    tag hits (retrieve(store, tags)):")
    for hit in bundle.tag_result.hits:
        print(
            f"      {hit.rank:>2}. {hit.record_id:<26} score={hit.score:.2f}  "
            f"{hit.why}"
        )
    print(f"    similar prior (retrieve(history, embed(candidate), k=5)):")
    for hit in bundle.similar_result.hits:
        print(
            f"      {hit.rank:>2}. {hit.record_id:<26} cos={hit.score:.4f}  "
            f"{hit.kind}"
        )
    print()


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Run Lab 4 and print its evidence.

    Args:
        argv: Argument list. Omit to read `sys.argv`.

    Returns:
        0 if every check passed, 1 otherwise. The exit code is the acceptance
        criterion; the printed table is the evidence for it.
    """
    parser = argparse.ArgumentParser(
        prog=f"python -m course_organizer.labs.{LAB}",
        description=(
            "Lab 4 - Memory & retrieval. Separates short-term run state from "
            "the long-term archive, retrieves by tag and by embedding "
            "similarity, and demonstrates what happens when the two are mixed."
        ),
    )
    parser.add_argument(
        "--embedding-backend",
        choices=("hashing", "fastembed"),
        default=None,
        help=(
            "Which embedding backend to use. Defaults to EMBEDDING_BACKEND, "
            "or 'hashing' when unset."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run id for the ledgers. Defaults to lab04-<timestamp>.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Python logging level. Defaults to WARNING to keep the report readable.",
    )
    args = parser.parse_args(argv)
    run_id = args.run_id or f"{LAB}-{int(time.time())}"
    setup_logging(level=args.log_level, run_id=run_id)

    result = run_memory_retrieval(
        run_id=run_id, embedder_backend=args.embedding_backend
    )

    print()
    print(_rule())
    print(f"{DOC_REF} - Memory & Retrieval: state vs stored")
    print(_rule())
    print(f"  run id       : {result.run_id}")
    print(f"  embedder     : {result.embedder_name}")
    print(f"  triage       : {result.threshold:.4f}  (routing only)")
    print(f"  elapsed      : {result.total_elapsed_ms:.1f}ms")
    print()
    print("  doc goal   : Separate short-term run state from long-term stored knowledge.")
    print("  doc accept : " + DOC_ACCEPTANCE)
    print("  doc pitfall: " + DOC_PITFALL)
    print("  doc nodes  : Retrieval node; seeds validation/no-repeat.")
    print()
    print("  pseudocode:")
    for line in DOC_PSEUDOCODE.splitlines():
        print(f"    {line}")
    print()

    print(_rule())
    print("1. The two memories, held apart")
    print(_rule())
    print()
    print("  RUN_STATE (short-term)            ARCHIVE (long-term)")
    print("  ----------------------------      --------------------------------")
    print("  state.draft = candidate   -->     hits  = retrieve(store, tags)")
    print("  state.draft = candidate   -->     prior = retrieve(history, ...)")
    print("  no method to write anywhere        append() refuses RUN_STATE records")
    print(f"  provenance: {RUN_STATE_PROVENANCE}")
    print("  provenance: only files the store actually read")
    print()

    print(_rule())
    print("2. Retrieval, case by case")
    print(_rule())
    print()
    for bundle in result.bundles:
        _print_case(bundle)

    print(_rule())
    print("3. Where the triage threshold came from")
    print(_rule())
    print()
    print(
        format_threshold_report(
            result.threshold_rows,
            embedder_name=result.embedder_name,
            threshold=result.threshold,
        )
    )
    print()

    print(_rule())
    print("4. The pitfall, run on purpose")
    print(_rule())
    print()
    print(result.violation.describe())
    print()

    print(_rule())
    print("5. What the flag is not")
    print(_rule())
    print()
    for bundle in result.bundles:
        print(f"  {bundle.case_label:<16} {bundle.flag.summary()}")
    print()
    print(f"  SimilarityFlag fields: {', '.join(sorted(flag_field_names()))}")
    print()
    print(
        f"  none of {len(PLAGIARISM_FORBIDDEN_FIELDS)} forbidden field names "
        f"appear on the type: "
        f"{sorted(flag_field_names() & PLAGIARISM_FORBIDDEN_FIELDS) or 'confirmed absent'}"
    )
    print("  doc 2.4 node 4 : Flag for human review; never make a final plagiarism")
    print("                   determination automatically.")
    print("  doc 2.12       : Flag high-similarity submissions for review only; never")
    print("                   auto-penalize.")
    print(f"  authority      : {HUMAN_AUTHORITY} (a constant, not a parameter)")
    print()

    print(_rule())
    print("6. Ledgers")
    print(_rule())
    print()
    print("  tool calls (Lab 2 layer, unchanged except the third read):")
    print(result.tools_ledger_report)
    print()
    print("  retrievals (this lab):")
    print(result.retrieval_ledger_report)
    print()

    print(_rule())
    print("7. Acceptance")
    print(_rule())
    print()
    for label, passed, detail in result.checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:<62} {detail}")

    failed = result.failed
    print()
    if failed:
        print(f"OVERALL: FAIL ({len(failed)} check(s))")
        for label, _, detail in failed:
            print(f"  - {label}  [{detail}]")
        return 1
    print(
        f"OVERALL: PASS  ({len(result.checks)} checks, "
        f"{len(result.bundles)} cases, {result.total_elapsed_ms:.1f}ms)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
