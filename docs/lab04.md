# Lab 4 — Memory & Retrieval: state vs stored
**Phase:** A — Foundations
**Goal (doc §2.10, Lab 4):** *"Separate short-term run state from long-term stored knowledge."*
**Nodes activated (doc §2.10):** *"Retrieval node; seeds validation/no-repeat."*
**Agents/nodes (doc):** *"Retrieval agent; foundations of later screening."*
---

## a) Goal and the document's requirements

Every requirement below is quoted from Worklet 2 (Lab 4, lines 643-651). Nothing in
this lab is inferred beyond those lines plus the guardrails in §2.4 and §2.12.

| # | Doc requirement (verbatim) | Where it is met |
|---|---|---|
| R1 | *"Separate short-term run state from long-term stored knowledge."* | `MemoryLayer` has exactly two values, `RUN_STATE` and `ARCHIVE`. `MemoryStore.append()` enforces **two independent checks** - layer match *and* provenance allow-list - and both live in one method, `_check_appendable()`. |
| R2 | *"Session state (the current draft) vs stored reference - the notes/materials store and the history of past assignment submissions - retrieved by tags and by similarity."* | `RunState` holds drafts and nothing else. `ArchiveStore.load_from_tools()` calls `read_notes_store` and `read_submission_records`, never anything that writes. Two retrievers, one per retrieval mode. |
| R3 | *"Retrieval over the store + history with tag filters and embedding similarity.  Accept: For a given task, retrieval returns correctly-tagged candidates and similar prior items."* | `TagQuery` (`match="all"`/`"any"`, `current_only`) and `SimilarityQuery` (`k`, `kinds`, `layer_read`). Acceptance is checked per case: every tag hit carries every requested tag, and similarity returns prior items. |
| R4 | *"Nodes activated.  Retrieval node; seeds validation/no-repeat."* | `RetrievalAgent.retrieve()` runs the node. It returns `NoRepeatSeed` and `SimilarityCheckSeed` per case; neither can ever return a determination. |
| R5 | *"Pitfall to avoid.  Don't conflate run state with the archive - mixing them corrupts the baseline."* | Made measurable. `MemoryStore.fingerprint()` is a 16-hex content digest; `ContaminatedArchive` overrides `_check_appendable()` so the corruption can be *run*: the baseline moves and the screen flags the system's own text. |
| R6 | *"state.draft = candidate  # short-term"* | `RetrievalAgent` calls `run_state.set_draft(...)`, and the candidate it passes on is `run_state.draft()` - a `RUN_STATE` record. A test asserts `RunState` exposes no way to reach any other store. |
| R7 | *"hits  = retrieve(store, tags)  # long-term"* | `retrieve_by_tags` is wired to the archive and refuses to run against run state. |
| R8 | *"prior = retrieve(history, embed(candidate), k=5)  # similarity"* | `retrieve_similar_prior` with `k=5`, restricted to `kinds=["submission"]`. That restriction is R2's "history of past assignment submissions" made literal, and §6 explains why it is not optional. |
| R9 | §2.4 node 4: *"Flag for human review; never make a final plagiarism determination automatically."* | `SimilarityFlag` has **no field in which a verdict could be written**. `PLAGIARISM_FORBIDDEN_FIELDS` (19 names) is asserted disjoint from the type's own field names. `reviewed_by` is empty on every flag; `authority` is the constant `course_faculty`. |
| R10 | §2.12: *"Flag high-similarity submissions for review only; never auto-penalize."* | The flag's `disposition` is `review_required` or `within_threshold`, never a grade. `raise_flag()` refuses an archive-layer candidate outright. |

### Why four cases, and why one of them is a different course

The acceptance criterion has two halves - correctly-tagged candidates, and similar
prior items - and a threshold has two sides. Three of the four cases sit in `CS201`;
the fourth is in `DBMS402`, and it is the one that matters most:

* `new-answer` - a new answer on a covered topic. The false-positive you get from a
  screen that only knows about copies.
* `verbatim-copy` - the planted copy of `DS-A1-S001`. The one true positive.
* `restatement` - the same content as `DS-A1-S002`, reordered and reworded. The
  case a *good* screen should flag and a naive one misses.
* `other-subject` - a new `DBMS402` answer to the normalisation question. It exists
  to prove the retrieval layer is course-agnostic, and it is also **the binding
  constraint on the threshold**: it is a different written answer to the *identical*
  question - same candidate keys, same transitive dependency, same supplier schema,
  same spurious-tuple counterexample - in different words. Section 3 shows the text.

The cross-course case is not decoration. When the measurement covered `CS201` alone,
both backends' thresholds looked comfortable; adding it exposed a real answer sitting
above the line. Section 3 gives the arithmetic.
---

## b) Brief concepts

### One field cannot express "separate the two memories"

The requirement sounds like one rule. It is two, and the reason it is worth two is
that each one has a working exploit:

| | `layer` | `provenance` |
|---|---|---|
| says | which half of memory this record is | *where this content actually came from* |
| values | `run_state` \| `archive` | a seed file path, or the sentinel `run_state:draft` |
| the attack it stops | writing a draft into the archive | relabelling the draft, or forging a plausible path |

A layer-only check is defeated by `record.model_copy(update={"layer": ARCHIVE})`.
That is not a contrived attack; `model_copy` is the ordinary way to derive a
variant record, and a variant with a corrected title should not thereby acquire a
license to be archived. So `append()` checks **layer first, provenance second**, and
both checks live in `_check_appendable()` only:

```python
def _check_appendable(self, record: MemoryRecord) -> None:
    if record.layer is not self.layer:
        raise MemoryBoundaryError(...)
    if record.provenance not in self.allowed_provenance:
        raise MemoryBoundaryError(...)
```

One method, one place to look, therefore one place to bypass - which is exactly why
`ContaminatedArchive` (section 8) has to override *that* method and not something
else. The base class is arranged so the demonstration cannot be faked.

### Provenance is a fact, not a claim

`allowed_provenance` on an `ArchiveStore` is the set of files the store actually
read, collected during `load_from_tools()`. It is not a schema of "what a valid
archive path looks like". A forged path that exists on disk but belongs to another
course is still refused, because the store never opened it. Test:
`test_a_forged_provenance_is_still_refused`.

The symmetric half matters too. `RUN_STATE_PROVENANCE = "run_state:draft"` is a
sentinel rather than a path, precisely so it cannot name a file - a run-state
record can never claim to have been read from disk. The allow-list is empty except
for the sentinel.

### The baseline needs a name before it can be corrupted

`fingerprint()` digests the sorted per-record fingerprints into 16 hex characters.
Order-independent, content-addressed, and stable across processes and run ids -
verified by loading the same corpus under two different run ids and comparing.

This is what makes R5 testable. Without it, "mixing them corrupts the baseline" is
an assertion in a docstring; with it, the CLI prints
`baseline : 9ea328fcba23de18 -> 6fd02fd9ef4fe30d corrupted=True` and a test asserts
those two values differ.

### `contains_foreign_layer()` is the cheap CI invariant

The full invariant is two checks. The cheap one is: does this store hold any record
whose `layer` disagrees with its own? One pass, no exceptions, and it catches the
class of bug where provenance was checked and layer was not.

### A registry, so Lab 6 has one shape to wrap

`MemoryRegistry` deliberately mirrors Lab 2's `ToolRegistry` and Lab 3's
`SkillRegistry`: `register()`, `names()`, `get()`, `invoke()` (returning a result
object with `.unwrap()`), a `ledger`, and `describe_all()`. Lab 6 wraps all three in
one runtime, and a uniform shape is what makes that a single implementation instead
of three. A test asserts the parity rather than trusting it.

### Embeddings: two backends, each with its own threshold

`EMBEDDING_BACKEND` selects one:

* **`hashing`** (default) - 3-5 character n-grams, 256 dimensions, blake2b, unsalted.
  No download, no network, fully deterministic. This is why it is the default: the
  acceptance criteria stay re-checkable by a reviewer with no credentials.
* **`fastembed`** - `BAAI/bge-small-en-v1.5`, 384 dimensions, ~69 MB one-time
  download, ~30 s cold.

Each embedder carries **its own** `triage_threshold`, because the score
distributions are not comparable. Across the 20 similarity hits this lab actually
retrieves, the hashing backend spans 0.9707-0.9996 - a usable spread of three
hundredths, and a verdict band has to fit inside it - while fastembed spans
0.6883-0.9881, thirty times wider. A single global threshold would have to be wrong
for one of them.

### A triage threshold is a routing decision

Nothing here decides that plagiarism occurred. The threshold decides *what a human
looks at*. `SimilarityFlag` enforces this structurally rather than by convention -
see section 6.
---

## c) The code

### Files

| file | lines | what it holds |
|---|---|---|
| `src/course_organizer/memory/__init__.py` | 195 | the package's public surface, and `build_default_memory_registry()` |
| `src/course_organizer/memory/base.py` | 1047 | `MemoryLayer`, `MemoryRecord`, `MemoryStore` (both invariants), `MemoryRegistry`, `RetrievalLedger`, `RetrievalResult`, `RetrieverSpec` |
| `src/course_organizer/memory/stores.py` | 539 | `RunState`, `ArchiveStore`, `draft_to_record()`. The **only** module that constructs `MemoryRecord` |
| `src/course_organizer/memory/embeddings.py` | 735 | `Embedder` protocol, `HashingEmbedder`, `FastEmbedEmbedder`, `build_embedder()`, both `TRIAGE_THRESHOLD_*` constants, `cosine_similarity()` |
| `src/course_organizer/memory/retrieval.py` | 941 | `TagQuery`, `SimilarityQuery`, the two retrievers, `TagResult`, `SimilarityResult`, `SimilarityFlag`, `raise_flag()`, `PLAGIARISM_FORBIDDEN_FIELDS` |
| `src/course_organizer/labs/lab04_memory_retrieval.py` | 2038 | `RetrievalAgent`, the four cases, `ContaminatedArchive`, `demonstrate_boundary_violation()`, `lab_threshold_report()`, `threshold_margins()`, `run_memory_retrieval()` |
| `tests/test_lab04_memory_retrieval.py` | 1292 | 63 tests |
| `notebooks/lab04_memory_retrieval.ipynb` | 45 cells (29 code) | the guided tour |
| `data/seed/data_structures/submissions.json` | 8 records | CS201 history, incl. the planted near-duplicate pair `DS-A1-S001`/`DS-A1-S003` |
| `data/seed/dbms/submissions.json` | 6 records | DBMS402 history, used by the `other-subject` case |

`memory/__init__.py` re-exports 44 names. The four modules split by concern rather
than by class, so that `stores.py` can be pointed at as *the* place a record is born.

### The boundary - both checks, one method

`memory/base.py`, `MemoryStore._check_appendable`:

```python
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
```

`append()` is the only way in, and it is where the duplicate-id check lives too -
not because a duplicate is a boundary problem, but because a duplicate would make
`fingerprint()` meaningless:

```python
def append(self, record: MemoryRecord) -> MemoryRecord:
    self._check_appendable(record)
    if record.record_id in self._records:
        raise MemoryError(
            f"record {record.record_id!r} is already in this "
            f"{self.layer.value} store; ids must be unique so the "
            "fingerprint is stable"
        )
    self._records[record.record_id] = record
    ...
```

`allowed_provenance` defaults to the sentinel only, so `RunState` needs no
override and the archive is the one that has to opt in to real paths:

```python
@property
def allowed_provenance(self) -> frozenset[str]:
    return frozenset({RUN_STATE_PROVENANCE})
```

`ArchiveStore` overrides it with the files `load_from_tools()` actually opened, so
a record claiming `data/seed/dbms/notes.json` is admissible for `DBMS402` and not
for `CS201`.
### The flag cannot express a verdict

`memory/retrieval.py`. The forbidden set is spelled out rather than implied:

```python
PLAGIARISM_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "is_plagiaristic", "is_plagiarism", "plagiarised", "plagiarized",
        "verdict", "determination", "decision", "finding", "guilt",
        "penalty", "penalty_applied", "mark_deduction", "deducted_marks",
        "score_applied", "plagiarism_score", "similarity_percentage_match",
        "rejected", "auto_rejected", "confirm_plagiarism",
    }
)

#: The two routing outcomes. A third value meaning "plagiaristic" is what the
#: document forbids, so there are exactly two and the type says so.
Disposition = Literal["review_required", "within_threshold"]
```

`SimilarityFlag`'s fourteen fields, measured:

```
authority, candidate_id, candidate_layer, compared_against, decided_by,
disposition, exceeds_threshold, rationale, requires_human_review, reviewed_by,
score_source, threshold, top_match_id, top_score
```

No overlap with the 19 forbidden names, and the CLI prints both lists so the claim
is checkable by eye:

```
SimilarityFlag fields: authority, candidate_id, candidate_layer, compared_against,
decided_by, disposition, exceeds_threshold, rationale, requires_human_review,
reviewed_by, score_source, threshold, top_match_id, top_score

none of 19 forbidden field names appear on the type: confirmed absent
```

Three design decisions inside that field list are worth naming:

* `reviewed_by` **exists and is always empty**. Doc §2.12 wants an audit of "every
  grade published and by whom approved"; the field the reviewer will eventually
  fill in has to be there from the start, starting empty because nobody has
  reviewed anything yet. The CLI asserts it: *"nothing has been reviewed yet, and
  the record says so"*.
* `authority` is the constant `course_faculty`, not a parameter - a caller cannot
  redirect the authority to an agent.
* `compared_against` is the **full** ranked list, not just the top hit, so a human
  reviewing the flag can see what else was in the running.

`raise_flag()` will not score an archive record at all:

```python
if candidate.layer is not MemoryLayer.RUN_STATE:
    raise MemoryBoundaryError(
        f"cannot raise a similarity flag about {candidate.record_id!r}: it "
        f"is in the {candidate.layer.value!r} layer, not run state, so it "
        f"is not a draft. "
        f"{LAYER_NOTE}"
    )
```

This is worth pausing on, because it catches a mistake the rest of the lab makes
easy to make. `resolve_candidate()` returns `source.model_copy(update={"record_id":
f"copy-of-{source_id}"})` for the verbatim case - an **archive** record with a new
id. Handing that straight to `raise_flag()` is refused, correctly: a flag about an
archived submission would be the archive reporting on itself, which is the same
corruption as the pitfall arriving through the front door. The agent avoids it by
taking only the `.content` and letting `RunState.set_draft()` create a genuine
short-term record. (This was found while writing the notebook, not by a test - see
section f.)

### The threshold is a property of the scorer

`memory/embeddings.py`. Not a module constant consulted by a retriever - each
embedder answers for itself:

```python
@property
def triage_threshold(self) -> float:
    """The score at or above which this backend routes a candidate to review."""
```

which is why the tests assert both that `threshold` is not a module global and
that `raise_flag` copies `result.threshold_used` rather than defaulting to a
constant. The second one matters: a flag that recorded the module default instead
of the number actually used would report a threshold nobody applied.

### `threshold_margins()` - the number a "4 of 4 correct" table hides

`labs/lab04_memory_retrieval.py`. A threshold that clears the bar by 0.0001 is not
a threshold you can trust after the next data edit, and a table that only prints
"4 of 4" invites the reader to assume otherwise:

```python
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
```

with a verdict banded on the tightest margin: under 0.005 the report says the
backend separates "far more by luck than by margin", under 0.02 "usable for triage,
not for a determination", and above that "comfortable for triage, and still not a
determination". The last clause is unconditional.

### The pitfall, as a class

`ContaminatedArchive` overrides `_check_appendable` - the only method that enforces
either invariant - so one override defeats both. That is not a shortcut; it is the
base class's own architecture being used against itself, which is why the class can
demonstrate the real failure instead of a staged one:

```python
def _check_appendable(self, record: MemoryRecord) -> None:
    """Accept anything, which is the whole point.

    The real implementation runs two checks -- layer and provenance -- and
    both are dropped here. A run-state draft's ``run_state:draft``
    provenance is therefore admitted, which is the one record kind that
    must never reach the archive.
    """
    if not self._warned:
        # Warn once, not once per record. This store loads the whole
        # genuine archive before it contaminates anything, and 18 identical
        # warnings would bury the report they are meant to interrupt.
        self._warned = True
        logger.warning(...)
```

The warning fires **once** for the store, not once per record, for the reason in
the comment: the store loads all 18 genuine archive records on the way in, and 18
identical warnings would bury the report they exist to interrupt.

Two tests keep the demonstration from leaking. An AST scan asserts
`_check_appendable` is overridden in exactly one place in the project, and a second
asserts the name `ContaminatedArchive` appears in no other module's code.
---

## d) Setup and run

### Environment (already provisioned)

```powershell
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
```

No new dependencies for the default path. Lab 4 uses `pydantic==2.13.5` plus the
standard library, and the default `hashing` backend needs no download at all. The
learned backend is already installed:

```powershell
& $py -c "from importlib.metadata import version; print(version('fastembed'))"
# 0.8.1
```

### Run the lab, default backend

```powershell
& $py -m course_organizer.labs.lab04_memory_retrieval
```

Expected: seven sections, 25/25 checks `[PASS]`, exit code **0**.

```
==============================================================================
7. Acceptance
==============================================================================

  [PASS] new-answer: every candidate carries the requested tags         1 hit(s) of 18 scanned
  [PASS] new-answer: similar prior items returned                       top DS-A1-S001 at 0.9785
  [PASS] new-answer: routed to within_threshold                         within_threshold
  [PASS] verbatim-copy: every candidate carries the requested tags      1 hit(s) of 18 scanned
  [PASS] verbatim-copy: similar prior items returned                    top DS-A1-S001 at 0.9996
  [PASS] verbatim-copy: routed to review_required                       review_required
  [PASS] restatement: every candidate carries the requested tags        1 hit(s) of 18 scanned
  [PASS] restatement: similar prior items returned                      top DS-A1-S002 at 0.9799
  [PASS] restatement: routed to within_threshold                        within_threshold
  [PASS] other-subject: every candidate carries the requested tags      1 hit(s) of 13 scanned
  [PASS] other-subject: similar prior items returned                    top DB-A2-S003 at 0.9903
  [PASS] other-subject: routed to within_threshold                      within_threshold
  [PASS] no case changed the archive's content fingerprint              4 case(s)
  [PASS] no run-state draft is in any archive                           the short-term half stayed short-term
  [PASS] no retrieval read the run state                                archive
  [PASS] no archive contains a foreign-layer record                     baseline 9ea328fcba23de18 -> 6fd02fd9ef4fe30d
  [PASS] the similarity flag has no verdict or penalty field            none present
  [PASS] no bundle produced a plagiarism determination                   2.4 node 4: flag for human review, never determine
  [PASS] every flagged case names the human authority                   course_faculty
  [PASS] nothing has been reviewed yet, and the record says so          reviewed_by is empty on every flag
  [PASS] writing a draft into the archive is refused                    layer=True provenance=True
  [PASS] the relabelled attempt is refused by provenance too            two independent checks, not one check twice
  [PASS] the contamination demonstrably changes the baseline            9ea328fcba23de18 -> 6fd02fd9ef4fe30d
  [PASS] the triage threshold separates the planted copy from the rest  4 of 4 case(s), tightest margin +0.0046 on verbatim-copy
  [PASS] every similarity hit came from the submission history only     11 distinct prior item(s) across 4 case(s)

OVERALL: PASS  (25 checks, 4 cases, 266.0ms)
```

Wall clock for the whole run on this machine is ~0.27 s: an 18-record CS201 archive
and a 13-record DBMS402 one, 14 retrieval calls across the four cases (2 + 4 + 6 +
2), and 11 distinct prior items returned.

### Run it with the learned backend

```powershell
# Suppress the HF symlink warning on Windows without Developer Mode.
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
& $py -m course_organizer.labs.lab04_memory_retrieval --embedding-backend fastembed
```

Same 25 checks, exit code **0**. The model is `BAAI/bge-small-en-v1.5` (384-dim);
the first run downloads ~69 MB and takes ~30 s to load, after which the run itself
is ~3.9 s. Both full CLI transcripts are kept in `docs/evidence/`.

### All the flags

```
  --embedding-backend {hashing,fastembed}
                        Which embedding backend to use. Defaults to
                        EMBEDDING_BACKEND, or 'hashing' when unset.
  --run-id RUN_ID       Run id for the ledgers. Defaults to lab04-<timestamp>.
  --log-level LOG_LEVEL
                        Python logging level. Defaults to WARNING to keep the
                        report readable.
```

`--embedding-backend` overrides the `EMBEDDING_BACKEND` env var, which is declared
in `config.Settings` and documented in `.env.example`. An unrecognised backend name
is **refused, not defaulted** - `build_embedder()` raises rather than silently
falling back, because a silent fallback to the weaker scorer is exactly the kind of
thing that makes a reported threshold meaningless.

### Run the notebook

```powershell
& $py -m jupyter lab notebooks\lab04_memory_retrieval.ipynb
```

### Run the tests

```powershell
& $py -m pytest tests\test_lab04_memory_retrieval.py -o addopts= -v   # 63 tests
& $py -m pytest                                                       # 267 passed, 1 skipped
```

> `pyproject.toml` sets `addopts = "-q --strict-markers"`. An extra `-q` makes it
> `-qq` and suppresses the summary line. Use bare `python -m pytest`, or
> `-o addopts= -v` for the full verbose list.

The one skip is `test_the_learned_backend_separates_further_than_the_hermetic_one`,
which is skipped unless `fastembed` and the model are both importable. The test
suite pins `EMBEDDING_BACKEND=hashing` in `conftest.py` so the suite is hermetic and
fast; the learned backend is exercised by the CLI run and by the notebook instead.
---

## e) Test cases, expected output, and proof

### The four cases, and what each is expected to produce

Every row below is a real measured run, not a prediction. "Expected" is fixed by the
case's construction - a planted copy **must** flag and a genuine new answer **must
not** - and both backends are held to it.

| case | course | candidate | expected disposition | hashing | fastembed |
|---|---|---|---|---|---|
| `new-answer` | CS201 | new linked-list answer | `within_threshold` | 0.9785 vs `DS-A1-S001` | 0.8821 vs `DS-A1-S001` |
| `verbatim-copy` | CS201 | byte-identical copy of `DS-A1-S001` | **`review_required`** | **0.9996** vs `DS-A1-S001` | **0.9881** vs `DS-A1-S001` |
| `restatement` | CS201 | `DS-A1-S002` reworded and reordered | `within_threshold` | 0.9799 vs `DS-A1-S002` | 0.7916 vs `DS-A1-S003` |
| `other-subject` | DBMS402 | new normalisation answer | `within_threshold` | 0.9903 vs `DB-A2-S003` | 0.9445 vs `DB-A2-S003` |

The verbatim case, verbatim from the CLI:

```
case verbatim-copy  [CS201]  run lab04-hashing/CS201
  candidate    : draft-22c22d66 (verbatim, run_state, 723 chars)
  hits         : 1 of 18 scanned, tags=['linked-list', 'unit:1'] match=all correct=True
  prior        : 5 of 8 compared, k=5 embedder=hashing-3-5-256d thr=0.995
  flag         : review_required: candidate draft-22c22d66 vs DS-A1-S001 score=0.9996 threshold=0.9950 review=True by=similarity_retriever
  seeds        : no-repeat seed: draft-22c22d66 vs DS-A1-S001 at 0.9996 over 8 prior item(s); flagged=True
                 similarity check seed: score 0.9996 attached to draft-22c22d66 from hashing-3-5-256d over 5 record(s); determination deferred to plagiarism_similarity_node, authority course_faculty
  archive      : 9ea328fcba23de18 -> 9ea328fcba23de18 unchanged=True draft_in_archive=False
  retrievals   : 4 call(s) over ['archive']
  elapsed      : 18.98ms

    tag hits (retrieve(store, tags)):
       1. ds-u1-linked-lists@v2      score=1.00  carries 2/2 requested tag(s): ['linked-list', 'unit:1']
    similar prior (retrieve(history, embed(candidate), k=5)):
       1. DS-A1-S001                 cos=0.9996  submission
       2. DS-A1-S003                 cos=0.9985  submission
       3. DS-A1-S002                 cos=0.9798  submission
       4. DS-A1-S004                 cos=0.9789  submission
       5. DS-A2-S005                 cos=0.9754  submission
```

Note the seed line: `determination deferred to plagiarism_similarity_node, authority
course_faculty`. The seed reports a score and names who decides. It does not decide.

The other three cases are listed in full in `docs/evidence/lab04_cli_hashing.txt`
and `lab04_cli_fastembed.txt`.

### The pitfall, expected to *succeed* at corrupting

The one check that is satisfied by the system misbehaving:

```
violation: CS201: draft written into the archive
  doc      : Don't conflate run state with the archive - mixing them corrupts the baseline.
  attempted: archive.append(draft-8629f647) -- a run_state record with provenance 'run_state:draft'
  REFUSED  : layer check=True provenance check=True
             cannot store a run_state record in a store holding archive records.
             ...
  [after relabelling the layer] record 'draft-8629f647' claims provenance 'run_state:draft', which is not a source this archive store reads from. Allowed: ['data/seed/data_structures/assignments.json', 'data/seed/data_structures/notes.json', 'data/seed/data_structures/submissions.json']
  baseline : 9ea328fcba23de18 -> 6fd02fd9ef4fe30d corrupted=True
  self-hit : DS-A1-S001 at 0.9996 (the draft found itself)
```

Both refusal lines are the acceptance criterion, and the corrupted baseline is the
consequence.

### The suite

**63 tests, 62 passed, 1 skipped, 3.20 s.** Full transcript:
`docs/evidence/lab04_pytest.txt`.

```
platform win32 -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0
collected 63 items

tests/test_lab04_memory_retrieval.py::test_the_layer_check_refuses_a_draft_in_the_archive PASSED [ 15%]
tests/test_lab04_memory_retrieval.py::test_the_provenance_check_refuses_a_relabelled_draft PASSED [ 17%]
tests/test_lab04_memory_retrieval.py::test_a_forged_provenance_is_still_refused PASSED [ 19%]
tests/test_lab04_memory_retrieval.py::test_run_state_store_has_no_way_to_reach_any_other_store PASSED [ 22%]
tests/test_lab04_memory_retrieval.py::test_fingerprint_is_stable_and_content_addressed PASSED [ 26%]
tests/test_lab04_memory_retrieval.py::test_tag_retrieval_returns_only_correctly_tagged_records PASSED [ 31%]
tests/test_lab04_memory_retrieval.py::test_similarity_retrieval_is_restricted_to_the_history PASSED [ 38%]
tests/test_lab04_memory_retrieval.py::test_the_notes_really_would_have_answered_the_history_question PASSED [ 39%]
tests/test_lab04_memory_retrieval.py::test_the_threshold_is_a_property_of_the_embedder_not_a_module_global PASSED [ 55%]
tests/test_lab04_memory_retrieval.py::test_each_threshold_is_the_midpoint_of_its_measured_gap PASSED [ 57%]
tests/test_lab04_memory_retrieval.py::test_contamination_makes_the_system_flag_itself PASSED [ 74%]
tests/test_lab04_memory_retrieval.py::test_similarity_flag_has_no_verdict_or_penalty_field PASSED [ 79%]
tests/test_lab04_memory_retrieval.py::test_the_flag_will_not_score_an_archive_layer_candidate PASSED [ 84%]
tests/test_lab04_memory_retrieval.py::test_nothing_writes_to_the_archive_during_a_run PASSED [ 88%]
tests/test_lab04_memory_retrieval.py::test_a_genuine_draft_is_below_the_threshold_on_the_default_backend[other-subject] PASSED [ 98%]
tests/test_lab04_memory_retrieval.py::test_the_learned_backend_separates_further_than_the_hermetic_one SKIPPED [100%]

======================== 62 passed, 1 skipped in 3.20s ========================
```

### The tests that matter, and what each would have caught

Grouped by what they protect, because "63 tests" is not a claim about quality.

**The two boundary checks are genuinely independent** - the core of R1/R5:

| test | what it proves |
|---|---|
| `test_the_layer_check_refuses_a_draft_in_the_archive` | the obvious attempt fails |
| `test_the_provenance_check_refuses_a_relabelled_draft` | a layer-only check would **pass** this record. This is the test that justifies having two checks |
| `test_a_forged_provenance_is_still_refused` | a path that exists on disk but belongs to another course is still refused, so the check is not "does it look like a path" |
| `test_run_state_store_refuses_an_archive_record` | the boundary holds in the other direction too |
| `test_run_state_store_has_no_way_to_reach_any_other_store` | asserted by **absence** of any method, not by calling one |
| `test_run_state_cannot_be_given_an_archive_provenance` | the sentinel is not a path, so a draft cannot claim a file |
| `test_both_boundary_checks_fire_in_order` | layer is reported first, provenance second - so the message tells the caller which one it hit |

**The demonstration cannot leak into production:**

| test | what it proves |
|---|---|
| `test_contaminated_archive_is_the_only_boundary_override` | AST scan: `_check_appendable` is overridden in exactly one place in the project |
| `test_contaminated_archive_never_appears_in_production_code` | the name appears in no other module's code |
| `test_nothing_writes_to_the_archive_during_a_run` | the normal retrieval path never appends |

**The threshold is calibrated, not asserted:**

| test | what it proves |
|---|---|
| `test_each_threshold_is_the_midpoint_of_its_measured_gap` | re-derives both constants from the measured rows every run, and asserts the hashing gap is still `0.9996 - 0.9903`. If a library bump moves it, this fails loudly rather than leaving a stale number in a docstring |
| `test_threshold_margins_report_the_headroom_honestly` | the report says "determination" on every backend |
| `test_the_threshold_report_measures_the_acceptance_cases` | the table and the acceptance checks cannot measure different things |
| `test_the_threshold_is_a_property_of_the_embedder_not_a_module_global` | swapping the embedder swaps the threshold |
| `test_the_threshold_on_the_flag_is_the_flag_scorers_not_a_module_default` | a flag records the threshold actually used, not a constant |
| `test_a_genuine_draft_is_below_the_threshold_on_the_default_backend[...]` | parametrized over all three non-copy cases |

**Retrieval is the acceptance criterion, tested as one:**

| test | what it proves |
|---|---|
| `test_tag_retrieval_returns_only_correctly_tagged_records` | every hit carries every requested tag - R3's first half |
| `test_tag_match_all_is_a_subset_of_tag_match_any` | the two match modes are consistent |
| `test_current_only_excludes_superseded_note_versions` | only the authoritative version is retrievable |
| `test_an_empty_tag_query_is_rejected_rather_than_matching_everything` | a query that would match everything is refused, not honoured |
| `test_similarity_retrieval_is_restricted_to_the_history` | R2's "history of past assignment submissions" holds |
| `test_the_notes_really_would_have_answered_the_history_question` | the restriction is not a formality: without it the top hit **is** a lecture note. Measured, under the default backend, `ds-u1-linked-lists@v2` at **0.9829** versus `DS-A1-S001` at **0.9785** |
| `test_a_verbatim_copy_scores_higher_than_a_restatement` | the ranking has the right order |
| `test_retrieval_is_course_scoped` | CS201 retrieval cannot see DBMS402 |
| `test_a_retriever_wired_to_the_wrong_layer_is_refused` / `test_invoking_an_archive_retriever_against_run_state_is_refused` | the wiring is checked, not assumed |
| `test_a_failing_retriever_is_recorded_as_a_failure_not_raised` | one broken retriever does not lose the other rows of the audit |

**The flag cannot overstate:**

| test | what it proves |
|---|---|
| `test_similarity_flag_has_no_verdict_or_penalty_field` | the 19 forbidden names are absent from the type |
| `test_the_flag_is_a_routing_decision_not_a_finding` | `requires_human_review` equals `exceeds_threshold`; `reviewed_by` empty; `authority` is the constant |
| `test_the_seeds_never_make_a_determination` | both seed types' methods always return `False` |

### Real bugs these tests and the notebook found

Twenty product bugs, listed in full in section f. The ones that would have shipped:

* `MemoryBoundaryError` was not imported in `retrieval.py`, so `raise_flag`'s own
  guard raised `NameError` instead of the boundary error it promised.
* `Embedder.embed()` iterated a bare `str` character-by-character, producing one
  directionless vector instead of one document's.
* `SimilarityQuery.kinds` defaulted to `[]` = no filter, so the "history" retriever
  returned the course's own lecture notes as a student's nearest prior work.
* `raise_flag` defaulted its `threshold` argument to the module constant rather than
  `result.threshold_used`.
* `ContaminatedArchive` warned once per record - 18 identical lines, buried under the
  report they were meant to interrupt.

### Proof captured

| file | what it is |
|---|---|
| `docs/evidence/lab04_pytest.txt` | 267 passed, 1 skipped, verbose - the whole suite, not just Lab 4 |
| `docs/evidence/lab04_cli_hashing.txt` | full CLI transcript, default backend, exit 0 |
| `docs/evidence/lab04_cli_fastembed.txt` | full CLI transcript, learned backend, exit 0 |
| `docs/evidence/lab04_notebook_output.txt` | all 45 notebook cells with outputs, ~40 KB, 0 error outputs |

The notebook was executed with `nbconvert --execute --inplace`, so the outputs in
`notebooks/lab04_memory_retrieval.ipynb` are real, and every one of the 29 code cells
produced output.
---

## f) Common errors, and the bugs found while building this

### Errors you will hit, and what they mean

**`MemoryBoundaryError: cannot store a run_state record in a store holding archive records.`**
This is R1 working. Call `run_state.set_draft(...)` and pass the archive a record
built by `draft_to_record(...).model_copy(update={"layer": ARCHIVE, "provenance":
<an allowed path>})`. There is no supported way to put a draft in the archive,
which is the point.

**`MemoryBoundaryError: record 'draft-8629f647' claims provenance 'run_state:draft', which is not a source this archive store reads from. Allowed: [...]`**
You relabelled the record and the layer check let it through. This is the second
check catching what the first could not - the exact case that justifies two checks.
The fix is the same as above: give it a real provenance, or do not archive it.

**`MemoryBoundaryError: cannot raise a similarity flag about 'copy-of-DS-A1-S001': it is in the 'archive' layer, not run state, so it is not a draft.`**
The subtlest one, and the one this lab's own author hit while writing the notebook.
`resolve_candidate()` returns an **archive** record with a new id, so it is the right
*text* but the wrong *layer* to flag. Take `.content` from it and create a real
draft:

```python
source_text = resolve_candidate(archive, case).content   # archive record: text only
run_state.set_draft(source_text, title="verbatim-copy candidate")
flag = raise_flag(run_state.draft(), result)             # run-state record: flaggable
```

**`MemoryError: record 'X' is already in this archive store; ids must be unique so the fingerprint is stable`**
Raised by `append()`, not by the boundary check. A duplicate would make `fingerprint()`
depend on insertion order, which would make the "baseline" meaningless.

**`EmbeddingError: text of 2 characters is shorter than the 3-character n-gram window; nothing to hash`**
The hashing backend needs 3 characters. It raises rather than returning a
zero vector, because a zero vector has no direction and would silently score 0
against everything.

**`EmbeddingError: unknown embedding backend 'nope'; expected 'hashing' or 'fastembed'`**
Deliberately a refusal rather than a default. A silent fallback to the weaker scorer
would make every reported threshold a lie.

**`EmbeddingError: cannot compare vectors of different widths: 2 and 3`**
`cosine_similarity()` refuses mixed widths. In practice this means one process is
holding vectors from two different backends.

**`TypeError: build_default_memory_registry() missing 1 required keyword-only argument: 'course_code'`**
`course_code` is required, not defaulted. The registry is course-scoped by
construction rather than by a caller remembering to pass one.

**`TypeError` / "you must call `.unwrap()`"**
`registry.invoke()` returns a `RetrievalResult`, never a bare value, so a raising
retriever is recorded in the ledger instead of aborting the run. Chain `.unwrap()`
to get the value.

**`ModuleNotFoundError: No module named 'fastembed'`, or a 30 s hang then a download error**
The learned backend needs `fastembed==0.8.1` and a one-time ~69 MB download of
`BAAI/bge-small-en-v1.5`. On Windows without Developer Mode, set
`HF_HUB_DISABLE_SYMLINKS_WARNING=1`. The hashing backend needs none of this and is
the default precisely so these acceptance criteria are re-checkable offline.

### The development bug log - 21 real defects, all fixed

Found by writing the tests, not by reading the code afterwards. Each is now either
covered by a test or by an assertion in the code.

| # | defect | why it mattered |
|---|---|---|
| 1 | `dataclasses.field` used as a Pydantic model default | the "default" was a `FieldInfo`, silently |
| 2 | `MemoryRegistry._stores` was a **class-level** mutable dict | every registry shared one store set; caught by `test_registry_state_is_per_instance_not_shared` |
| 3 | two names in `__all__` did not exist | `from ... import *` would raise |
| 4 | `append()` overwrote `record.layer` with the store's layer | made `contains_foreign_layer()` a tautology - it could never report a foreign layer |
| 5 | `ArchiveStore.describe()` iterated dict keys, not `.values()` | the summary described record *ids* as if they were records |
| 6 | `read_submissions` rejected an ISO-string `submitted_at` | the seed data is JSON, so timestamps arrive as strings |
| 7 | `FastEmbedEmbedder.dim` read `.dim`; fastembed exposes `.embedding_size` | `AttributeError` on first use of the learned backend |
| 8 | `raise_flag`'s `threshold` defaulted to the module constant | a flag could record a threshold nobody applied |
| 9 | `tag_result` needed a `model_copy` post-pass | Pydantic v2 validates on construction, not on mutation |
| 10 | an `edit` truncated `tools/seed_store.py` mid-write | Lab 2's tests caught it immediately; the module is the only filesystem boundary |
| 11 | an invalid placeholder left in `_resolve_candidate` | `NameError` at the first verbatim case |
| 12 | wrong logging import (`configure_logging`, not `setup_logging`) | `ImportError` at startup |
| 13 | `__init_subclass__` was too strict | it rejected the very class that defines `layer`; relaxed to `getattr(cls, "layer", None) is None` |
| 14 | the `ContaminatedArchive` bypass was **incomplete** | the layer check lived in `append()`, not `_check_appendable()`, so the override did not defeat it. Both invariants were moved into `_check_appendable()` for exactly this reason |
| 15 | `ContaminatedArchive` warned once per record | 18 identical lines burying the report |
| 16 | wrong keyword `store=` where the factory takes `archive=` | `TypeError` |
| 17 | **`MemoryBoundaryError` not imported in `retrieval.py`** | `raise_flag`'s own guard raised `NameError` instead of the boundary error it promises |
| 18 | **`Embedder.embed()` iterated a bare `str` character-by-character** | one directionless vector per character instead of one document |
| 19 | **`SimilarityQuery.kinds` defaulted to `[]` = no filter** | the "history" retriever returned the course's own lecture notes as the student's nearest prior work |
| 20 | `SimilarityFlag.summary()` formatted the threshold `.2f` | `0.995` printed as `0.99`, contradicting the threshold table two lines above |
| 21 | `raise_flag`'s refusal message said "it is a archive record" | grammar in the one message a user is most likely to read |

Defects 17-20 are the ones worth remembering, because all four were found by a test
or a notebook cell rather than by inspection, and all four would have produced a
*plausible wrong answer* rather than a crash.
---

## g) Requirement coverage

| # | Doc requirement | Where met | Evidence | Status |
|---|---|---|---|---|
| R1 | *"Separate short-term run state from long-term stored knowledge."* | `MemoryLayer`, `MemoryStore._check_appendable()` (layer + provenance) | `test_the_layer_check_refuses_a_draft_in_the_archive`, `test_the_provenance_check_refuses_a_relabelled_draft`, `test_a_forged_provenance_is_still_refused`, `test_run_state_store_refuses_an_archive_record`, `test_run_state_store_has_no_way_to_reach_any_other_store` | done |
| R2 | *"Session state (the current draft) vs stored reference - the notes/materials store and the history of past assignment submissions - retrieved by tags and by similarity."* | `RunState`, `ArchiveStore.load_from_tools()`, `retrieve_by_tags`, `retrieve_similar_prior(kinds=["submission"])` | `test_similarity_retrieval_is_restricted_to_the_history`, `test_the_notes_really_would_have_answered_the_history_question`, `test_archive_records_all_carry_a_real_file_as_provenance` | done |
| R3 | *"Retrieval over the store + history with tag filters and embedding similarity.  Accept: For a given task, retrieval returns correctly-tagged candidates and similar prior items."* | `TagQuery`, `SimilarityQuery`; acceptance checked per case in all four cases | 12 of the 25 CLI checks; `test_tag_retrieval_returns_only_correctly_tagged_records`, `test_similarity_respects_k`, `test_retrieval_is_course_scoped` | done |
| R4 | *"Nodes activated.  Retrieval node; seeds validation/no-repeat."* | `RetrievalAgent`, `NoRepeatSeed`, `SimilarityCheckSeed` | CLI section 2 prints both seeds per case; `test_the_seeds_never_make_a_determination` | done |
| R5 | *"Pitfall to avoid.  Don't conflate run state with the archive - mixing them corrupts the baseline."* | `MemoryStore.fingerprint()`, `ContaminatedArchive`, `demonstrate_boundary_violation()` | CLI section 4: `baseline : 9ea328fcba23de18 -> 6fd02fd9ef4fe30d corrupted=True`; `test_contamination_changes_the_fingerprint`, `test_contamination_makes_the_system_flag_itself`, `test_contaminated_archive_is_the_only_boundary_override` | done |
| R6 | *"state.draft = candidate  # short-term"* | `RetrievalAgent` calls `run_state.set_draft()` and passes `run_state.draft()` onward | CLI section 1 side-by-side; `test_nothing_writes_to_the_archive_during_a_run` | done |
| R7 | *"hits  = retrieve(store, tags)  # long-term"* | `retrieve_by_tags`, wired to the archive | `test_a_retriever_wired_to_the_wrong_layer_is_refused`, `test_invoking_an_archive_retriever_against_run_state_is_refused` | done |
| R8 | *"prior = retrieve(history, embed(candidate), k=5)  # similarity"* | `retrieve_similar_prior`, `k=5`, `layer_read=ARCHIVE`, kinds restricted | `test_similarity_respects_k`, `test_the_threshold_is_a_property_of_the_embedder_not_a_module_global` | done |
| R9 | §2.4 node 4 *"Flag for human review; never make a final plagiarism determination automatically."* | `SimilarityFlag` (14 fields), `PLAGIARISM_FORBIDDEN_FIELDS` (19 names), `Disposition` of exactly two values | CLI section 5 prints both lists; `test_similarity_flag_has_no_verdict_or_penalty_field`, `test_the_flag_is_a_routing_decision_not_a_finding`, `test_the_flag_will_not_score_an_archive_layer_candidate` | done |
| R10 | §2.12 *"Flag high-similarity submissions for review only; never auto-penalize."* | `reviewed_by` present and empty; `authority = course_faculty` as a constant; no penalty field | CLI check *"nothing has been reviewed yet, and the record says so"* | done |

**Deliberately deferred, and where each lands**

| limitation | why it is not in this lab | settled by |
|---|---|---|
| the similarity flag **is never reviewed** | §2.12 requires a human decision; `reviewed_by` is empty on every flag and Lab 4 has no approval gate to obtain one | Lab 7 |
| nothing is ever **written** to the archive | the archive is read-only, and that is *how* the boundary is held. A real product needs an archive write path carrying the same guards | Lab 6-7 |
| the archive is the **seed corpus**, not Google | `SOURCE_ADAPTER=local` is the default so the acceptance criteria are re-checkable offline | Lab 5 |
| the ledger is **in memory, per run** | `RetrievalLedger` exists but nothing is persisted; §2.12 wants a durable audit log | Lab 6 |
| the corpus is **not indexed** | 8-12 documents embed in milliseconds. A real deployment needs an index, which changes the latency profile entirely | — |
| triage covers **submissions only** | the document says "history". Searching lecture notes too is a legitimate different question, but it is not a no-repeat check | — |

---

## h) Report and documentation text

The following is written for submission. It states what was built, what was
measured, and what was deliberately left out.

### Summary

Lab 4 implements the document's memory split as a package that makes the split
**checkable** rather than merely intended. Short-term run state and the long-term
archive are separate stores, and the boundary between them is enforced by two
independent checks that live in one method: a record's layer must match the store's,
*and* its provenance must be a source that store actually reads. The second check is
the one that does the real work, because a layer check alone is defeated by
`model_copy(update={"layer": ...})` - a routine operation.

The retrieval half satisfies the acceptance criterion literally: tags return
correctly-tagged candidates, and similarity returns prior items. Four cases across
two courses exercise it, of which one - a new `DBMS402` answer to the normalisation
question - turned out to be the binding constraint on the similarity threshold and
forced both thresholds to be re-derived from measurement.

The pitfall - *"Don't conflate run state with the archive - mixing them corrupts the
baseline"* - is implemented as a runnable demonstration rather than a warning.
`ContaminatedArchive` removes the single method that enforces either invariant, and
the resulting run shows the baseline fingerprint moving from `9ea328fcba23de18` to
`6fd02fd9ef4fe30d` and the no-repeat screen flagging the system's own draft at
cosine 0.9996. The reported damage is not slowness but trust: a checker that flags
itself teaches its reviewer to discount it, and every genuine flag after it is
discounted too.

Consistent with doc §2.4 node 4 and §2.12, nothing here determines plagiarism.
`SimilarityFlag` has no field in which such a verdict could be written - the 19
forbidden names are asserted absent from its 14 - `reviewed_by` exists and is empty
on every flag, and the authority is the constant `course_faculty`. The threshold
decides what a human looks at, and the CLI says so in the same line that prints it.

### What was measured

Both embedding backends were run end to end, 25/25 acceptance checks passing under
each, with exit code 0.

| case | course | hashing | fastembed | must flag |
|---|---|---|---|---|
| `verbatim-copy` | CS201 | **0.9996** | **0.9881** | yes |
| `other-subject` | DBMS402 | 0.9903 | 0.9445 | no |
| `new-answer` | CS201 | 0.9785 | 0.8821 | no |
| `restatement` | CS201 | 0.9799 | 0.7916 | no |
| **triage threshold** | | **0.9950** | **0.9663** | |
| headroom above / below | | +0.0046 / +0.0047 | +0.0218 / +0.0218 | |
| measured gap width | | 0.0093 | 0.0436 | |
| cold load | | none | ~30 s (once) | |
| whole-run wall clock | | ~0.27 s | ~3.9 s | |

Both thresholds are the midpoint of their own measured gap - one rule, applied
twice, re-derived on every run by `test_each_threshold_is_the_midpoint_of_its_measured_gap`.

The two gaps are not equally wide, and that is the most useful number in the table.
Under fastembed the copy and the hardest genuine draft are 0.0436 apart, which
survives a seed-data edit. Under hashing they are 0.0093 apart: the backend detects
*copied text* and little else, separating a verbatim copy from a new written answer
by about one percent. It is the default only because it needs no download, so a
reviewer with no credentials and no network can re-check every acceptance criterion.

One discrepancy worth recording rather than smoothing over: the `restatement` case's
top match differs by backend - `DS-A1-S002` at 0.9799 under hashing, `DS-A1-S003`
at 0.7916 under fastembed. The case's text is derived from `S002`, and character
n-grams still favour it; the learned model ranks `S003` first, which is the planted
near-duplicate of `S001`. Both are correct answers to "what is nearest", and the
difference is exactly why the two backends cannot share one threshold.

### Honest limitations

* **Nothing here has been reviewed by a human.** Every flag is un-reviewed and says
  so. Lab 4 has no approval gate.
* **The archive is read-only.** That is how the boundary is demonstrated, but a real
  product needs a write path with the same guards.
* **The corpus is 18 records.** Embedding is recomputed per retrieval rather than
  indexed, which is fine at this size and wrong at any other.
* **The archive is the seed corpus, not Google Workspace.** `SOURCE_ADAPTER=local`
  stays the default for reproducibility; Lab 5 is the connector that changes this.
* **21 product defects were found and fixed while building this**, four of them
  found by a test or a notebook cell rather than by inspection - a missing import
  that turned the boundary guard into a `NameError`, an `embed()` that iterated a
  string character-by-character, a `kinds` default that returned lecture notes as a
  student's prior work, and a `.2f` format that printed a `0.995` threshold as
  `0.99`. All are listed in section f.

### Reproducing this

```powershell
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"

& $py -m course_organizer.labs.lab04_memory_retrieval
& $py -m pytest tests\test_lab04_memory_retrieval.py -o addopts= -v
& $py -m jupyter lab notebooks\lab04_memory_retrieval.ipynb
```

With the learned backend, and a ~69 MB one-time download:

```powershell
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
& $py -m course_organizer.labs.lab04_memory_retrieval --embedding-backend fastembed
```