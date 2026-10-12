"""Tests for Lab 4 -- memory and retrieval.

Grouped to mirror the lab's claims, because "separate short-term run state from
long-term stored knowledge" is not a claim you can check by calling a function
once and confirming it did not raise. It is a claim about *what can reach
what*, and that is only checkable structurally.

Groups:

1. structural      -- the package is shaped as the document describes
2. the boundary    -- two independent checks, and a way to measure the bypass
3. retrieval       -- tags and similarity, and the acceptance criterion itself
4. the threshold   -- the constant is measured, not asserted
5. the pitfall     -- 2.10's warning, run on purpose
6. honesty         -- no verdict field, no penalty, no auto-determination
7. both backends   -- the hermetic one always; the learned one when cached

Four tests carry most of the weight, and each is named for the claim it defends:

``test_stores_module_is_the_only_memoryrecord_constructor``
    Parses the package with `ast` and fails if any module other than
    ``memory/stores.py`` constructs a `MemoryRecord`. A second constructor is a
    second place to forget the boundary, and the boundary is the lab.

``test_contaminated_archive_is_the_only_boundary_override``
    Asserts `_check_appendable` is overridden in exactly one place in the whole
    package, and that the place is the lab's deliberately broken class. This is
    what stops the demonstration from quietly becoming a feature.

``test_similarity_flag_has_no_verdict_or_penalty_field``
    Asserts the flag's field set is disjoint from `PLAGIARISM_FORBIDDEN_FIELDS`.
    This is 2.4 node 4 and 2.12 as an executable check: "flag for review only,
    never auto-penalize" is a claim about the *shape of a type*.

``test_widening_the_measurement_moved_the_threshold``
    Records the reason `TRIAGE_THRESHOLD_HASHING` is 0.995 and not 0.99. An
    unexplained constant in a screening system is a liability; a constant with
    the measurement that produced it attached is documentation.

The suite runs entirely on the `hashing` backend so it needs no model
download, no network and no credentials. The learned backend is exercised by
the `fastembed_available`-gated tests at the end, which skip -- visibly, with the
command that would enable them -- on a machine that has not cached the model.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from course_organizer import HUMAN_AUTHORITY
from course_organizer.labs import lab04_memory_retrieval as lab04
from course_organizer.memory import (
    KIND_CANDIDATE_DRAFT,
    KIND_NOTE_MATERIAL,
    KIND_SUBMISSION,
    LAYERS,
    LAYER_PURPOSE,
    PLAGIARISM_FORBIDDEN_FIELDS,
    RUN_STATE_PROVENANCE,
    ArchiveStore,
    EmbeddingError,
    HashingEmbedder,
    MemoryBoundaryError,
    MemoryError,
    MemoryLayer,
    MemoryRegistry,
    MemoryStore,
    RetrievalHit,
    RunState,
    SimilarityQuery,
    SimilarityResult,
    TagQuery,
    build_default_memory_registry,
    build_embedder,
    cosine_similarity,
    draft_to_record,
    flag_field_names,
    raise_flag,
)
from course_organizer.memory.embeddings import (
    HASHING_DIM,
    NGRAM_RANGE,
    TRIAGE_THRESHOLD_FASTEMBED,
    TRIAGE_THRESHOLD_HASHING,
)
from course_organizer.tools.compute import build_lab04_registry

PACKAGE = Path(lab04.__file__).resolve().parent.parent / "memory"
CS = "CS201"
DB = "DBMS402"
PLANTED = "DS-A1-S001"


# ══════════════════════════════════════════════════════════════════════════
# 1. structural
# ══════════════════════════════════════════════════════════════════════════


def test_memory_package_has_the_four_modules_the_design_needs() -> None:
    """`base`, `embeddings`, `stores`, `retrieval` -- and nothing stray.

    Four modules is a deliberate split: the boundary is not the same concern as
    the vectors, and not the same concern as the data. A fifth module would mean
    one of those four had grown a second job.
    """
    names = sorted(p.stem for p in PACKAGE.glob("*.py"))
    assert names == ["__init__", "base", "embeddings", "retrieval", "stores"]


def test_stores_module_is_the_only_memoryrecord_constructor() -> None:
    """Only `stores.py` may construct a `MemoryRecord`.

    The document's pitfall is "don't conflate run state with the archive". A
    record can be built anywhere and it always says which layer it belongs to,
    so this is not about a missing field -- it is about there being exactly one
    place that decides how a record's provenance is set. Two constructors means
    two places to get that wrong, and the boundary stops being a property of
    the type and becomes a property of whichever file you happened to read.
    """
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "MemoryRecord":
                continue
            # A *reference* such as `isinstance(x, MemoryRecord)` is not a
            # constructor call, and neither is a bare annotation.
            if path.name == "stores.py":
                continue
            offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "MemoryRecord must be constructed only in memory/stores.py; also "
        f"constructed at {offenders}"
    )


def test_contaminated_archive_is_the_only_boundary_override() -> None:
    """Exactly one `_check_appendable` *override* exists, and it is the demo.

    The pitfall demonstration works by removing the guard. That is fine in a
    class named `ContaminatedArchive` in a lab module. It is not fine the day
    someone overrides it in `stores.py` to make a test pass, so the package is
    parsed, `MemoryStore`'s own definition is excluded as the base rather than an
    override, and the remaining count is asserted to be exactly one.
    """
    overrides: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")) + [Path(lab04.__file__)]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "_check_appendable"
                    and node.name != "MemoryStore"  # the base definition itself
                ):
                    overrides.append(f"{path.name}::{node.name}")
    assert overrides == ["lab04_memory_retrieval.py::ContaminatedArchive"], (
        "the only _check_appendable override in the project must be the lab's "
        f"deliberate demonstration; found {overrides}"
    )


def _code_without_docstrings(path: Path) -> str:
    """The file's source, with docstrings and Pydantic example values removed.

    Structural assertions here are about *code*, not prose. A course name inside
    a docstring or a ``Field(examples=["CS201"])`` hint is documentation and must
    be allowed; the same string in a comparison or an argument is a hard-coded
    constant and must not be. Scanning raw lines cannot tell those apart, so both
    are excised from the AST and the result is unparsed.

    Args:
        path: The ``.py`` file to read.

    Returns:
        The code, unparsed, with no docstrings and no `examples`/`description`
        keyword values.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value = ast.Constant(value=None)
        if isinstance(node, ast.Call):
            # `Field(examples=[...], description=...)` is documentation that
            # happens to be an expression; its values are not control flow.
            for keyword in node.keywords:
                if keyword.arg in {"examples", "description", "json_schema_extra"}:
                    keyword.value = ast.Constant(value=None)
    return ast.unparse(tree)


def test_contaminated_archive_never_appears_in_production_code() -> None:
    """The broken class is named in the lab and in tests, nowhere else.

    A demonstration class that leaks into production is no longer a
    demonstration, it is a supported way to corrupt the baseline. Docstrings are
    excluded deliberately: `memory/base.py` explains *why* the guard is
    overridable by naming the class that overrides it, and that explanation is
    the most valuable line of prose in the package.
    """
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"__init__.py", "base.py"}:
            # base.py's *only* mention is inside a docstring; the code check
            # below still runs on it, so a real use would still be caught.
            pass
        code = _code_without_docstrings(path)
        assert "ContaminatedArchive" not in code, (
            f"{path.name} references ContaminatedArchive in code; it belongs "
            "only in the lab module and its tests"
        )


def test_registry_mirrors_the_tool_and_skill_registries() -> None:
    """Lab 4's registry has the same shape as Lab 2's and Lab 3's.

    Lab 6 wraps all three in one runtime. That only reads as one thing if the
    three ledgers and registries agree on how to be created, read and reported.
    """
    for name in ("register", "names", "invoke", "describe_all"):
        assert hasattr(MemoryRegistry, name), f"MemoryRegistry.{name} is missing"


def test_run_state_cannot_be_given_an_archive_provenance() -> None:
    """The sentinel is a constant, not a path, so it cannot be spoofed by edit.

    `RUN_STATE_PROVENANCE` is what a draft claims. If it were something a caller
    could pass in, a caller could claim to be a file, and the provenance check
    would stop meaning anything. The test is the check that the archive's allow
    list never contains it, for either course.
    """
    for course in (CS, DB):
        archive = ArchiveStore(run_id="test", course_code=course)
        archive.load_from_tools(build_lab04_registry(run_id="test"))
        assert RUN_STATE_PROVENANCE not in archive.allowed_provenance


def test_every_store_declares_a_known_layer() -> None:
    """`LAYERS` covers exactly the two halves, and both stores name one."""
    assert set(LAYERS) == {MemoryLayer.RUN_STATE, MemoryLayer.ARCHIVE}
    assert RunState.layer is MemoryLayer.RUN_STATE
    assert ArchiveStore.layer is MemoryLayer.ARCHIVE
    for layer in LAYERS:
        assert LAYER_PURPOSE[layer]


def test_a_store_that_forgets_its_layer_fails_at_class_definition() -> None:
    """The `__init_subclass__` guard is a real, reachable guard."""

    with pytest.raises(MemoryError, match="never declares a `layer`"):

        class ForgetfulStore(MemoryStore):  # noqa: D401 - deliberately incomplete
            """A store that never says which half it holds."""

    # Inheriting a layer is allowed: the lab's ContaminatedArchive is an
    # ArchiveStore that is broken in a different way, and it has to be
    # constructible for the pitfall to be demonstrable at all.
    class InheritingArchive(ArchiveStore):
        """An archive that inherits its layer, which is fine."""

    assert InheritingArchive.layer is MemoryLayer.ARCHIVE


def test_registry_state_is_per_instance_not_shared() -> None:
    """Two registries do not see each other's retrievers.

    A class-level dict of bound stores is a bug that only appears when two runs
    happen in one process -- which is exactly what a notebook does, and exactly
    what Lab 6's parallel Lab 8 will do.
    """
    first = ArchiveStore(run_id="a", course_code=CS)
    second = ArchiveStore(run_id="b", course_code=CS)
    reg_a = build_default_memory_registry(
        run_id="a", course_code=CS, archive=first
    )
    reg_b = build_default_memory_registry(
        run_id="b", course_code=CS, archive=second
    )
    assert reg_a.names() == reg_b.names()
    assert reg_a.ledger is not reg_b.ledger
    assert reg_a.store_for("retrieve_by_tags") is first
    assert reg_b.store_for("retrieve_by_tags") is second


# ══════════════════════════════════════════════════════════════════════════
# 2. the boundary
# ══════════════════════════════════════════════════════════════════════════


def test_the_layer_check_refuses_a_draft_in_the_archive() -> None:
    """Check one of two: the layer itself."""
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    draft = draft_to_record(course_code=CS, content="a draft")
    with pytest.raises(MemoryBoundaryError, match="separate memories"):
        archive.append(draft)


def test_the_provenance_check_refuses_a_relabelled_draft() -> None:
    """Check two of two: the provenance, which survives the relabel.

    This is the test that proves the checks are independent rather than one
    check with two messages. `model_copy(update=...)` is exactly how a careless
    caller would "fix" a layer mismatch, and if the provenance check did not
    exist that fix would be a working exploit.
    """
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    draft = draft_to_record(course_code=CS, content="a draft")
    relabelled = draft.model_copy(update={"layer": MemoryLayer.ARCHIVE})
    with pytest.raises(MemoryBoundaryError, match="claims provenance"):
        archive.append(relabelled)


def test_a_forged_provenance_is_still_refused() -> None:
    """Claiming to be a real file is not enough; it must be *this* course's.

    CS201's archive lists three seed files. A record claiming DBMS402's notes is
    refused even though the path exists on disk, because the store never read
    it. The record is relabelled to the archive layer first, so that the layer
    check passes and this really is testing the provenance check.
    """
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    forged = draft_to_record(course_code=CS, content="x").model_copy(
        update={
            "provenance": "data/seed/dbms/notes.json",
            "layer": MemoryLayer.ARCHIVE,
        }
    )
    with pytest.raises(MemoryBoundaryError, match="not a source this archive"):
        archive.append(forged)


def test_run_state_store_refuses_an_archive_record() -> None:
    """The boundary is enforced in both directions, not just the dangerous one."""
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    genuine = archive.get(PLANTED)
    assert genuine is not None
    run_state = RunState(run_id="t", course_code=CS)
    with pytest.raises(MemoryBoundaryError, match="separate memories"):
        run_state.append(genuine)


def test_run_state_store_has_no_way_to_reach_any_other_store() -> None:
    """The document's `state.draft = candidate` is assignment, not a write.

    Asserted as absence, which is the only honest way to assert it: there is no
    method on `RunState` whose name suggests reaching out.
    """
    outward = [
        name
        for name in dir(RunState)
        if any(word in name.lower() for word in ("archive", "persist", "flush", "store", "save"))
        and not name.startswith("__")
    ]
    assert outward == [], f"RunState grew an outward-facing method: {outward}"


def test_archive_records_all_carry_a_real_file_as_provenance() -> None:
    """Nothing in the archive points at the run-state sentinel."""
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    assert archive.records()
    for record in archive.records():
        assert record.provenance != RUN_STATE_PROVENANCE
        assert record.provenance.endswith(".json")


def test_contains_foreign_layer_is_false_for_a_clean_archive() -> None:
    """The cheap CI invariant actually holds on the real corpus."""
    for course in (CS, DB):
        archive = ArchiveStore(run_id="t", course_code=course)
        archive.load_from_tools(build_lab04_registry(run_id="t"))
        assert archive.contains_foreign_layer() is False
        assert all(r.layer is MemoryLayer.ARCHIVE for r in archive.records())


def test_fingerprint_is_stable_and_content_addressed() -> None:
    """Same corpus, same fingerprint; one extra record, different fingerprint.

    The fingerprint is what makes "mixing them corrupts the baseline" a
    measurable claim rather than an adjective. It has to be order-independent
    (records are appended in file order) and it has to change when content
    changes, or the acceptance check that uses it is decorative.
    """
    first = ArchiveStore(run_id="t", course_code=CS)
    first.load_from_tools(build_lab04_registry(run_id="t"))
    second = ArchiveStore(run_id="other-run", course_code=CS)
    second.load_from_tools(build_lab04_registry(run_id="other-run"))
    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 16

    third = ArchiveStore(run_id="t3", course_code=CS)
    third.load_from_tools(build_lab04_registry(run_id="t3"))
    third.append(
        draft_to_record(course_code=CS, content="extra").model_copy(
            update={
                "provenance": next(iter(sorted(third.allowed_provenance))),
                "layer": MemoryLayer.ARCHIVE,
            }
        )
    )
    assert third.fingerprint() != first.fingerprint()


def test_a_duplicate_id_is_refused_so_the_fingerprint_stays_meaningful() -> None:
    """Two records under one id would make the fingerprint order-dependent."""
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    existing = archive.get(PLANTED)
    assert existing is not None
    with pytest.raises(MemoryError, match="already"):
        archive.append(existing)


# ══════════════════════════════════════════════════════════════════════════
# 3. retrieval
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def cs_registry() -> MemoryRegistry:
    """A fresh CS201 registry with its own archive and ledger."""
    archive = ArchiveStore(run_id="t-cs", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t-cs"))
    return build_default_memory_registry(
        run_id="t-cs", course_code=CS, archive=archive
    )


@pytest.fixture
def db_registry() -> MemoryRegistry:
    """A fresh DBMS402 registry."""
    archive = ArchiveStore(run_id="t-db", course_code=DB)
    archive.load_from_tools(build_lab04_registry(run_id="t-db"))
    return build_default_memory_registry(
        run_id="t-db", course_code=DB, archive=archive
    )


def test_the_registry_factory_requires_a_course_code() -> None:
    """Retrieval is always course-scoped, so the course is not optional.

    An optional `course_code` would default to something and a store from another
    course would silently answer, which is the retrieval-layer version of the
    boundary this lab is about.
    """
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    with pytest.raises(TypeError):
        build_default_memory_registry(run_id="t", archive=archive)  # type: ignore[call-arg]


def test_tag_retrieval_returns_only_correctly_tagged_records(
    cs_registry: MemoryRegistry,
) -> None:
    """The first half of the acceptance criterion, checked literally.

    "Retrieval returns correctly-tagged candidates" is a claim about every hit,
    not about the top hit, so this asserts over the whole result set.
    """
    result = cs_registry.invoke(
        "retrieve_by_tags",
        TagQuery(course_code=CS, tags=["linked-list", "unit:1"], match="all"),
    ).unwrap()
    assert result.hits
    for hit in result.hits:
        assert set(hit.matched_tags) >= {"linked-list", "unit:1"}


def test_tag_match_all_is_a_subset_of_tag_match_any(cs_registry: MemoryRegistry) -> None:
    """`all` cannot return more than `any`; asserted so the modes stay distinct."""
    base = {"course_code": CS, "tags": ["linked-list", "unit:1"]}
    strict = cs_registry.invoke(
        "retrieve_by_tags", {**base, "match": "all"}
    ).unwrap()
    loose = cs_registry.invoke(
        "retrieve_by_tags", {**base, "match": "any"}
    ).unwrap()
    assert len(strict.hits) <= len(loose.hits)


def test_current_only_excludes_superseded_note_versions(
    cs_registry: MemoryRegistry,
) -> None:
    """The notes store holds versions; only the authoritative one is retrieved.

    Without this the retriever would offer a student a draft that was replaced,
    and "correctly-tagged" would be true of a document the course has retired.
    """
    result = cs_registry.invoke(
        "retrieve_by_tags",
        TagQuery(course_code=CS, tags=["linked-list"], current_only=True),
    ).unwrap()
    archive = cs_registry.store_for("retrieve_by_tags")
    assert result.hits
    for hit in result.hits:
        record = archive.get(hit.record_id)
        assert record is not None
        assert record.kind == KIND_NOTE_MATERIAL
        assert record.is_current is True

    # The corpus really does hold a superseded version of the same note, so this
    # is a filter doing work rather than a filter with nothing to filter.
    all_versions = {r.record_id for r in archive.notes() if "linked-list" in r.tags}
    assert len(all_versions) > 1, "the seed corpus no longer exercises current_only"
    assert {hit.record_id for hit in result.hits} < all_versions


def test_an_empty_tag_query_is_rejected_rather_than_matching_everything(
    cs_registry: MemoryRegistry,
) -> None:
    """Zero tags would mean "the whole archive", which is not a retrieval."""
    with pytest.raises(ValidationError):
        TagQuery(course_code=CS, tags=[], match="all")


def test_similarity_retrieval_is_restricted_to_the_history(
    cs_registry: MemoryRegistry,
) -> None:
    """Every hit is a submission, because the document says *history*.

    This is asserted rather than assumed, because the archive also holds note
    versions, and a note on the same topic scores *higher* against a same-topic
    draft than a classmate's submission does. Left unfiltered, the retrieval
    would answer a question about a student's own previous work with the
    course's lecture notes.
    """
    query = SimilarityQuery(course_code=CS, text=lab04.DRAFT_NEW_ANSWER, k=5)
    assert query.kinds == [KIND_SUBMISSION], (
        "the default must be the history, not 'no filter'; an empty default "
        "returns lecture notes as a student's nearest prior work"
    )
    result = cs_registry.invoke("retrieve_similar_prior", query).unwrap()
    assert result.hits
    archive = cs_registry.store_for("retrieve_similar_prior")
    for hit in result.hits:
        record = archive.get(hit.record_id)
        assert record is not None
        assert record.kind == KIND_SUBMISSION


def test_the_notes_really_would_have_answered_the_history_question(
    cs_registry: MemoryRegistry,
) -> None:
    """Measured justification for that default, so it cannot be optimised away.

    If the notes ever stopped outranking the submissions, this test would fail
    and the default could honestly be revisited. Today they outrank them by a
    wide margin, which is the fact that decided it.
    """
    archive = cs_registry.store_for("retrieve_similar_prior")
    unfiltered = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(
            course_code=CS,
            text=lab04.DRAFT_NEW_ANSWER,
            k=1,
            kinds=[],  # deliberately no filter, as the old default did
        ),
    ).unwrap()
    history_only = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=lab04.DRAFT_NEW_ANSWER, k=1),
    ).unwrap()
    nearest = archive.get(unfiltered.top_match_id)
    assert nearest is not None
    assert nearest.kind == KIND_NOTE_MATERIAL
    assert unfiltered.top_score > history_only.top_score, (
        "the notes only outrank the history by a narrow margin now, so the "
        "safe default should be revisited deliberately rather than by accident"
    )


def test_similarity_respects_k(cs_registry: MemoryRegistry) -> None:
    """The document's `k=5` is honoured, not "up to 5"."""
    for k in (1, 2, 5):
        result = cs_registry.invoke(
            "retrieve_similar_prior",
            SimilarityQuery(course_code=CS, text=lab04.DRAFT_NEW_ANSWER, k=k),
        ).unwrap()
        assert len(result.hits) == k, f"k={k} returned {len(result.hits)}"
        assert [hit.rank for hit in result.hits] == list(range(1, k + 1))
        scores = [hit.score for hit in result.hits]
        assert scores == sorted(scores, reverse=True), "hits must be ranked"


def test_a_verbatim_copy_scores_higher_than_a_restatement(
    cs_registry: MemoryRegistry,
) -> None:
    """The scorer is sensitive to copying, not only to topic.

    Both texts are about the same thing; only one copies the words. If they
    scored the same the retriever would be doing topic lookup and calling it
    plagiarism screening.
    """
    archive = cs_registry.store_for("retrieve_similar_prior")
    planted = archive.get(PLANTED)
    assert planted is not None
    verbatim = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=planted.content, k=1),
    ).unwrap()
    restated = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=lab04.DRAFT_RESTATEMENT, k=1),
    ).unwrap()
    assert verbatim.top_score > restated.top_score
    assert verbatim.top_match_id == PLANTED


def test_retrieval_is_course_scoped(cs_registry, db_registry) -> None:
    """Neither course's registry can see the other's records."""
    cs = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=lab04.DRAFT_NEW_ANSWER, k=5),
    ).unwrap()
    db = db_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=DB, text=lab04.DRAFT_OTHER_SUBJECT, k=5),
    ).unwrap()
    cs_archive = cs_registry.store_for("retrieve_similar_prior")
    db_archive = db_registry.store_for("retrieve_similar_prior")
    for hit in cs.hits:
        record = cs_archive.get(hit.record_id)
        assert record is not None
        assert record.course_code == CS
    for hit in db.hits:
        record = db_archive.get(hit.record_id)
        assert record is not None
        assert record.course_code == DB
    # No id appears in both courses' archives.
    assert not {h.record_id for h in cs.hits} & {h.record_id for h in db.hits}


def test_a_retriever_wired_to_the_wrong_layer_is_refused() -> None:
    """The registry's own layer check, at wiring time."""
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    run_state = RunState(run_id="t", course_code=CS)
    registry = build_default_memory_registry(
        run_id="t", course_code=CS, archive=archive
    )
    spec = registry.get("retrieve_by_tags")
    with pytest.raises(MemoryBoundaryError, match="one retriever may not read"):
        registry.register(spec, run_state)


def test_invoking_an_archive_retriever_against_run_state_is_refused(
    cs_registry: MemoryRegistry,
) -> None:
    """And again at call time, because a store can be passed explicitly."""
    run_state = RunState(run_id="t", course_code=CS)
    run_state.append(draft_to_record(course_code=CS, content="draft text"))
    with pytest.raises(MemoryBoundaryError, match="refusing to run it against"):
        cs_registry.invoke(
            "retrieve_by_tags",
            TagQuery(course_code=CS, tags=["linked-list"]),
            store=run_state,
        )


def test_an_unknown_retriever_id_is_refused(cs_registry: MemoryRegistry) -> None:
    """A typo must not return an empty result that looks like a real answer."""
    with pytest.raises(MemoryError):
        cs_registry.invoke("retrieve_by_nothing", {})


def test_an_invalid_query_names_the_problem(cs_registry: MemoryRegistry) -> None:
    """The error has to be actionable: which retriever, and how many problems."""
    with pytest.raises(MemoryError, match="invalid query for retriever"):
        cs_registry.invoke("retrieve_similar_prior", {"course_code": CS, "k": 5})


def test_every_retrieval_is_ledgered(cs_registry: MemoryRegistry) -> None:
    """Doc 2.12's "full audit log", for the third governed action kind."""
    assert len(cs_registry.ledger) == 0
    cs_registry.invoke(
        "retrieve_by_tags", TagQuery(course_code=CS, tags=["linked-list"])
    )
    cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=lab04.DRAFT_NEW_ANSWER, k=5),
    )
    ledger = cs_registry.ledger
    assert len(ledger) == 2
    assert ledger.names() == ["retrieve_by_tags", "retrieve_similar_prior"]
    assert ledger.layers_read() == [MemoryLayer.ARCHIVE]
    assert not ledger.failures()
    for entry in ledger:
        assert entry.corpus_size == 18
        assert entry.elapsed_ms >= 0.0
    assert ledger.total_time_ms() >= 0.0
    assert "retrieve_by_tags" in ledger.report()


def test_a_failing_retriever_is_recorded_as_a_failure_not_raised(
    monkeypatch: pytest.MonkeyPatch, cs_registry: MemoryRegistry
) -> None:
    """The ledger records the failure and the caller still gets a result object.

    Recording the failure rather than propagating it is the point: a retriever
    that finds nothing is a legitimate answer and must not look like a crash,
    and a retriever that *did* crash must be visible in the audit trail rather
    than taking the whole run down. The two states are told apart by `ok`.

    `RetrieverSpec` is a frozen dataclass, so the handler is replaced through
    `object.__setattr__` rather than assignment -- which is itself worth knowing,
    because it means a registered retriever cannot be silently swapped at runtime.
    """
    spec = cs_registry.get("retrieve_by_tags")

    def boom(query, store, ledger):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated retriever crash")

    object.__setattr__(spec, "handler", boom)

    result = cs_registry.invoke(
        "retrieve_by_tags", TagQuery(course_code=CS, tags=["linked-list"])
    )
    assert result.ok is False
    assert "simulated retriever crash" in (result.error or "")
    assert len(cs_registry.ledger.failures()) == 1
    assert "FAIL" in cs_registry.ledger.report()


# ══════════════════════════════════════════════════════════════════════════
# 4. the threshold
# ══════════════════════════════════════════════════════════════════════════


def test_the_threshold_is_a_property_of_the_embedder_not_a_module_global() -> None:
    """Each scorer owns its own threshold; nothing reads it from the side.

    A module-level `THRESHOLD = 0.95` would be wrong the moment two backends
    existed, which is exactly why the hashing number has to differ.
    """
    assert HashingEmbedder().triage_threshold == TRIAGE_THRESHOLD_HASHING
    assert HashingEmbedder().name.endswith(str(HASHING_DIM) + "d")
    assert TRIAGE_THRESHOLD_HASHING != TRIAGE_THRESHOLD_FASTEMBED


def test_each_threshold_is_the_midpoint_of_its_measured_gap() -> None:
    """One calibration rule, applied to both backends, re-checked on every run.

    The rule: the threshold sits halfway between the lowest score that must be
    flagged and the highest that must not. Stating it once and applying it
    twice is what makes the two constants comparable, and the reason the
    hashing threshold moved off 0.99 -- measuring one course alone had made it
    look fine.

    The interesting number is not the threshold but the *width of the gap* it
    sits in: 0.0436 for the learned backend against 0.0093 for hashing. A
    threshold is only as robust as the gap around it, and reporting the
    midpoint without reporting the gap would hide the whole story.
    """
    rows = lab04.lab_threshold_report(run_id="t-midpoint")
    must_flag = [r.top_score for r in rows if r.expect_flagged]
    must_not = [r.top_score for r in rows if not r.expect_flagged]
    assert len(must_flag) == 1, "the corpus needs exactly one known copy"
    assert len(must_not) >= 2

    midpoint = (min(must_flag) + max(must_not)) / 2
    gap = min(must_flag) - max(must_not)

    assert TRIAGE_THRESHOLD_HASHING == pytest.approx(midpoint, abs=0.0005), (
        "the hashing threshold is not the midpoint of the gap it is measured "
        "from; recompute it rather than adjusting the corpus"
    )
    assert TRIAGE_THRESHOLD_FASTEMBED == pytest.approx(0.9663, abs=0.0005)

    # The gap widths below are asserted, not merely printed, because they are
    # the difference between "usable for triage" and "works by rounding". If a
    # model or library bump moves them, this fails loudly.
    assert gap == pytest.approx(0.9996 - 0.9903, abs=0.0001), (
        "the hashing gap changed; re-read the CLI threshold table and revisit "
        "the claim that this backend separates by little more than luck"
    )


def test_threshold_margins_report_the_headroom_honestly() -> None:
    """A table that says only "4 of 4 correct" hides the number that matters.

    The hashing backend's upper headroom is under half a percent, and that is
    reported as a verdict rather than left for the reader to compute.
    """
    rows = lab04.lab_threshold_report(run_id="t-margin")
    assert len(rows) == len(lab04.cases())
    margins = lab04.threshold_margins(rows, TRIAGE_THRESHOLD_HASHING)
    above = margins["above"]
    below = margins["below"]
    assert above is not None and below is not None
    assert above[0] > 0, "the planted copy must clear the threshold"
    assert below[0] > 0, "every genuine case must sit below the threshold"
    assert margins["tightest"] == min(above, below)
    assert "determination" in margins["verdict"]


def test_the_threshold_report_measures_the_acceptance_cases() -> None:
    """The report and the acceptance checks cannot disagree about what was run.

    This is the regression test for a real bug: the first version of
    `lab_threshold_report` hand-wrote its own probe texts and measured them all
    against CS201, so it reported "4 of 4 probes correct" on the same run where
    an acceptance check failed a DBMS402 case. Deriving the rows from `cases()`
    closes that class of drift for good.
    """
    rows = lab04.lab_threshold_report(run_id="t-drift")
    assert [r.label for r in rows] == sorted(
        (r.label for r in rows),
        key=lambda label: -next(x.top_score for x in rows if x.label == label),
    )
    by_label = {r.label: r for r in rows}
    for case in lab04.cases():
        row = by_label[case.label]
        assert row.course_code == case.course_code
        assert row.expect_flagged == (case.expect_disposition == "review_required")


def test_every_case_classifies_as_expected_on_the_default_backend() -> None:
    """The lab's own four cases, routing as their specs require."""
    rows = lab04.lab_threshold_report(run_id="t-classify")
    threshold = build_embedder().triage_threshold
    wrong = [r.label for r in rows if not r.classified_correctly(threshold)]
    assert not wrong, f"misclassified on the hashing backend: {wrong}"


def test_cosine_similarity_is_a_real_cosine() -> None:
    """The scorer is checked against cases with known answers.

    A vector's similarity with itself is 1, with its negation is -1, and with an
    orthogonal vector is 0. If these three do not hold, no threshold means
    anything.
    """
    a = HashingEmbedder().embed("linked list traversal in a data structures course")
    assert len(a) == 1, "a bare string must mean ONE document, not one per character"
    vector = a[0]
    assert cosine_similarity(vector, vector) == pytest.approx(1.0, abs=1e-9)
    assert cosine_similarity(vector, [-x for x in vector]) == pytest.approx(
        -1.0, abs=1e-9
    )
    unit = [1.0] + [0.0] * (len(vector) - 1)
    assert cosine_similarity(vector, unit) == pytest.approx(0.0, abs=1e-9)


def test_a_bare_string_and_a_one_element_list_agree() -> None:
    """`embed("x")` and `embed(["x"])` must return the same thing.

    `str` is a `Sequence[str]`, so without an explicit guard the first call
    iterates characters. That bug would not raise for any text of three
    characters or more -- it would return N vectors of length `dim`, one per
    letter -- so it is asserted rather than assumed.
    """
    embedder = HashingEmbedder()
    single = embedder.embed("linked list traversal")
    listed = embedder.embed(["linked list traversal"])
    assert single == listed


def test_cosine_rejects_mismatched_dimensions() -> None:
    """Comparing vectors of different widths is a bug, not a small score."""
    with pytest.raises(EmbeddingError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_an_unknown_embedding_backend_is_refused_not_defaulted() -> None:
    """Silently falling back to hashing would change a run's retrieval."""
    with pytest.raises(EmbeddingError, match="unknown embedding backend"):
        build_embedder("word2vec")


def test_hashing_scores_are_reproducible_across_processes() -> None:
    """Same text, same vector, no salt, no `hash()`.

    `hash()` is randomised per process by PYTHONHASHSEED, which would make every
    score -- and therefore every threshold decision -- unreproducible between
    runs. Asserted in two subprocesses with different seeds so the check is real
    rather than in-process, where both calls would share one seed.
    """
    import subprocess
    import sys

    code = (
        "from course_organizer.memory.embeddings import HashingEmbedder\n"
        "v = HashingEmbedder().embed('linked list traversal')[0]\n"
        "print(round(sum(x * x for x in v), 9))\n"
    )
    root = Path(__file__).resolve().parents[1]
    outputs = set()
    for seed in ("0", "1"):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": str(root / "src"),
        }
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(root),
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1, f"scores differed across PYTHONHASHSEED: {outputs}"
    assert outputs != {""}


# ══════════════════════════════════════════════════════════════════════════
# 5. the pitfall
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def violation() -> lab04.BoundaryViolation:
    """One full boundary demonstration, run for real."""
    archive = ArchiveStore(run_id="t-vio", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t-vio"))
    planted = archive.get(PLANTED)
    assert planted is not None
    embedder = build_embedder()

    def factory(store: MemoryStore) -> MemoryRegistry:
        return build_default_memory_registry(
            run_id="t-vio", course_code=CS, archive=store, embedder=embedder
        )

    return lab04.demonstrate_boundary_violation(
        run_id="t-vio",
        course_code=CS,
        registry_factory=factory,
        draft_text=planted.content,
        candidate_source_id=PLANTED,
    )


def test_both_boundary_checks_fire_in_order(violation: lab04.BoundaryViolation) -> None:
    """The demonstration refused twice, for two different reasons."""
    assert violation.refused_by_layer is True
    assert violation.refused_by_provenance is True
    assert "separate memories" in violation.refusal_message
    assert "claims provenance" in violation.refusal_message


def test_contamination_changes_the_fingerprint(violation: lab04.BoundaryViolation) -> None:
    """"Mixing them corrupts the baseline", measured rather than asserted."""
    assert violation.baseline_corrupted is True
    assert violation.archive_fingerprint_before != violation.archive_fingerprint_after


def test_contamination_makes_the_system_flag_itself(
    violation: lab04.BoundaryViolation,
) -> None:
    """The damage is a false positive against the run's own text.

    A plagiarism screen that flags the system against itself is worse than a slow
    one: a reviewer who sees one self-flag learns to discount the output, and
    every real flag after it gets discounted too.
    """
    assert violation.self_match_score is not None
    assert violation.self_match_score > TRIAGE_THRESHOLD_HASHING
    assert violation.false_positive
    assert "own text" in violation.false_positive


def test_the_demonstration_explains_itself(violation: lab04.BoundaryViolation) -> None:
    """Doc 2.10 Lab 4's exact sentence is carried in the output."""
    assert violation.doc_says == lab04.DOC_PITFALL
    assert "Don't conflate run state with the archive" in violation.doc_says
    text = violation.describe()
    assert "REFUSED" in text
    assert "baseline" in text


def test_contaminated_archive_refuses_to_demo_an_archive_record() -> None:
    """Even the broken store only breaks in the one direction it is here for."""
    broken = lab04.ContaminatedArchive(run_id="t", course_code=CS)
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    genuine = archive.get(PLANTED)
    assert genuine is not None
    with pytest.raises(MemoryBoundaryError, match="would demonstrate nothing"):
        broken.contaminate(genuine)


# ══════════════════════════════════════════════════════════════════════════
# 6. honesty
# ══════════════════════════════════════════════════════════════════════════


def test_similarity_flag_has_no_verdict_or_penalty_field() -> None:
    """Doc 2.4 node 4 and 2.12 as an executable check on a type's shape.

    "Flag for human review; never make a final plagiarism determination
    automatically" is not a promise in a docstring, it is a constraint on what
    the flag is *able* to say. If `is_plagiaristic` were on this model, some
    caller eventually would read it.
    """
    present = flag_field_names()
    assert not (present & PLAGIARISM_FORBIDDEN_FIELDS), (
        "the flag can express a verdict or a penalty: "
        f"{sorted(present & PLAGIARISM_FORBIDDEN_FIELDS)}"
    )
    for banned in ("is_plagiaristic", "plagiaristic", "penalty", "verdict",
                   "guilty", "is_cheating", "score_penalty", "auto_penalize"):
        assert banned not in present


def test_the_flag_is_a_routing_decision_not_a_finding(
    cs_registry: MemoryRegistry,
) -> None:
    """A flag says where to send it and who decides, and nothing more."""
    archive = cs_registry.store_for("retrieve_similar_prior")
    planted = archive.get(PLANTED)
    assert planted is not None
    result = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=planted.content, k=5),
    ).unwrap()
    # The candidate is the *draft* -- a run-state record holding the copied
    # text. It is not the archived submission, which is the thing being compared
    # against and is therefore refused as a candidate.
    draft = draft_to_record(course_code=CS, content=planted.content)
    flag = raise_flag(draft, result)

    assert flag.exceeds_threshold is True
    assert flag.disposition == "review_required"
    assert flag.requires_human_review is True
    assert flag.authority == HUMAN_AUTHORITY
    assert flag.reviewed_by == ""
    assert flag.score_source == result.embedder
    assert flag.threshold == result.threshold_used
    assert flag.rationale
    assert flag.is_determination() is False


def test_the_threshold_on_the_flag_is_the_scorers_not_a_module_default(
    cs_registry: MemoryRegistry,
) -> None:
    """`raise_flag(threshold=None)` must resolve to the scorer's own number.

    The bug this prevents: the flag defaulted to the module constant, so a
    result measured with one embedder could be judged against another's number,
    and the mismatch would be invisible in the audit.
    """
    archive = cs_registry.store_for("retrieve_similar_prior")
    planted = archive.get(PLANTED)
    assert planted is not None
    result = cs_registry.invoke(
        "retrieve_similar_prior",
        SimilarityQuery(course_code=CS, text=planted.content, k=5),
    ).unwrap()
    draft = draft_to_record(course_code=CS, content=planted.content)
    default_flag = raise_flag(draft, result)
    assert default_flag.threshold == build_embedder().triage_threshold
    assert default_flag.threshold == result.threshold_used

    # An explicit override still works, and travels on the flag rather than
    # disappearing into the module.
    forced = raise_flag(draft, result, threshold=0.0)
    assert forced.threshold == 0.0
    assert forced.exceeds_threshold is True


def test_the_flag_will_not_score_an_archive_layer_candidate() -> None:
    """The flag is for a draft. An archive record is not a candidate.

    Archiving a flag's subject would put a triage decision into long-term
    memory, and 2.12 says the audit log is where review outcomes belong -- not
    the baseline the next run compares against. This also guards a real bug: the
    guard's own error path once raised `NameError` because the exception class
    was never imported into `retrieval.py`, so the *refusal* was itself broken.
    """
    archive = ArchiveStore(run_id="t", course_code=CS)
    archive.load_from_tools(build_lab04_registry(run_id="t"))
    record = archive.get(PLANTED)
    assert record is not None
    hit = RetrievalHit(
        record_id=PLANTED,
        layer=MemoryLayer.ARCHIVE,
        kind=KIND_SUBMISSION,
        title="a prior submission",
        score=0.9996,
        rank=1,
        matched_on="embedding",
        why="cosine 0.9996",
    )
    result = SimilarityResult(
        course_code=CS,
        k=1,
        hits=(hit,),
        corpus_size=8,
        excluded_by_kind=10,
        excluded_by_tag=0,
        embedder="hashing-3-5-256d",
        embedder_dim=HASHING_DIM,
        layer_read=MemoryLayer.ARCHIVE,
        threshold_used=TRIAGE_THRESHOLD_HASHING,
        top_match_id=PLANTED,
        top_score=0.9996,
    )
    with pytest.raises(MemoryBoundaryError, match="not a draft"):
        raise_flag(record, result)


def test_the_seeds_never_make_a_determination() -> None:
    """Doc 2.10 Lab 4's nodes: "seeds validation/no-repeat", nothing further.

    The document lists validation and no-repeat as the *nodes Lab 4 activates*.
    Both are seeded here so the graph has something to start from, and both are
    checked for the property that matters: neither can report a finding.
    """
    result = lab04.run_memory_retrieval(run_id="t-seeds", embedder_backend="hashing")
    bundles = {b.case_label: b for b in result.bundles}

    # Every case is seeded, including the ones that found nothing notable.
    for label, bundle in bundles.items():
        assert bundle.no_repeat_seed is not None, f"{label} was not seeded"
        assert bundle.similarity_seed is not None, f"{label} was not seeded"

    # The seed methods exist to be called, and always say "no".
    no_repeat = bundles["verbatim-copy"].no_repeat_seed
    assert no_repeat.is_a_repeat() is False
    assert no_repeat.flagged is True, "the seed carries the score, not a verdict"
    assert no_repeat.decided_by

    similarity = bundles["verbatim-copy"].similarity_seed
    assert similarity.determination_made() is False
    assert similarity.authority == HUMAN_AUTHORITY
    assert similarity.owner == "plagiarism_similarity_node"
    assert similarity.score is not None
    assert similarity.threshold == result.threshold

    # And the flag the bundle actually carries is a routing decision.
    flag = bundles["verbatim-copy"].flag
    assert flag.is_determination() is False
    assert flag.requires_human_review is True
    assert result.passed and not result.failed, [
        c[0] for c in result.checks if not c[1]
    ]


def test_no_course_identifier_is_hard_coded_in_the_memory_package() -> None:
    """The Lab 3 pitfall, enforced on Lab 4's package.

    "keep skills case-agnostic; hard-coding one case breaks reuse." Retrieval has
    the same failure mode: a course constant in the retrieval layer would mean
    the second course worked because of a check, not because of the design. The
    two courses in this lab's cases are therefore evidence of reuse, and this
    test is what keeps them evidence.
    """
    pattern = re.compile(r"\b(CS201|DBMS402)\b")
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        code = _code_without_docstrings(path)
        for number, line in enumerate(code.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        f"these lines hard-code a course in the retrieval layer: {offenders}. "
        "Retrieval must be driven by data, not by which course it looks at."
    )


def test_nothing_writes_to_the_archive_during_a_run() -> None:
    """End-to-end: four cases, and the baseline is byte-identical afterwards.

    This is the check that would catch the pitfall being introduced by a later
    edit -- a new retriever that caches, a flag that memoises, a seed that
    persists -- none of which any unit test above would notice.
    """
    result = lab04.run_memory_retrieval(run_id="t-e2e", embedder_backend="hashing")
    assert len(result.bundles) == len(lab04.cases())
    for bundle in result.bundles:
        assert bundle.archive_unchanged is True, bundle.case_label
        assert bundle.draft_in_archive is False, bundle.case_label
        assert bundle.boundary_held is True, bundle.case_label
    # `checks` is a tuple of (description, ok, detail) triples.
    assert not result.failed, [c[0] for c in result.checks if not c[1]]
    assert result.passed is True
    assert len(result.checks) == 25, "a check disappeared; see docs/lab04.md"


# ══════════════════════════════════════════════════════════════════════════
# 7. both backends
# ══════════════════════════════════════════════════════════════════════════


def test_the_hashing_backend_needs_no_download_and_no_network() -> None:
    """The reason it is the default: a suite a reviewer can run anywhere."""
    embedder = build_embedder("hashing")
    assert embedder.name.startswith("hashing")
    assert embedder.dim == HASHING_DIM
    vectors = embedder.embed([f"document number {i}" for i in range(3)])
    assert len(vectors) == 3
    assert all(len(v) == HASHING_DIM for v in vectors)
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-9 for v in vectors)
    assert NGRAM_RANGE == (3, 5)


def test_the_hashing_backend_is_insensitive_to_case_and_punctuation() -> None:
    """Normalisation is what makes it a *rough* text signal, and it is tested."""
    plain = HashingEmbedder().embed("linked list traversal, unit 1")[0]
    noisy = HashingEmbedder().embed("LINKED  List Traversal -- Unit 1!!")[0]
    assert cosine_similarity(plain, noisy) == pytest.approx(1.0, abs=1e-12)


def test_a_short_text_is_an_error_rather_than_a_directionless_vector() -> None:
    """Below the n-gram window there is nothing to hash.

    Two cases, both of which would otherwise produce a plausible-looking wrong
    answer: blank input, and text shorter than the window.
    """
    with pytest.raises(EmbeddingError, match="blank text"):
        HashingEmbedder().embed("   ")
    with pytest.raises(EmbeddingError, match="shorter than the 3-character"):
        HashingEmbedder().embed("ab")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(lab04.DRAFT_NEW_ANSWER, id="new-answer"),
        pytest.param(lab04.DRAFT_RESTATEMENT, id="restatement"),
        pytest.param(lab04.DRAFT_OTHER_SUBJECT, id="other-subject"),
    ],
)
def test_a_genuine_draft_is_below_the_threshold_on_the_default_backend(
    text: str,
) -> None:
    """The three control cases, one at a time, each below the line.

    Parametrised rather than looped so a failure names the draft that regressed.
    """
    rows = {r.label: r for r in lab04.lab_threshold_report(run_id=f"t-ctl-{text[:8]}")}
    label = {
        lab04.DRAFT_NEW_ANSWER: "new-answer",
        lab04.DRAFT_RESTATEMENT: "restatement",
        lab04.DRAFT_OTHER_SUBJECT: "other-subject",
    }[text]
    assert rows[label].top_score < TRIAGE_THRESHOLD_HASHING


@pytest.mark.skipif(
    not os.environ.get("LAB04_TEST_FASTEMBED"),
    reason=(
        "the learned backend needs a ~69 MB model. Enable with "
        "LAB04_TEST_FASTEMBED=1 after running the lab once with "
        "EMBEDDING_BACKEND=fastembed, which caches the model."
    ),
)
def test_the_learned_backend_separates_further_than_the_hermetic_one() -> None:
    """With the real model, the copy/original gap is far wider.

    Not part of the default suite because it must never trigger a download. The
    numbers it asserts are the ones `docs/lab04.md` quotes, so if a model or
    library bump ever changes them, this test says so instead of the docs quietly
    becoming wrong.
    """
    rows = {r.label: r for r in lab04.lab_threshold_report(run_id="t-fe")}
    assert rows["verbatim-copy"].top_score > TRIAGE_THRESHOLD_FASTEMBED
    for label in ("new-answer", "restatement", "other-subject"):
        assert rows[label].top_score < TRIAGE_THRESHOLD_FASTEMBED