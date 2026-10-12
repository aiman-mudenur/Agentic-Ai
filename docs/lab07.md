# Lab 7 — Agentic Node Graph: the full pipeline

**Phase:** B — Orchestration
**Goal (doc §2.10, Lab 7):** *"Wire the specialist agents into one explicit graph with state and branching."*
**What it adds (doc §2.10):** *"The complete pipeline as a node graph: Notes Repository Agent, Assignment Distribution Agent, Submission Collection Agent → … → course faculty gate → publish, with a validation → regenerate back-edge."*
**Outputs (doc §2.10):** *"An executable graph producing a first end-to-end draft."*
**Deliverable (doc §2.10):** *"A graph runner that produces a full organised course workspace (notes, assignments, grades) draft end to end. Accept: One command yields a validated organised course workspace (notes, assignments, grades) draft end to end."*
**Nodes activated (doc §2.10):** *"All core nodes + the back-edge."*
**Pitfall (doc §2.10):** *"Bound the retry edge — an unbounded regenerate loop can spin forever."*

---

## a) Goal and the document's requirements

The document's requirements for Lab 7 are on lines 685–697 of the extracted
Worklet 2 text, plus the §2.7 validation checks (line 558), the §2.12 guardrails
and escalation row (lines 825–834), and the §2.4/§2.5/§2.6 system definitions
(lines 515–549) that name the agents this lab wires together. Nothing below is
inferred beyond those lines.

| # | Doc requirement (verbatim) | Where it is met |
|---|---|---|
| R1 | *"Goal: Wire the specialist agents into one explicit graph with state and branching."* | `graph/builder.py::build_graph` returns one `StateGraph` compiled once; `graph/state.py::CourseState` is the shared state. There is one graph object, built in one place. |
| R2 | *"The complete pipeline as a node graph: Notes Repository Agent, Assignment Distribution Agent, Submission Collection Agent → … → course faculty gate → publish"* | The compiled graph (`compiled_nodes`) is `ingest, plan, workers, validate, assemble, gate, publish, escalate`. The six Phase-A agents run inside `workers`, in roster order, each auditing itself. |
| R3 | *"with a validation → regenerate back-edge"* | `validate --fail--> workers` as a conditional edge, plus `workers --forward--> validate`. Verified from the compiled graph, not from the constant: `[cond] validate -> workers`. |
| R4 | *"An executable graph producing a first end-to-end draft."* | `graph/runner.py::run_pipeline`. One call executes the chain and stops at the gate with a draft and a validation report. |
| R5 | *"Deliverable. A graph runner that produces a full organised course workspace (notes, assignments, grades) draft end to end."* | The `graph/` package — 9 modules, 3,720 lines — plus `labs/lab07_node_graph.py`, 575 lines. 4,295 lines in total. |
| R6 | *"Accept: One command yields a validated organised course workspace (notes, assignments, grades) draft end to end."* | `python -m course_organizer.labs.lab07_node_graph` runs four scenarios and prints `OVERALL: PASS (18/18 checks)`. Captured in `docs/evidence/lab07_cli.txt`. |
| R7 | *"Nodes activated. All core nodes + the back-edge."* | Acceptance check 1 requires every node named on line 696 to be present in the compiled graph, and check 2 requires the back-edge. |
| R8 | *"Pitfall. Bound the retry edge — an unbounded regenerate loop can spin forever."* | An explicit `attempts` counter in the state, `MAX_ATTEMPTS = 3`. The route after `validate` sends a failing run back to `workers` **only while budget remains**; once spent it goes to `escalate`. Checks 12–15. |
| R9 | §2.12 *"Bound MAX_ATTEMPTS (Lab 7/10); on exhaustion, escalate to course faculty with a diagnosis."* | `graph/escalation.py::build_escalation` attaches the diagnosis *and* the failing draft, so the human can correct the rows without re-running. Checks 14 and 15. |
| R10 | §2.7 *"a draft grade never exceeds the rubric maximum"* | `check_draft_grade_within_rubric_maximum`. `RUBRIC_TOLERANCE = 0.0`, because §2.7 says *"never"*. |
| R11 | §2.7 *"late submissions are flagged accurately"* | `check_late_submissions_flagged_accurately`, comparing the flag against the store's own timestamp — never against itself. |
| R12 | §2.7 *"a plagiarism/similarity score is attached"* | `check_similarity_score_attached`. An absent score is a failure, because an absent score is not a zero score. |
| R13 | §2.7 *"no grade is published without faculty approval"* | `check_no_grade_published_without_faculty_approval`, and a second, local guard inside the `publish` node so the invariant does not depend on the graph being wired correctly. |
| R14 | §2.12 *"Never publish a grade or feedback without faculty approval."* | `publish` recomputes the approval from the state and raises if any grade is not approved by the gate's own decision. Check 7 (nothing published) and check 16 (a refusal publishes nothing). |
| R15 | §2.12 *"Never make a final plagiarism/similarity determination automatically — always flag for human review."* | `similarity_flagged` is set, and no penalty is applied to any mark. The gate request renders the flags under *"flagged for faculty review (not adjudicated)"*. |
| R16 | §2.12 *"Never alter a submission timestamp."* | The similarity and late checks read the store's timestamp and copy it; they never rewrite it. The escalation remedy for a late-flag failure is *"re-read the submission timestamps from the store"*. |
| R17 | §2.6 message map: Coordinator → … → Faculty Review → Publishing → Course Faculty gate → Coordinator | `interrupt()` is a real LangGraph interrupt; `Command(resume=…)` carries the faculty decision back in. Verified across a process boundary. |
| R18 | §2.4 roster: the six specialist agents (positions 1–6) | `graph/specialists.py`. `SPECIALIST_NAMES` matches the roster order exactly; `test_there_are_six_specialists_in_doc_roster_order` asserts it. |
| R19 | §2.12 *"Keep a full audit log of every grade published and who approved it."* | Every node appends to `audit_trail`; a published grade carries `approved_by` and `approved_at`, and the published document names the approver. |

### Where the lab's own choices are labelled as such

The document fixes the nodes and the edges; it does not fix every implementation
choice. These are the lab's decisions, and each is labelled here so a reviewer can
tell what the document required from what was chosen:

* **One checkpointer file per run** (`graph.sqlite`), with the graph under
  LangGraph's own namespace and Lab 6's runtime under
  `checkpoint_ns='course-organizer/runtime'`. The two coexist in one file; this
  was verified rather than assumed.
* **The bound is an explicit `attempts` counter**, not the recursion limit.
  `RECURSION_BACKSTOP = 32` exists only so a miswired graph cannot run forever.
* **No `Send` fan-out.** The six specialists run sequentially inside `workers`.
  Parallel fan-out is Lab 8's subject, and doing it here would blur the two labs.
* **The similarity measure is lexical**: word-trigram shingles after stopword
  removal, Jaccard overlap, flag at `0.30`. §2.7 requires a score to be attached;
  it does not prescribe how it is computed. See section b for the design that was
  rejected.

---

## b) Brief concepts

### Doc 2.10's pseudocode, read literally

Line 696 gives the graph as four lines:

```
graph.chain(ingest -> plan -> workers -> validate)
graph.edge(validate --fail--> workers)   # bounded retry
graph.edge(validate --pass--> assemble -> gate -> publish)
```

Read as a specification this says: a linear chain, plus a back-edge from
`validate` to `workers`, plus a forward route from `validate` through `assemble`
and `gate` to `publish`. The compiled graph is exactly that, with one addition —
`escalate` — because the pitfall on line 693 and the §2.12 row on line 825 both
demand somewhere for an exhausted run to go.

### Why a conditional edge alone is not a cycle

This is the single most likely way to get Lab 7 wrong, so it is worth stating
plainly. A conditional edge `validate -> workers` does **not** by itself create a
loop. Without `workers -> validate` as well, the run reaches `validate`, routes
to `workers`, and stops — the back-edge has nothing to return to. The cycle
exists only because both edges are present.

The lab therefore asserts both directions separately, read off the compiled edge
set:

```
[cond] validate -> workers
[edge] workers -> validate
```

This was verified against langgraph 1.2.12 with a probe before either edge was
trusted.

### The bound, and why the recursion limit is not it

`recursion_limit` is enforced by LangGraph and raises
`GraphRecursionError: Recursion limit of 5 reached without hitting a stop
condition.` It is a crash, not an outcome — the human gets a stack trace, not a
diagnosis. So the bound that does the work is the explicit `attempts` counter in
the state, which routes to `escalate` and produces a written diagnosis. The
recursion limit is set to a high backstop (`32`) and must never fire on a
legitimate run; a test asserts the loop is bounded by the design and not by the
limit.

### The similarity measure, and a rejected design

Doc §2.7 requires *"a plagiarism/similarity score is attached"* and §2.12 forbids
an automatic determination. Both matter: the score must exist, and it must not be
an accusation.

The first design produced a score by hashing the submission id. It was
**fabricated evidence dressed as a measurement** — every stored
`similarity_score` in the seed corpus is `None`, so any number would have been
invented. It was removed.

The replacement is a real lexical measure: lowercase and tokenise the submission
text, drop stopwords, form word-trigram shingles, and take the Jaccard overlap
against every peer **in the same assignment**, keeping the maximum. Flag at
`SIMILARITY_FLAG_THRESHOLD = 0.30`, with `SHINGLE_WIDTH = 3`. The store's own
score wins when it is populated; otherwise the measured score is used, and each
flag records which of the two it came from (`source`).

On the seed corpus the measure finds a genuine near-duplicate:
`DS-A1-S001` and `DS-A1-S003` score **1.000** against each other, because the two
texts differ only by `insert_after` → `insert-after` style edits, so their
trigram *sets* are identical (88 shingles each). Every other pair is at or below
0.01. That is a real property of the data, not a chosen result.

### `workers` is six agents, not six nodes

Doc §2.10 names the pipeline by its agents; doc §2.4 lists eight roster
positions. This lab keeps the graph small on purpose: `workers` runs all six
Phase-A specialists in roster order and threads each one's output into the next,
auditing each separately. The graph therefore has eight nodes, not fourteen, and
Lab 8 can add parallelism without changing the topology contract.

### Drift: report only what the plan verified

The `assemble` node builds the workspace *shell* and reports facts back to the
locked plan. The danger is reporting facts the plan never verified — a number
that appears in the document but not in the plan is drift, and drift means the
document is asserting something nobody checked.

So `assemble` reports **only** keys in `GROUNDED_FACT_KEYS`, sourced from the
scope tool's own `verified_facts`. The pipeline's internal counts (draft grades
produced, marks totalled) are *not* reported as workspace facts; the grades live
in the gate request and the published document instead. This turned 11 false
"out of scope" drift findings into **0**.

---

## c) The code

### Files

```
src/course_organizer/graph/
    __init__.py       191 lines   the public surface, re-exported
    state.py          399 lines   CourseState, the models, the doc constants
    validation.py     383 lines   doc 2.7's four checks, explain_failure, next_route
    specialists.py    739 lines   the six Phase-A agents, the similarity measure
    context.py        261 lines   RunContext and the query factories
    nodes.py          877 lines   the eight nodes, including the real gate
    escalation.py     107 lines   the escalation, with the draft attached
    builder.py        314 lines   the topology, the routes, the backstop
    runner.py         449 lines   run_pipeline, resume_run, the checkpointer
                      ---
                    3,720 lines   the package
src/course_organizer/labs/
    lab07_node_graph.py  575 lines   the CLI, the four scenarios, 18 checks
                      ---
                    4,295 lines   Lab 7 in total
```

Nine modules, one concern each. `__init__.py` re-exports 74 names in its
`__all__`, so a caller writes `from course_organizer.graph import run_pipeline`.

### The state: 42 keys, and the AST guard that keeps them honest

`CourseState` is a `TypedDict` of 42 keys. LangGraph **silently discards** any key
a node returns that the TypedDict does not declare — the state simply does not
grow, and the next node sees `None`. That cost hours before it was understood, so
a test now parses `nodes.py` and `specialists.py`, finds every top-level
`return {...}` of a node factory, and fails if any returned key is not declared:

```python
def test_every_key_a_node_returns_is_declared_in_the_state():
    # ... walks the AST of nodes.py and specialists.py ...
    assert not offenders, (
        "these keys are returned at the top level of a node but are not "
        f"declared in CourseState, so LangGraph discards them: {sorted(offenders)}"
    )
```

### The topology

`build_graph(ctx, checkpointer=...)` adds the nodes, the linear chain, the two
conditional edges and the back-edge, then compiles **with the checkpointer
passed to `compile()`**. This matters: a checkpointer given to `invoke()` instead
is ignored, and the run silently uses an in-memory saver. `graph_edges()` reads
the compiled structure rather than a declared list:

```
compiled edges (source -> target, conditional):
  [edge] __start__ -> ingest
  [edge] assemble -> gate
  [edge] escalate -> __end__
  [cond] gate -> escalate
  [cond] gate -> publish
  [edge] ingest -> plan
  [edge] plan -> workers
  [edge] publish -> __end__
  [cond] validate -> assemble
  [cond] validate -> escalate
  [cond] validate -> workers
  [edge] workers -> validate
```

### Doc 2.7's four checks

`validation.py` implements the four checks on line 558 as pure functions of a
`CheckContext`, and `validate()` runs all four and returns a `ValidationOutcome`
carrying `passed`, `failing`, the per-check details, and a diagnosis.

A defect worth naming: two of the four checks were bound directly into the
dispatcher while the other two used a `ctx`-unpacking lambda, so the direct ones
raised `TypeError: 'CheckContext' object is not iterable`. The dispatcher now
calls every check uniformly with the context.

### The six specialists

`run_specialists(state, ctx)` runs the roster in order. Each specialist returns a
`SpecialistResult` with its own `step_id`, `agent`, `intent` and `ok` flag, and
each appends its own audit entry. The output of each is merged into the state
before the next runs — the bug that motivated this was passing every specialist
the same un-merged state, so `grading` never saw `similarity`'s scores.

### The gate: a real `interrupt()`

The `gate` node calls `interrupt(payload)`. `invoke()` returns with the payload
under `__interrupt__`, the run suspends, and the checkpoint is written. Resuming
with `Command(resume=decision)` re-enters the gate node from the top, where
`interrupt()` immediately returns the resumed value —— so the nodes *before* the
gate do not re-run. The cross-process test asserts exactly that: after a resume,
`nodes_ran()` counts `ingest` once.

`FacultyDecision.decision` has a `field_validator` that normalises case and
whitespace and then rejects anything unknown, so `"Approve"` is honoured while a
typo raises instead of silently becoming a refusal:

```
ValidationError: 1 validation error for FacultyDecision
decision
  Value error, unknown faculty decision 'aprove'; the gate answers with one of:
  approve, edit, override, refuse
```

### Publish re-checks the approval

Lab 3's formatter is hard-wired for a draft and emits `Approved : no` and
`Published: no`. A published artifact carrying those lines would contradict
itself above a table of published grades. Rather than edit Lab 3's tested module,
the `publish` node stamps the pre-gate lines (`## Approval Required` →
`## Approval Recorded`) and re-checks the approval locally. So the guardrail does
not depend on `validate` having run before `publish`.

### The checkpointer's type allowlist

LangGraph serialises state with ormsgpack and refuses by default to rebuild a
type it does not recognise, warning once per type per process: *"Deserializing
unregistered type … This will be blocked in a future version."* Every pydantic
model and enum in the state is one of those. `runner.py` declares them explicitly
via `JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINTED_TYPES)`, which is the
remedy the warning itself names. The allowlist is also a security control: it
names exactly which classes may be rebuilt from a checkpoint file, and every entry
must come from `course_organizer`. A test derives the set from `CourseState`'s
annotations, so a new model cannot be forgotten.

---

## d) Setup and run

### Environment (already provisioned)

```
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe
```

Python 3.14.7. The Lab 7 dependencies are already in the environment:
`langgraph 1.2.12`, `langgraph-checkpoint 4.2.0`,
`langgraph-checkpoint-sqlite 3.1.1`, `pydantic 2.13.5`. No API key and no network:
the default `LLM_BACKEND=scripted` runs the whole lab, which is what lets a
reviewer re-run the acceptance criteria without credentials.

### Run the lab

```powershell
$env:PYTHONPATH = "src"
python -m course_organizer.labs.lab07_node_graph
```

The command runs four scenarios in a fresh temporary directory and prints the
compiled topology, each scenario's run, and the acceptance checks. It exits 0
when every check passes, 1 otherwise. `--keep` preserves the workspace,
`--json` writes the checks to `lab07_checks.json`, `--verbose` logs each node
execution.

### The 18 acceptance checks

```
  [PASS]  1. every doc node is compiled into the graph
  [PASS]  2. the back-edge exists: validate --fail--> workers
  [PASS]  3. workers --forward--> validate, without which the loop cannot cycle
  [PASS]  4. the clean run executes the doc path and stops at the gate
  [PASS]  5. the clean run stops at a real human gate
  [PASS]  6. all four of doc 2.7's checks ran and passed
  [PASS]  7. nothing published before a human approved
  [PASS]  8. every grade offered to the reviewer is still a draft
  [PASS]  9. the assembled document has no drift against the locked plan
  [PASS] 10. the gate request carries grades, flags and the validation report
  [PASS] 11. the plan was locked before any work began
  [PASS] 12. the back-edge retried and converged inside the bound
  [PASS] 13. each retry re-entered workers
  [PASS] 14. the bound stops a loop that never converges
  [PASS] 15. exhaustion escalates with a diagnosis and the failing draft
  [PASS] 16. a faculty refusal publishes nothing and escalates
  [PASS] 17. a refused run leaves every grade unapproved
  [PASS] 18. a suspended run's gate request survives the process
```

Everything printed is computed from the runs. No number in the transcript is
written by hand, so the transcript and the checks cannot disagree.

### Run the notebook

```powershell
python -m jupyter nbconvert --to notebook --execute --inplace notebooks\lab07_node_graph.ipynb
```

36 cells, 16 of them code. It executes with the `course-organizer` kernelspec and
finishes with 0 errors.

### Run the tests

```powershell
python -m pytest tests/test_lab07_graph.py tests/test_lab07_runner.py
# 133 passed

python -m pytest
# 681 passed, 1 skipped
```

---

## e) Test cases, expected output, and proof

### The four scenarios, and what each must show

| Scenario | Command | Must show |
|---|---|---|
| Clean | `run_pipeline(run_id="clean")` | 5 nodes `ingest → plan → workers → validate → assemble`; suspended, `status=draft`; 8 draft grades; 0 drift; 2 similarity flags; **no** published file. |
| Retry | `run_pipeline(run_id="retry", fail_until_attempt=2)` | 9 nodes; passes `[1, 2, 3]`; `workers` and `validate` each executed 3 times; converges on pass 3 and reaches the gate. |
| Exhaustion | `run_pipeline(run_id="exhaustion", fail_until_attempt=99)` | 9 nodes; stops at exactly `MAX_ATTEMPTS`; `escalate` reaches `course_faculty` with a diagnosis and the failing draft. |
| Refusal | `resume_run("refusal", DECISION_REFUSE)` | `status=refused`; `published=False`; `published_path=None`; every grade still `draft`; escalation `reason=refuse`. |

The retry log, quoted from `docs/evidence/lab07_cli.txt`:

```
pass 1: faulty: w4, w5
pass 2: faulty: w4, w5
pass 3: all specialists ok

validate   pass 1: 2 of 4 check(s) failed (draft_grade_within_rubric_maximum, similarity_score_attached) -> fail
validate   pass 2: 2 of 4 check(s) failed (draft_grade_within_rubric_maximum, similarity_score_attached) -> fail
validate   pass 3: all 4 check(s) passed
```

### The suite

133 Lab 7 test cases: 73 in `tests/test_lab07_graph.py` (pieces — state,
validation, specialists, escalation, topology) and 60 in
`tests/test_lab07_runner.py` (the compiled graph actually running). Three
functions are parametrised, which is why 126 `def test_*` functions collect as
133 cases.

Every test runs the real compiled graph against the real seed corpus with the
scripted backend. There are no mocks around `invoke()` on purpose: a mock cannot
show whether the checkpointer reached `compile()`, and that is one of the
mistakes this lab is most likely to make.

The full suite is **681 passed, 1 skipped**, exit 0. The skip is Lab 4's
network-dependent fastembed test, unchanged.

### The tests that matter, and what each would have caught

* `test_every_doc_node_is_compiled_into_the_graph` — reads node ids off the
  compiled graph, so a node dropped from the wiring fails even though the
  declared chain still reads correctly.
* `test_workers_points_at_validate_which_is_what_makes_the_back_edge_loop` —
  asserts the forward edge, without which the back-edge cannot cycle.
* `test_validate_can_reach_workers_assemble_and_escalate` and
  `test_the_three_validate_destinations_are_conditional` — all three routes
  exist, and all three are conditional.
* `test_the_loop_is_bounded_by_the_design_not_by_the_recursion_limit` — the
  exhaustion run stops at `MAX_ATTEMPTS` and escalates; no `GraphRecursionError`.
* `test_resume_runs_only_the_gate_and_publish` — pre-gate nodes do not re-run.
* `test_resume_preserves_the_grades_the_reviewer_was_shown` — the grades are the
  checkpointed ones, not recomputed.
* `test_a_refusal_publishes_nothing_and_escalates` and
  `test_a_refusal_never_writes_a_file` — a refusal adds no file at all.
* `test_the_published_document_names_who_approved_it` — the artifact names the
  approver, rather than asserting it was not approved.
* `test_identical_text_scores_one` / `test_unrelated_text_scores_near_zero` — the
  similarity measure measures, including the empty-document case that divides by
  zero if handled naively.
* `test_the_similarity_agent_never_sets_a_penalty` — measuring is not
  adjudicating; no mark is changed by a flag.
* `test_every_key_a_node_returns_is_declared_in_the_state` — the AST guard
  against keys LangGraph would silently discard.
* `test_every_checkpointed_type_is_declared_to_langgraph` and
  `test_a_resume_emits_no_unregistered_type_warning` — the allowlist is the
  right classes, and a real resume emits no unregistered-type warning.
* `test_an_unknown_course_raises_rather_than_reporting_an_empty_scope` — an
  unknown course fails loudly; a silent empty result would validate trivially.
* `test_a_diagnosis_never_opens_with_whitespace` and
  `test_an_escalation_diagnosis_contains_no_doubled_space` — the rendered
  diagnosis reads as prose.

### Real bugs this lab found

Fifteen defects were found and fixed while building Lab 7. They are numbered
38–52 in the README's project bug log; the two most instructive are:

* **Fabricated similarity scores.** The first measure hashed the submission id.
  Every real `similarity_score` in the seed corpus is `None`, so the number was
  invented. It was replaced by the trigram-Jaccard measure in section b.
* **A published document that contradicted itself.** Lab 3's formatter stamps
  `Approved : no` and `Published: no` for a draft. A run that had been approved
  and published still carried those lines above its table of published grades.
  The `publish` node now stamps them, and a test asserts the artifact names the
  approver.

### Proof captured

| Evidence file | What it shows |
|---|---|
| `docs/evidence/lab07_cli.txt` (8,984 bytes) | The CLI transcript: compiled topology, all four scenarios, `OVERALL: PASS (18/18 checks)`, exit 0. |
| `docs/evidence/lab07_pytest.txt` (1,182 bytes) | `python -m pytest`, `681 passed, 1 skipped`, exit 0. |
| `docs/evidence/lab07_notebook_output.txt` (26,578 bytes) | The notebook rendered by `tools/nb_to_text.py`: 36 cells, 16 code, 16 executed, 0 errors. |

All three are UTF-8 without a BOM and pure ASCII. The CLI evidence keeps stdout
and stderr separate, so the two legitimate `escalate` warnings are recorded
rather than interleaved into the transcript.

---

## f) Common errors, and the bugs found while building this

### Errors you will hit, and what they mean

**`AttributeError: '_GeneratorContextManager' object has no attribute 'setup'`**
`SqliteSaver.from_conn_string(path)` returns a *context manager*, not a saver.
Enter it first. This lab opens the connection itself so it can pass a serializer,
which `from_conn_string` does not allow.

**The run doesn't persist, or a resume finds nothing.**
The checkpointer was passed to `invoke()` rather than to `compile()`. LangGraph
silently substitutes an in-memory saver, so nothing is written and nothing can be
resumed. Pass it to `compile(checkpointer=...)`.

**A state key a node returns is `None` downstream.**
The key is not declared in `CourseState`. LangGraph discards undeclared keys
without an error. Add it to the TypedDict; the AST test will then keep it honest.

**`GraphRecursionError: Recursion limit of N reached without hitting a stop
condition.`**
The back-edge has no bound that can succeed. Either the forward edge is missing
(so the "loop" is not a loop and the run stops early) or the `attempts` counter is
not advancing. This is a miswiring, not a legitimate retry: a correct run stops
by routing to `escalate`, never by hitting the limit.

**A resume re-runs the nodes before the gate.**
It should not. On resume the gate node re-enters from the top and `interrupt()`
returns immediately; if earlier nodes run again, the resume is rebuilding state
instead of reading it from the checkpoint.

**`TypeError: 'CheckContext' object is not iterable`.**
A check was bound directly into the dispatcher instead of being called with the
context. All four checks take the context as their single argument.

**`ValidationError: unknown faculty decision 'aprove'`**
A spelling mistake in the gate answer. The validator normalises case and
whitespace, then rejects anything not in `approve | edit | override | refuse`.
This is deliberately strict: an unknown answer must not be silently treated as a
refusal.

**`ImportError: Install grandalf to draw graphs`**
`graph.draw_ascii()` needs an optional dependency. `describe_topology` reads the
structural edge set instead, so no drawing library is required.

### The development bug log — Lab 7's fifteen

1. `validation.py` — a check bound directly into the dispatcher while the others
   unpacked the context: `TypeError: 'CheckContext' object is not iterable`.
2. `validation.py` — the same defect in the approval check.
3. `specialists.py` — called a non-existent `ToolResult.failure()`; the API is
   `.unwrap()`, which raises `ToolError` carrying the connector's message.
4. `specialists.py` — `assignment_distribution` dropped `due_date`, so the
   milestone specialist raised `KeyError`.
5. `specialists.py` — every specialist got the same un-merged state, so grading
   never saw the similarity scores and the run escalated while both specialists
   reported `ok=True`.
6. `nodes.py` — `assemble` subscripted a pydantic `LabStep` (`s["step_id"]`)
   instead of using the attribute.
7. `nodes.py` — `ingest` passed a `NotesQuery` where `compute_scope_validation`
   needs a `ScopeQuery`; the models are `extra="forbid"`.
8. Five state keys were undeclared and silently dropped, so the plan locked an
   empty scope and reported three false "out of scope" findings.
9. `assemble` reported ungrounded facts and the plan locked an empty assignment
   scope, together producing 11 drift findings. Now 0, and grounded in
   `GROUNDED_FACT_KEYS`.
10. The fabricated similarity score (section b).
11. `describe_topology` used `draw_ascii()`, which needs `grandalf`; replaced with
    the structural edge set.
12. `resume_run` ignored its `root` argument and wrote into the repository
    instead of beside the checkpoint.
13. The published document contradicted itself (section b).
14. `FacultyDecision` accepted any string, so a typo became a refusal.
15. `context.py` imported a non-existent `SkillRegistry` and an unused symbol.

To which three fixes from this session belong:

16. The retry ordinal baked its spaces into the fragment (`" Attempt 3 "`), so
    every retry diagnosis opened with a space and the escalation rendered a
    double space after *"forever."*. Fixed at the source, with two tests.
17. The checkpointer emitted four `Deserializing unregistered type` warnings per
    resume; the checkpointed types are now declared to LangGraph.
18. The bare `build_graph(ctx)` used only to render topology warned "compiled the
    graph without a checkpointer", training a reader to ignore a warning that
    matters when a graph is about to run. Inspection now goes through
    `inspect_graph` with an explicit in-memory saver.

---

## g) Requirement coverage

| Doc line | Requirement | Met by | Checked by |
|---|---|---|---|
| 686 | Wire the specialists into one explicit graph with state and branching | `build_graph`, `CourseState` | 1, 4 |
| 687 | The complete pipeline, gate, publish, and a regenerate back-edge | compiled topology | 1, 2, 3 |
| 690 | One command yields a validated workspace draft end to end | `run_pipeline`, the CLI | `lab07_cli.txt` |
| 692 | All core nodes plus the back-edge | `compiled_nodes` | 1, 2, 3 |
| 693 | Bound the retry edge | `MAX_ATTEMPTS`, `route_after_validate` | 12, 13, 14 |
| 696 | The four-line graph specification | `builder.build_graph` | 1, 2, 3 |
| 558 | A draft grade never exceeds the rubric maximum | `check_draft_grade_within_rubric_maximum` | 6 |
| 558 | Late submissions flagged accurately | `check_late_submissions_flagged_accurately` | 6 |
| 558 | A plagiarism/similarity score is attached | `check_similarity_score_attached` | 6, 10 |
| 558 | No grade published without faculty approval | `check_no_grade_published_without_faculty_approval`, `publish` | 7, 16, 17 |
| 825 | On exhaustion, escalate with a diagnosis | `escalate`, `build_escalation` | 14, 15 |
| 830 | Never publish without faculty approval | `publish` re-checks | 16, 17 |
| 832 | Never adjudicate similarity — always flag | flags only; no penalty | 10 |
| 834 | Full audit log of every published grade and approver | `audit_trail`, approval fields | 15, 16 |
| 515–523 | The six Phase-A specialists in roster order | `SPECIALIST_NAMES` | `test_there_are_six_specialists_in_doc_roster_order` |

---

## h) Report and documentation text

### Summary

Lab 7 wires the Lab 1–6 components into one explicit LangGraph state machine and
runs it end to end. The compiled graph is the seven nodes named at doc §2.10 plus
an `escalate` node for the case the pitfall describes: an exhausted retry budget.

One command — `python -m course_organizer.labs.lab07_node_graph` — produces a
validated organised course workspace draft and stops at a real human gate
implemented with LangGraph's `interrupt()` / `Command(resume=…)`. The draft is
produced, validated against doc §2.7's four checks, and suspended; nothing is
published until a faculty decision is resumed into the graph, and a refusal
publishes nothing.

The lab is 4,295 lines (a 3,720-line `graph/` package in nine modules, plus a
575-line CLI) and 133 test cases, run with no API key and no network.

### What was measured

Every number in this document comes from a captured run:

* Clean run: 5 nodes, 16 obligations locked, plan `sha256=<captured>`,
  **0 drift findings**, 8 draft grades, 2 similarity flags, no published file.
  (The digest is stable for a given case; the captured value is in
  `docs/evidence/lab07_cli.txt`, quoted there rather than here as an invariant.)
* The similarity measure on the seed corpus: `DS-A1-S001` and `DS-A1-S003`
  score **1.000** against each other (a genuine near-duplicate differing only in
  hyphenation), `DS-A1-S002` at 0.006, `DS-A1-S004` at 0.000, and the remaining
  submissions at or below 0.023. Flag threshold 0.30.
* Retry: 9 nodes; `workers` and `validate` each executed 3 times; passes
  `[1, 2, 3]`.
* Exhaustion: 9 nodes; `retry_budget_exhausted` after 3 of 3 permitted attempts;
  the escalation carries 2 failing checks and all 8 draft grades.
* Refusal: 7 nodes; `status=refused`; 0 files published.
* Cross-process resume: the gate request (`8` grades) was read from SQLite by a
  separate interpreter, a decision resumed, and `ingest` executed **once** — the
  pre-gate nodes did not re-run.
* Published document: 4,695 characters, pure ASCII, containing
  `## Approval Recorded`, `Published: yes`, and the approver's name.
* Full suite: 681 passed, 1 skipped.

### Honest limitations

* **The backend is scripted.** `LLM_BACKEND=scripted` is the default and no
  `GOOGLE_API_KEY` is set, so the free-text feedback a live model would write is
  produced deterministically. The graph, the validation, the gate and the
  checkpointer are all exercised for real — only the prose is scripted.
* **The similarity measure is lexical.** Trigram Jaccard finds verbatim reuse and
  near-duplicates; it will not find paraphrasing or translation. This is
  deliberate: it measures and flags, and never adjudicates, so its limits are the
  human reviewer's problem to resolve, not an accusation to defend.
* **`workers` is sequential.** The six specialists run one after another. Parallel
  fan-out and the comparison of strategies are Lab 8's subject.
* **The published grades in this lab are computing-assisted, not final.** The
  gate returns `approve | edit | override | refuse`; an approval publishes the
  reviewed draft, and an edit or override changes marks before publication. The
  human remains the authority, as §2.12 requires.

### Reproducing this

```powershell
# the whole lab, self-checking: prints 18/18 and exits 0
python -m course_organizer.labs.lab07_node_graph

# the lab's own tests
python -m pytest tests/test_lab07_graph.py tests/test_lab07_runner.py

# the whole project
python -m pytest

# the notebook, executed
python -m jupyter nbconvert --to notebook --execute --inplace notebooks\lab07_node_graph.ipynb
python tools/nb_to_text.py notebooks\lab07_node_graph.ipynb docs\evidence\lab07_notebook_output.txt
```
