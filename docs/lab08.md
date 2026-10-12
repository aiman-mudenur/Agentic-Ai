# Lab 8 — Parallel Swarm: fan out the work, merge by plan alignment

**Phase:** B — Orchestration
**Goal (doc §2.10, Lab 8):** *"Parallelise the per-unit work and select the best."*
**What it adds (doc §2.10):** *"Several worker agents process assignment submissions in parallel; a merge/selector picks the best per the plan - cutting latency and improving quality."*
**Inputs (doc §2.10):** *"Locked plan; per-unit work items."*
**Outputs (doc §2.10):** *"Best-of-N results merged into one compliant output."*
**Agents (doc §2.10):** *"Parallel workers + a Merge selector under the Coordinator."*
**Deliverable (doc §2.10):** *"A fan-out/fan-in that processes all units and merges by alignment to the plan. Accept: Parallel run matches or beats sequential quality at lower wall-clock time."*
**Pattern (doc §2.10):** *"Parallel worker fan-out + merge."*
**Pitfall (doc §2.10):** *"Merge strictly by plan alignment; picking the 'nicest' output quietly breaks coverage."*
**Pseudocode (doc §2.10):** `results = parallel_map(process_assignment, plan.units)` / `output = merge(results, key=alignment_to(plan))`

---

## a) Goal and the document's requirements

The document's requirements for Lab 8 are on lines 699–711 of the extracted
Worklet 2 text, and the chapter it belongs to (chapter 2, lines 449–860)
supplies four more constraints the lab inherits. Each is quoted here verbatim
and traced to the code that satisfies it in §g.

| # | Line | Requirement, as written |
|---|------|--------------------------|
| R1 | 699 | *"Lab 8 - Parallel Swarm - many assignment submissions, then merge"* |
| R2 | 700 | *"Parallelise the per-unit work and select the best."* |
| R3 | 701 | *"Several worker agents process assignment submissions in parallel; a merge/selector picks the best per the plan - cutting latency and improving quality."* |
| R4 | 704 (inputs) | *"Locked plan; per-unit work items."* |
| R5 | 704 (outputs) | *"Best-of-N results merged into one compliant output."* |
| R6 | 704 (agents) | *"Parallel workers + a Merge selector under the Coordinator."* |
| R7 | 704 (deliverable) | *"A fan-out/fan-in that processes all units and merges by alignment to the plan. Accept: Parallel run matches or beats sequential quality at lower wall-clock time."* |
| R8 | 706 | *"Parallel worker fan-out + merge."* |
| R9 | 707 | *"Merge strictly by plan alignment; picking the 'nicest' output quietly breaks coverage."* |
| R10 | 710 | `results = parallel_map(process_assignment, plan.units)` and `output = merge(results, key=alignment_to(plan))` |
| R11 | 573 (§2.7) | *"Candidates are calibrated and screened; the best-aligned one that satisfies the plan is selected, the rest logged."* |
| R12 | 574 (§2.7) | *"on failure only this assignment submission is regenerated."* |
| R13 | 558 (§2.7) | Doc §2.7's four validation checks, which still gate the run after the merge |
| R14 | 823 (§2.11) | *"Ungrounded output \| content not backed by the faculty-approved rubric and syllabus \| Grounded generation with an evidence record per unit; ungrounded items are rejected."* |
| R15 | 825 (§2.11) | *"Bound `MAX_ATTEMPTS`"*, then escalate to course faculty with a diagnosis |
| R16 | 830 (§2.12) | *"Never publish a grade or feedback without faculty approval."* |
| R17 | 833 (§2.12) | *"Keep clear version history for notes and materials."* |

Two of these deserve comment before any code is written, because both are
contradictions that only show up when you try to implement them literally.

**R1/R3/R4/R7 disagree about what a work item is.** R10 says
`parallel_map(process_assignment, plan.units)`; R3 says workers process
*assignment submissions*; R7 says the fan-out "processes **all units**". Read
as one thing they are contradictory, because §2.7 line 570 fixes the chapter's
unit of work as *"one assignment submission"* — the unit of work is a
submission, and `plan.units` is the document's generic name for the work list.
The resolution this lab uses is therefore **not** to pick one and discard the
other: the fan-out covers both kinds of work item, submissions first.

| kind | count on the seed corpus | candidate strategies |
|------|---------------------------|----------------------|
| `submission` | 8 (`DS-A1-S001`…`DS-A3-S008`) | `strict` / `generous` / `sparse` |
| `unit` | 5 (units 1–5) | `complete` / `outline` / `overclaimed` |

Thirteen items × three strategies = **39 candidates per pass**. Units 2 and 4
have no assignments in the seed corpus, which is a useful accident: it is the
case where "all units" and "all assignments" would disagree, and the lab has to
process the unit anyway.

**R9 is a warning about a trap that only fires with good candidates.** The trap
is "pick the 'nicest' output". Here the nicest candidate is literally the
highest-marked one: `generous` inflates a draft by
[`GENEROUS_FACTOR = 1.10`](https://github.com/) — no, stated plainly: by 10%
(`GENEROUS_FACTOR = 1.10`). §e shows the measured outcome, and it is subtler
than the warning suggests: on most submissions the generous draft is *not* even
caught, because 10% of a well-marked submission stays under the rubric maximum.
It ties at alignment 1.0 and loses on the declared strategy order. That is a
tie-break, not a screen firing, and the lab says so rather than overclaiming
(`test_the_nicest_candidate_is_rejected_in_favour_of_the_aligned_one`).

### Where the lab's own choices are labelled as such

The document does not name these. Each is a decision, and each is a constant
somewhere in `swarm.py` so a reader can find it and disagree with it.

| Choice | Value | Why |
|--------|-------|-----|
| Work item kinds | both submissions and units | R1/R3/R4/R7 conflict; §a above |
| Priority weights | submission 2, unit 1 | R3 names submissions first, so the quality number must be able to say which kind dominated |
| Best-of-N size | 3 per item | R5 says best-of-N; 3 is the smallest N with a loser worth logging, and R11 requires the losers to be logged |
| Strategies | deterministic, not sampled | a scripted run must be reproducible, or the merge cannot be tested |
| Alignment metric | `(satisfied − violated) / total`, clamped to [0, 1] | R9/R10 say "alignment", not "a score"; a ratio that cannot be gamed by adding trivially satisfied checks |
| Tie-break | declared strategy order, then name | the fan-in must not depend on which thread finished first |
| Fan-out width | 8 | measured, not chosen; §b and §f |
| Output root | `out/lab08/` | beside Lab 7's checkpoints, not on top of them |

---

## b) Brief concepts

### Doc §2.10's pseudocode, read literally

```python
results = parallel_map(process_assignment, plan.units)
output  = merge(results, key=alignment_to(plan))
```

Three words carry the whole design: `parallel_map`, `key=`, and `plan.units`.
`parallel_map` says the *shape* (fan out, fan in). `key=` says the *selection
rule* is a total order on a per-item scalar, not a global preference. `plan.units`
says the unit of work is a **plan element**, which is what makes
`alignment_to(plan)` computable at all: you cannot align to a plan you did not
read.

### The merge recomputes the score it selects on

A candidate carries a `self_reported_alignment` so the log can show what the
producer claimed. `merge_results` never reads it. It calls
`alignment_to_plan` again, from the candidate's `content` and the locked plan's
scope:

```python
for cand in grouped[key]:
    content = cand.get("content", {}) or {}
    alignment = alignment_to_plan(item, content, scope)   # <- recomputed, not read
    scored.append((alignment.score, _strategy_rank(item, str(cand.get("strategy", ""))),
                   str(cand.get("strategy", "")), cand, alignment))
...
best = max(scored, key=lambda t: (t[0], -t[1]))
```

A selector that trusted the candidate's own score would be selecting on the
thing it exists to verify. The recomputation is the screen; the candidate's
number is a claim. `test_the_merge_recomputes_alignment_rather_than_reading_the_claim`
pins the difference by feeding a merge a candidate whose `self_reported_alignment`
is 1.0 on content that scores 0.2.

### Alignment is a ratio over a fixed obligation set, so it cannot be gamed

```python
score = max(0.0, min(1.0, (len(satisfied) - len(violated)) / total))
```

`total` is the length of a module constant — `SUBMISSION_OBLIGATIONS` (5) or
`UNIT_OBLIGATIONS` (5) — not the number of checks that happened to run. A
candidate cannot raise its own score by adding checks. Violations cost twice
what satisfactions earn, which is what makes the metric order-preserving in the
direction R9 cares about: a candidate that breaks one obligation to satisfy
three others is still behind.

### The `Send` fan-out, and where it has to live

LangGraph 1.2.12 runs a `Send` list on a thread pool and fans the results back
in. A `Send` list is legal **only** as the return value of a conditional-edge
path function; returning one from a node raises `InvalidUpdateError` (verified
against the installed library, and pinned by
`test_a_send_list_is_only_legal_from_a_conditional_edge`). So the shape is:

```
workers -> swarm_dispatch --[cond: Send(swarm_worker)]--> swarm_worker -> swarm_merge -> validate
```

with `swarm_dispatch` a plain node that only builds the work items and starts
the clock, and `fan_out` the path function that reads `swarm_items` and emits one
`Send` per `(item, strategy)`.

Two LangGraph details cost real time to find and are worth writing down:

1. `add_conditional_edges(src, fan_out)` with **no** path list renders as
   `src -> __end__` in `get_graph()`, so the `Send` edge is invisible in the
   topology dump. The path list `["swarm_worker"]` has to be given explicitly or
   the evidence does not show the fan-out at all.
2. Each `Send` payload must carry everything its node needs, because a `Send`
   target receives the *payload*, not the accumulated state. `swarm_worker`
   therefore takes the item, its base record, the strategy, the attempt number
   and the plan scope — and deliberately does not read the shared state.

### The reducer is load-bearing, not decoration

`swarm_candidates` is `Annotated[list[dict], append]`. Thirty-nine workers write
it concurrently; under LangGraph's default `LastValue` reducer they would
clobber each other and the merge would see exactly one candidate — silently, with
no error. `test_the_candidate_channel_is_an_append_channel` asserts the
annotation rather than the behaviour, because a reducer that is accidentally
removed still *works* on a single candidate and only fails at 39.

### The GIL is the whole wall-clock story, and it is why the fan-out width is measured

CPython's GIL means a thread pool does **not** speed up pure-Python CPU work.
Probed directly on this machine: a fan-out of hash-heavy tasks ran at **0.99x**
of serial. So "we parallelised it" is not an argument; the parallel pass only
wins if the per-candidate work releases the GIL, and here exactly one thing
does: the durable evidence write in `write_candidate_artifact`
(`open` → `write` → `flush` → `os.fsync`). That is not padding — R11 requires the
rejected candidates to be *logged*, and R14 names "an evidence record per unit".
The lab asserts both the timing and a structural check so neither can carry the
claim alone.

And an unbounded pool is worse than a bounded one. Measured stage speedup
against the sequential topology, median of three runs per width, on one machine,
in a plain process and inside a Jupyter kernel:

| fan-out width | plain process | in a Jupyter kernel |
| --- | --- | --- |
| 2 | 1.04x | 1.00x |
| 3 | 1.26x | 1.25x |
| 4 | 1.22x | 1.46x |
| 6 | 1.35x | 1.65x |
| **8** | **1.38x** | **1.68x** |
| 12 | 1.14x | 1.52x |
| 39 | 1.13x | 1.29x |
| unbounded (39 tasks) | 1.27x – 1.58x | 0.71x |

Two lessons, and the second is the one that mattered. A narrow fan-out is not
better than none, because then nothing overlaps. And an unbounded one is a coin
toss: it won in a plain process and *lost reproducibly* in a Jupyter kernel,
because each task's LangGraph bookkeeping is GIL-serialised. `SWARM_MAX_CONCURRENCY
= 8` is the width that won in both, so R7's acceptance criterion holds
everywhere instead of sometimes. §f records this as a real defect in the first
implementation.

The bound has a second, better property: **it is checkable without a stopwatch.**
`max_overlap` counts worker intervals that were in flight at the same instant, so
it cannot exceed the pool width. `2 <= max_overlap <= SWARM_MAX_CONCURRENCY`
therefore proves the fan-out both overlapped *and* stayed bounded, and an
unbounded pool could not pass it.

### "Cold start" is not the swarm's fault, and comparing against it is still wrong

The first graph run in a fresh interpreter pays for the thread pool, the `Send`
code path, and the SQLite checkpoint file. Measured in the Jupyter kernel, that
premium was about **49ms** — larger than the entire parallel stage. Comparing a
cold parallel run against a warm sequential one would report a loss for reasons
that have nothing to do with parallelism. The notebook therefore warms both
topologies once and discards those numbers, and says so in the output. The lab's
own command does not need this: its cold premium is about 4ms, and
`docs/evidence/lab08_cli.txt` shows it passing the same check from cold.

---

## c) The code

### Files

| File | Lines | What it is |
|------|-------|-----------|
| `src/course_organizer/graph/swarm.py` | 1,589 | the module: work items, candidates, alignment, merge, fan-out, the two topologies, the runners |
| `src/course_organizer/labs/lab08_parallel_swarm.py` | 777 | the CLI: 4 scenarios, 27 acceptance checks, exit code 0 on pass |
| `tests/test_lab08_swarm.py` | 1,467 | 104 test cases |
| `notebooks/lab08_parallel_swarm.ipynb` | 44 cells (22 code) | the walkthrough; 22 executed, 0 errors |

`graph/swarm.py` is **additive**. Lab 7's `builder.py`, `nodes.py`, `runner.py`,
`state.py`, `validation.py` and its two test files are untouched — the swarm is a
second topology built from Lab 7's own `build_nodes(ctx)`, and its own state is a
subclass:

```python
class SwarmState(CourseState, total=False):
    """Lab 7's state plus the swarm's channels. ..."""
```

13 swarm keys on top of Lab 7's 42, for 55 in all. Inheriting rather than
editing is deliberate: a key only Lab 8 writes has no business in the contract
Lab 7's tests pin. `test_the_swarm_state_keeps_lab_sevens_keys` asserts both
halves — that all 42 of Lab 7's keys survive, and that the inherited
`Annotated` reducers come with them.

> A `TypedDict` note, because it bit twice: `SwarmState.__mro__[1]` is `dict`,
> not `CourseState`, and `__annotations__` is already **flattened** to 55 keys —
> so there is no base class to walk for inherited fields. And
> `typing.get_type_hints(SwarmState)` strips the `Annotated` reducer metadata
> unless you pass `include_extras=True`; without it the append channel is
> invisible and the test that checks it silently proves nothing.

### The state: 13 channels, two of them load-bearing

| key | reducer | why |
|-----|---------|-----|
| `swarm_mode` | last value | `parallel` or `sequential`, so the transcript can name the mode it measured |
| `swarm_items` | last value | the work items, each with the `base` record its candidates calibrate |
| `swarm_candidates` | **`append`** | 39 concurrent writers; `LastValue` would clobber them |
| `swarm_intervals` | **`append`** | one `(start, end)` per worker — the structural proof of overlap |
| `swarm_selected` | last value | the per-item winners |
| `swarm_merge_log` | last value | every candidate, its recomputed alignment, and whether it won |
| `swarm_quality` | last value | priority-weighted mean alignment of the winners, in [0, 1] |
| `swarm_quality_by_kind` | last value | the same mean per kind, so the priority weighting is visible |
| `swarm_stats` | last value | counts for the transcript |
| `swarm_t0` | last value | `perf_counter()` at dispatch, so the merge can time the stage |
| `swarm_stage_ms` | last value | set directly by the sequential node; derived from `swarm_t0` by the parallel merge |
| `swarm_timing` | last value | what the tests and the transcript read |
| `unit_coverage` | last value | the selected unit coverage records |

Lab 6's AST guard (which exists because LangGraph *silently discards* undeclared
keys) is applied to `SwarmState` too:
`test_every_swarm_state_key_is_declared_with_a_docstring`.

### The topology

`build_swarm_graph(ctx, parallel=True)` returns a graph whose edges, read from
the **compiled** graph rather than from a declared constant, are:

```
[edge] __start__ -> ingest
[edge] ingest -> plan
[edge] plan -> workers
[cond] swarm_dispatch -> swarm_worker        <- the Send fan-out
[edge] swarm_worker -> swarm_merge            <- the fan-in
[edge] swarm_merge -> validate
[cond] validate -> workers                   <- Lab 7's back-edge, intact
[cond] validate -> assemble
[cond] validate -> escalate
[edge] assemble -> gate
[cond] gate -> publish
[cond] gate -> escalate
[edge] publish -> __end__
[edge] escalate -> __end__
```

`parallel=False` compiles the same graph with `workers -> swarm_loop` instead,
where `swarm_loop` runs all 39 candidates in one node. That is the whole
difference between the two topologies, which is what makes check 13 ("parallel
and sequential select identical candidates") a real comparison rather than a
comparison of two different systems. The `Send` edge only appears in
`get_graph()` because the path list is passed explicitly; see §b.

### The work item is both kinds, and the priority is a weight

```python
def build_work_items(state: SwarmState) -> list[WorkItem]:
    ...
    submissions first (PRIORITY_SUBMISSION = 2), then units (PRIORITY_UNIT = 1)
```

Submission work is dispatched first, and the merge's quality number is
priority-weighted:

```python
weight_total += item.priority
weight_score  += item.priority * winner_alignment.score
quality = round(weight_score / weight_total, 6) if weight_total else 0.0
```

so on the seed corpus the denominator is `8*2 + 5*1 = 21` and the two kinds
contribute 16 and 5 of it. `quality_by_kind` reports them separately, because a
single number that hides a kind losing is the "quietly breaks coverage" failure
in a different costume.

### The screens, restated as checkable obligations

`SUBMISSION_OBLIGATIONS` and `UNIT_OBLIGATIONS` are tuples of names, each with
the doc line that earns it. `align_submission` and `align_unit` turn a
candidate's content into a pass/fail per obligation:

```python
checks = [
    ("within_rubric_maximum",
     float(content.get("awarded_marks", 0.0)) <= float(content.get("max_marks", 0.0)) + RUBRIC_TOLERANCE),
    ("similarity_attached", content.get("similarity_score") is not None),
    ("late_accurate", bool(content.get("late")) == bool(content.get("expected_late"))),
    ("criteria_complete", criteria_present == criteria_needed),
    ("assignment_in_plan", (not plan_assignments) or str(content.get("assignment_id", "")) in plan_assignments),
]
```

`no_overclaim` is the R14 screen, and it is worth naming how it is *not* written.
It is not "does the text mention anything absent from the corpus" — that would
need a model. It is `claimed_a <= expected_a and claimed_s <= expected_s`: the
`overclaimed` strategy claims an assignment the unit does not have, and when the
unit has no real foreign assignment to borrow it claims a named sentinel,
`GHOST_999 = "GHOST-999"`, so a reader can see it is a sentinel and not a data
value.

> A confirmed non-defect, recorded because it looks like one. When
> `fail_until_attempt` is injecting a defect, `similarity_scores` comes back
> `{}` and every candidate's `similarity_score` is `None` — so
> `similarity_attached` fails for *all three* strategies and they all score 0.2.
> That is correct, not broken: `graph/specialists.py` deliberately skips scoring
> while it is injecting a defect, so that the injected failure and a genuine
> missing score are the same observable. The swarm inherits that behaviour
> rather than working around it.

### The merge: strict, deterministic, and logged in full

```python
best = max(scored, key=lambda t: (t[0], -t[1]))   # score desc, declared order asc
```

`_strategy_rank` maps a strategy to its position in the kind's declared tuple, so
`strict` (0) beats `generous` (1) beats `sparse` (2) at equal alignment. Every
candidate produces a `swarm_merge_log` record whether it won or lost, and an
item with no candidate at all is logged as a decision with a null winner rather
than skipped — so "all units processed" is checkable rather than assumed. That is
R11's *"the rest logged"* and R7's *"all units"* in one decision.

### The worker's durable evidence record

```python
def write_candidate_artifact(output_dir, attempt, candidate) -> str:
    path = _candidate_path(output_dir, attempt, str(candidate.get("key", "item")),
                           str(candidate.get("strategy", "strategy")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())      # durable, not just flushed
    return str(path)
```

Two reasons, both load-bearing. It satisfies R11/R14's logging requirement, and
because it is written *from the worker as the candidate is produced*, a fan-out
that dies half way leaves the candidates it had already produced. It is also the
GIL-releasing work that makes the parallel pass win — which is why Lab 5's
durability-containment tests were extended rather than bypassed
(`_EXPECTED_IO_CALLS["graph/swarm.py"] = 1`).

### The bounded fan-out, and why it is an adapter

`GraphRun.start` builds its own config from `graph_config(...)`, and Lab 7's
runner is not Lab 8's to edit. So the bound is applied by a small named adapter
rather than by a patch:

```python
class _BoundedFanOut:
    def invoke(self, *args, **kwargs):
        if len(args) > 1:
            config = args[1]
            args = (args[0], {**(config or {}), "max_concurrency": self._width}) + args[2:]
        else:
            config = kwargs.pop("config", None)
            kwargs["config"] = {**(config or {}), "max_concurrency": self._width}
        return self._graph.invoke(*args, **kwargs)

    def __getattr__(self, name):        # get_graph(), get_state(), ... stay the graph's
        return getattr(self._graph, name)
```

That `max_concurrency` really is the pool width was **read out of the installed
library**, not assumed: `langgraph.pregel._executor.BackgroundExecutor` delegates
to `langchain_core.runnables.config.get_executor_for_config`, which builds
`ContextThreadPoolExecutor(max_workers=config.get("max_concurrency"))`.
`resume_swarm` needs no adapter because it builds its config itself and merges the
key directly.

### The two runners

`run_swarm(...)` and `resume_swarm(run_id, decision)` mirror Lab 7's
`run_pipeline` / `resume_run`, with `parallel` and `max_concurrency` added. They
open the same `open_checkpointer` and write to `out/lab08/<run_id>/` — beside
Lab 7's checkpoints rather than on top of them (`SWARM_OUTPUT_ROOT = "lab08"`),
so a Lab 7 run is still reproducible after a Lab 8 run.

---

## d) Setup and run

### Environment (already provisioned)

```
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
py -3.14 -m venv C:\Users\ummea\.virtualenvs\course-organizer
& C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe -m pip install -e .
```

Lab 8 adds **no** dependency. It uses `langgraph.types.Send`, which Lab 7 already
required. No API key, no network: `LLM_BACKEND=scripted` is the default.

### Run the lab

```
python -m course_organizer.labs.lab08_parallel_swarm
# OVERALL: PASS (27/27 checks)      <- exit code 0
```

The transcript is captured at [`docs/evidence/lab08_cli.txt`](evidence/lab08_cli.txt)
(ASCII, UTF-8 without a BOM). Four scenarios run in order:

| scenario | what it does |
|----------|--------------|
| topologies | compiles both graphs, dumps the real edge sets, asserts the `Send` edge and the fan-in edge exist and that Lab 7's back-edge survives |
| quality | one parallel run, one sequential run, then the alignment tables for every candidate and the "nicest loses" demonstration |
| gate | approves a suspended run with a real `Command(resume=...)` from a **second process** and reads the published document |
| exhaustion | injects a defect that never converges, so `MAX_ATTEMPTS` fires, and checks the escalation carries a diagnosis and the failing draft |

### The 27 acceptance checks

Grouped as the lab prints them. Full text and per-check detail in
`docs/evidence/lab08_cli.txt`.

| # | check | requirement |
|---|-------|-------------|
| 1 | the fan-out node and the merge node are compiled (parallel) | R8 |
| 2 | `swarm_dispatch --Send--> swarm_worker` is a conditional edge | R8, R10 |
| 3 | `swarm_worker --> swarm_merge` is the fan-in edge | R7 |
| 4 | the sequential graph has a loop node and no `Send` worker | R7 (the baseline has to be a real baseline) |
| 5 | Lab 7's back-edge survives under the swarm | R12, R15 |
| 6 | both work-item kinds are fanned out | R1, R3, R7 |
| 7 | submission work is dispatched before unit work (priority) | R3 |
| 8 | every work item is processed and gets a selected candidate | R7 "all units" |
| 9 | each item is best-of-N: three candidates, losers logged | R5, R11 |
| 10 | every candidate is logged with its alignment and its screen | R11 |
| 11 | merge selects the maximum plan-alignment candidate per item | R9, R10 |
| 12 | the 'nicest' (highest-marked) candidate is rejected | R9 |
| 13 | parallel and sequential select identical candidates | R7 |
| 14 | parallel quality matches or beats sequential quality | R7 |
| 15 | quality is priority-weighted (submission 2 : unit 1) | R3 |
| 16 | the parallel pass is lower wall-clock than the sequential pass | R7 |
| 17 | the fan-out really ran concurrently | R7 |
| 18 | the fan-out stayed inside the measured pool width | R7, §b |
| 19 | the fan-out did not drop any work | R7 |
| 20 | the parallel run stops at the human gate with doc §2.7 passing | R13, R16 |
| 21 | resuming does not re-run the swarm or the pre-gate nodes | R16, R17 |
| 22 | an approved run publishes the merged grades | R5, R16 |
| 23 | the `MAX_ATTEMPTS` bound still stops a non-converging swarm | R15 |
| 24 | the swarm ran on every retry, not just the first | R12 |
| 25 | the bound escalates with a diagnosis and the failing draft | R15 |
| 26 | every candidate's durable evidence record is on disk | R11, R14 |
| 27 | the exhaustion run logged every attempt's candidates | R11, R12 |

Check 21 is worth its own line: it asserts from the *compiled* graph's checkpoint
that `ingest`, `plan`, `workers`, `swarm_dispatch` and `swarm_merge` did not run a
second time, so "resume re-ran the expensive stage" cannot pass unnoticed.

### Run the notebook

```
cd notebooks
jupyter nbconvert --to notebook --execute --inplace lab08_parallel_swarm.ipynb
cd ..
python tools/nb_to_text.py notebooks/lab08_parallel_swarm.ipynb docs/evidence/lab08_notebook_output.txt
```

44 cells, 22 of them code, all 22 executed, 0 errors. Rendered output:
[`docs/evidence/lab08_notebook_output.txt`](evidence/lab08_notebook_output.txt).
The notebook ends by removing its own scratch directory (541 paths, four graph
runs and 195 candidate records), so re-running it does not fill the temp volume.

### Run the tests

```
python -m pytest tests/test_lab08_swarm.py -o addopts=
# 104 passed
```

Evidence: [`docs/evidence/lab08_pytest.txt`](evidence/lab08_pytest.txt).
Whole suite: `python -m pytest -o addopts=` → **785 passed, 1 skipped**.

| lab | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|------|---|---|---|---|---|---|---|---|
| cases | 25 | 59 | 121 | 63 | 111 | 170 | 133 | 104 |

(786 collected = 785 passed + 1 skipped; Lab 4's skip is documented in
`docs/lab04.md`.)

---

## e) Test cases and what they prove

104 cases in 13 groups. Every `test_*` name quoted in this document exists in
`tests/test_lab08_swarm.py`.

### The work items

The work-item tests confirm: (1) both kinds are produced (submissions and units), (2) units with no assignment in the corpus still become work items, (3) submission work is enumerated before unit work, (4) every item carries its base record, and (5) unknown kinds raise a loud `ValueError` rather than producing an empty list.

### The candidates

The candidate-calibration tests confirm: submission strategies are distinct, unit strategies are distinct, `generous` inflates the draft by 10%, `sparse` drops a rubric criterion, `overclaimed` invents an assignment it does not own (using a named sentinel), and that sentinel is observable.

### The alignment metric

Alignment tests cover the formula (`(satisfied − violated) / total`, clamped to [0,1]), the fact that `total` comes from a constant (not the run), the trade-off direction, and that a missing `similarity_score` is a violation.

### The merge

The merge tests prove the selector recomputes alignment rather than trusting the candidate's own score; selects by maximum alignment (with tie-break on declared strategy order); rejects the highest-marked candidate when alignment ties; ignores candidates from other attempts; logs every candidate with its own marks (the regression for the stale-variable defect); ensures a work item with no candidate is logged with a null winner; reports priority-weighted quality and quality-by-kind; and that the winner is not trivially "least bad".

### The fan-out

The fan-out tests verify: a `Send` list is only legal from a conditional edge; `fan_out` emits one `Send` per (item, strategy); it carries the plan scope in every payload and reads `swarm_items` from `swarm_dispatch`; both `swarm_candidates` and `swarm_intervals` use the append reducer; and every `SwarmState` key is declared with a docstring.

### The two topologies

Topology tests confirm: the parallel graph has a conditional `Send` edge; the sequential graph has no `Send`; the two differ only in the swarm entry node; Lab 7's back-edge (`validate -> workers`) and gate are preserved; the recursion backstop never fires; and `SwarmState` keeps all of Lab 7's 42 keys while adding the 13 swarm keys.

### The measured criterion

Timing and concurrency tests assert: `max_overlap >= 2` (parallel really ran concurrently); `max_overlap <= SWARM_MAX_CONCURRENCY` (structural bound, no stopwatch); the width is pinned at 8; the adapter pins the config, rejects nonsense widths, and forwards other attributes; parallel and sequential select identical candidates; the parallel stage is not slower than the sequential stage; the sequential baseline has `max_overlap == 1`; and the timing summary reports tasks, span and overlap. No absolute timing values are asserted as invariants.

**No timing value is asserted as an invariant.** The tests assert
`parallel stage_ms < sequential stage_ms` and
`2 <= max_overlap <= SWARM_MAX_CONCURRENCY` — the *relationships*. The absolute
figures in §b's width table and in the evidence files are measurements on one
machine and will differ elsewhere; they are recorded as evidence, not as
contracts.

### Durability, retry and escalation

Durability tests confirm: each candidate writes one durable record; records carry provenance (item, strategy, attempt, plan scope); different attempts do not overwrite; an unserialisable payload fails loudly; the retry bound stops a non-converging run; exhaustion names the failing checks; and the swarm stage runs on every attempt.

### End to end

End-to-end tests confirm: the parallel run stops at the human gate; nothing publishes before the gate; an approved run publishes the merged grades; resuming from another process does not re-run the swarm; the published document reports the merged grades; and the package star-import resolves every declared name.

### The measured evidence

From `docs/evidence/lab08_cli.txt`:

```
candidates  : 39 produced, 13 selected, 26 logged as rejected
submission priority=2, unit priority=1, weights 21
quality=1.0, by kind={'submission': 1.0, 'unit': 1.0}, weights 16:5
submission:DS-A1-S001: generous awarded 17.48 rejected; strict awarded 15.89
  selected at alignment 1.0
length     : 4713 ASCII char(s), approval marker present: True
grades     : 8 approved
stage-speedup: 1.40x at identical quality
parallel peak overlap=8, sequential peak overlap=1
OVERALL: PASS (27/27 checks)
```

and from the exhaustion scenario: 15 nodes, 3 attempts, reason
`retry_budget_exhausted`, failing checks
`draft_grade_within_rubric_maximum` and `similarity_score_attached`, 117
candidate records on disk (3 attempts × 39).

**Reading the "nicest loses" result honestly.** On `DS-A1-S001` the generous
draft is awarded 17.48 against a maximum of 20, so it is *inside* the rubric,
its five screens all pass, it ties `strict` at alignment 1.0, and it loses on
declared strategy order. That is a tie-break, not a screen firing, and the lab
labels it that way. On `DS-A1-S003` the same strategy *does* exceed the maximum
and scores 0.6, so the screen fires there. Both outcomes are in the alignment
table in `docs/evidence/lab08_notebook_output.txt`; the assertion is about the
pair, not about one strategy always losing.

---

## f) Bugs found and fixed

Ten, of which four are in the shipped system and six were in this lab's own
first-draft expectations. Both groups are listed because the second group is what
a reviewer's first hour of reading the tests would find if they were not listed
here.

| # | defect | why it mattered |
|---|--------|-----------------|
| 53 | `graph/__init__.py` listed `CHECKPOINTED_TYPES` and `checkpoint_serializer` in `__all__` **without importing them** | `from course_organizer.graph import *` raised `AttributeError`. A public-API list naming something the module does not have is worse than no list: it fails at the reader's end, not the author's. Found while exporting the 21 new swarm names; fixed by importing both, and locked in by `test_the_package_star_import_resolves_every_declared_name` |
| 54 | `merge_results`'s second loop reused the **stale** `content` variable from the first loop | Every one of the 39 merge-log records carried the *last* candidate's `awarded_marks`/`max_marks` instead of its own. The log looked plausible and was wrong for 38 of 39 rows, which is exactly the shape of a report nobody checks. Fixed with a per-candidate `cand_content`; regression test `test_every_candidate_reaches_the_merge_log_with_its_marks` |
| 55 | The `Send` fan-out's pool width was **unbounded** | Reproducibly unstable: the stage won in a plain process (1.27x–1.58x) and **lost** in a Jupyter kernel (0.71x), because each task's LangGraph bookkeeping is GIL-serialised. R7's acceptance criterion was a coin toss. Fixed by pinning `max_concurrency` to the measured width 8 (§b), and the bound is now checkable without a stopwatch |
| 56 | The parallel/sequential comparison was made across a **cold start** | The first graph run in a fresh kernel absorbed ~49ms of thread-pool, `Send`-path and SQLite setup — more than the whole parallel stage — while the sequential run that followed was warm. The measurement was unfair in the direction of failure, and it would have been unfair in either direction. Both topologies are now warmed once, those numbers printed and discarded, and the lab's own command shown to pass from cold |
| 57 | The first test suite assumed `generous` always breaks `within_rubric_maximum` | It only does when the base is near the maximum. On the seed corpus the generous draft is usually *legal*, ties at 1.0, and loses on declared order. The test asserted a screen firing where a tie-break was correct — a test that would have failed for the right reason and been "fixed" the wrong way |
| 58 | The suite assumed two violations score 0.6 | The metric is `(satisfied − violated) / total`, so two violations of five score 0.2. Both numbers were in the code; the expectation was not derived from it |
| 59 | The suite assumed `overclaimed` violates only `no_overclaim` | It also breaks `assignments_in_unit`, which is the more interesting half: the same invented assignment is caught by two independent screens |
| 60 | The `fan_out` test passed a raw corpus state | `fan_out` reads `swarm_items`, which `swarm_dispatch` writes. The test produced **zero** sends and still passed, because it only counted what it had put in |
| 61 | The suite assumed `SwarmState.__mro__[1]` is `CourseState` | It is `dict`. A `TypedDict` flattens inherited annotations, so there is no base to walk and the reducer-inheritance test was walking the wrong object |
| 62 | `typing.get_type_hints(SwarmState)` was called without `include_extras=True` | The `Annotated` reducer metadata is stripped, so the test asserting the append channel was asserting against a stripped copy and could never have failed |

Bug 62 is the one worth generalising: **a test that reads the object through an
API that silently drops the property under test is worse than no test**, because
it reports coverage. Bug 60 is the same shape from the other side — a test that
counted its own fixture.

Bug 55 and bug 56 also share a shape with Lab 6's bug 32 (*the code was correct
and the feature was inert*): the first implementation of "parallel" satisfied
every structural requirement and still did not reliably deliver a lower
wall-clock time, because the thing that decides the number — how wide the pool
is, and whether the interpreter is warm — was never made explicit.

### Known limitation, stated rather than hidden

The wall-clock win comes from the durable evidence write, because that is the
only GIL-releasing work in the stage. In a real deployment the workers would call
an LLM, and the margin would be orders of magnitude larger. On the seed corpus
the ratio is around 1.4x, not 10x, and the lab does not pretend otherwise. A
CPU-only variant of this stage would be *slower* in parallel than in sequence —
probed, at 0.99x for a hash-heavy equivalent — and the lab says so in the
notebook rather than hiding the probe.

### Common errors and fixes

| symptom | cause | fix |
|---------|-------|-----|
| `InvalidUpdateError` on a `Send` list | the list was returned from a node | a `Send` list is only legal as a conditional-edge path function; move the fan-out into `fan_out` |
| the topology dump shows `swarm_dispatch -> __end__` and no `Send` edge | `add_conditional_edges` was called with no path list | pass `["swarm_worker"]` explicitly, or the edge is invisible in `get_graph()` |
| the merge sees exactly one candidate | `swarm_candidates` is on the default `LastValue` reducer | declare it `Annotated[list[dict], append]`; assert the annotation, because at 1 candidate both reducers behave identically |
| `max_overlap == 1` in the parallel run | the graph was compiled without the `Send` edge, or the run used `parallel=False` | check `inspect_swarm(ctx, parallel=True)["edges"]` before trusting a timing number |
| parallel slower than sequential | the pool was unbounded, or the interpreter was cold | §b's width table; `SWARM_MAX_CONCURRENCY = 8` and a warm-up pair |
| `AttributeError` on `from course_organizer.graph import *` | a name in `__all__` that is not imported | bug 53; `test_the_package_star_import_resolves_every_declared_name` |
| a resume re-ran the whole swarm | the resume did not use `Command(resume=...)`, or the pre-gate node was marked retryable | check 21 asserts the pre-gate nodes did not run again, from the checkpoint |
| `RecursionError` / `GraphRecursionError` | the back-edge was unbounded | `MAX_ATTEMPTS`; the recursion limit is a backstop that must never fire, and there is a test that says so |

---

## g) Requirement coverage

| # | Requirement (quoted in §a) | Where it is met | How it is checked |
|---|-----------------------------|-----------------|-------------------|
| R1 | *"many assignment submissions, then merge"* | `build_work_items`, `fan_out`, `merge_results` | checks 6, 8, 9 |
| R2 | *"Parallelise the per-unit work and select the best."* | `fan_out` (`Send`) + `merge_results` | checks 2, 3, 11 |
| R3 | *"Several worker agents process assignment submissions in parallel; a merge/selector picks the best per the plan"* | `make_swarm_worker`, `KIND_SUBMISSION` items, priority 2 | checks 6, 7, 11, 15 |
| R4 | *"Locked plan; per-unit work items."* | `swarm_dispatch` reads `plan_locked`; `plan_scope` is built before the fan-out | checks 8, 20 |
| R5 | *"Best-of-N results merged into one compliant output."* | 3 strategies per item; `unit_coverage` + `draft_grades` assembled by Lab 7's `assemble`/`publish` | checks 9, 22 |
| R6 | *"Parallel workers + a Merge selector under the Coordinator."* | `swarm_worker` × 39, `swarm_merge`, both reached through Lab 7's `ingest -> plan -> workers` | checks 1, 2, 3 |
| R7 | *"A fan-out/fan-in that processes all units and merges by alignment to the plan. Accept: Parallel run matches or beats sequential quality at lower wall-clock time."* | all 13 items processed; `alignment_to_plan` recomputed in the merge; `swarm_timing` | checks 8, 11, 13, 14, 16, 17, 18, 19 |
| R8 | *"Parallel worker fan-out + merge."* | `Send` conditional edge + `swarm_worker -> swarm_merge` | checks 1, 2, 3 |
| R9 | *"Merge strictly by plan alignment; picking the 'nicest' output quietly breaks coverage."* | `max(scored, key=score then declared order)`; the log records the marks so the loser is visible | checks 11, 12 + `test_the_nicest_candidate_is_rejected_in_favour_of_the_aligned_one` |
| R10 | `parallel_map(process_assignment, plan.units)` / `merge(results, key=alignment_to(plan))` | `fan_out` and `alignment_to_plan` | checks 2, 11 |
| R11 | *"the best-aligned one that satisfies the plan is selected, the rest logged"* | `merge_results` + `write_candidate_artifact` per candidate | checks 9, 10, 26, 27 |
| R12 | *"on failure only this assignment submission is regenerated."* | per-item work items; each attempt re-fans-out | checks 5, 24 |
| R13 | §2.7's four validation checks still gate the run | `validate` is Lab 7's node, unchanged, after `swarm_merge` | check 20 |
| R14 | *"Grounded generation with an evidence record per unit; ungrounded items are rejected."* | the `no_overclaim` screen; `GHOST_999`; per-candidate evidence records | check 26 + `test_the_overclaimed_unit_borrows_an_assignment_it_does_not_own` |
| R15 | *Bound `MAX_ATTEMPTS`*, escalate with a diagnosis | Lab 7's bound, unchanged; the exhaustion scenario | checks 23, 25 |
| R16 | *"Never publish a grade or feedback without faculty approval."* | Lab 7's real `interrupt()` gate, reached from `swarm_merge`'s superstep | checks 20, 21, 22 |
| R17 | *"Keep clear version history for notes and materials."* | `out/lab08/<run_id>/` per run; attempt in every record's filename | `test_records_from_different_attempts_do_not_overwrite_each_other` |

### Where the lab does *not* touch the document

Lab 8 owns no new agent, no new data model, and no change to the validation
rules. The document's §2.8 "Parallel collection" row (line 584) — *"Submission
Collection Agent and Plagiarism/Similarity Agent process each submission as it
arrives"* — is the same fan-out idea one level up, and Lab 8 implements it at the
work-item level rather than the agent level; the agents themselves are still
Lab 7's six specialists, run once in `workers` before the fan-out. That is a
deliberate scope decision, and it is the reason Lab 8 required no change to
`nodes.py`.

---

## h) Report / documentation text

The following is the prose suitable for submission as the lab write-up.

---

**Lab 8 — Parallel Swarm**

**Objective.** §2.10 specifies a fan-out/fan-in over the course's work items,
merged by alignment to the locked plan, accepted when the parallel run *matches
or beats* sequential quality *at lower wall-clock time*. This lab implements that
as a second LangGraph topology that reuses the Lab 7 pipeline and inserts one
stage between `workers` and `validate`.

**Method.** A dispatch node reads the locked plan and builds 13 work items — 8
assignment submissions and 5 syllabus units, submissions first because §2.10 names
them first — and attaches to each the base record its candidates will calibrate.
A conditional edge then emits one `langgraph.types.Send` per `(item, strategy)`
pair, 39 in total: three deterministic calibrations of each submission draft
(`strict`, `generous`, `sparse`) and three of each unit's coverage record
(`complete`, `outline`, `overclaimed`). Each worker produces its candidate,
writes a durable evidence record, and returns it through an `append` channel; the
fan-in at `swarm_merge` recomputes each candidate's alignment to the plan from
the candidate's own content and selects, per item, the maximum. A submission that
was already selected still produces its two losers, and they are logged, because
§2.7 requires the rest to be logged and §2.11 requires an evidence record per
unit.

**The merge key.** Alignment is `(satisfied − violated) / total` over a fixed
obligation set — five screens for a submission, five for a unit — where `total`
comes from a module constant rather than from the run, so a candidate cannot
raise its own score by satisfying more checks. Each obligation is a doc sentence
restated as something checkable: *a draft grade never exceeds the rubric
maximum*; *a plagiarism/similarity score is attached*; *late submissions are
flagged accurately*; rubric criteria complete; the assignment is in the plan; and,
for a unit, no claim beyond the corpus. The merge does not read the candidate's
self-reported score. It recomputes it, because a selector that trusted the
producer's number would be selecting on the thing it exists to verify.

**The pitfall, and what actually happened.** §2.10's warning — *picking the
'nicest' output quietly breaks coverage* — is demonstrated rather than asserted.
The `generous` calibration inflates each draft by 10%, so it is the nicest
candidate by marks. On the seed corpus the result is more interesting than the
warning implies: on most submissions the inflated draft is still *within* the
rubric, passes all five screens, and **ties** the strict draft at alignment 1.0,
losing only on the declared strategy order. On `DS-A1-S003` it exceeds the
maximum and is caught, scoring 0.6. The report says which is which, because
claiming that a screen fired when a tie-break decided the outcome would be the
same class of error the pitfall is about.

**Wall-clock.** The acceptance criterion is met and measured, and the measurement
is reported with its conditions. Two findings shaped the design. First, CPython's
GIL means a thread pool does not speed up pure-Python work — a hash-heavy fan-out
probed at 0.99x of serial — so the only source of a real win here is the durable
evidence write each worker must make, which releases the GIL. Second, an unbounded
fan-out is not merely wide, it is *unstable*: with 39 threads the stage won in a
plain process and lost reproducibly in a Jupyter kernel, because LangGraph's
per-task bookkeeping is GIL-serialised. Pinning the pool to a measured width of 8
made the criterion hold in both contexts. The bound is also verifiable without a
stopwatch, because peak overlap cannot exceed the pool width — so the lab asserts
both the timing relationship and the structural one, and a single favourable
measurement cannot carry the claim.

**Result.** On the seed corpus: 13 work items, 39 candidates, 13 selected and 26
logged as rejected, priority-weighted quality 1.0 for both kinds of work item,
§2.7's four checks passing, the run stopping at the faculty gate, and — after a
real cross-process `Command(resume=…)` approval — an 8-grade published document of
4,713 characters with the approval marker present. Under an injected defect that
never converges, the `MAX_ATTEMPTS` bound fires at 3 attempts, all 117 candidates
across the three attempts are on disk, and the escalation names both failing
checks. The CLI reports `OVERALL: PASS (27/27 checks)` with exit code 0, from a
cold process; 104 tests pass for this lab and 785 across the suite.

**Scope.** Lab 8 changed no Lab 7 graph, node, runner or test. Its module is
additive (`graph/swarm.py`), its state is a subclass of `CourseState` with 13
added keys, and its only edit to a Lab 7 module was repairing a pre-existing
defect in `graph/__init__.py`'s `__all__`, which is now covered by a test.
