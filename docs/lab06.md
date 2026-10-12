# Lab 6 — Governed runtime: budgets, gates, checkpoint/resume, audit log
**Phase:** A — Foundations
**Goal (doc §2.10, Lab 6):** *"Run the agent inside a controlled runtime with a full audit log."*
**What it adds (doc §2.10):** *"A runtime binding skills + tools + memory behind the loop, enforcing budgets and approval gates and logging every step — the backbone later labs plug into."*
**Deliverable (doc §2.10):** *"Runtime wrapping the agent: budgets, gates, checkpoint/resume, audit log."*
**Accept (doc §2.10):** *"Every action is logged with inputs/outputs; a crashed run resumes from its last checkpoint."*
**Nodes activated (doc §2.10):** *"Coordinator runtime around all nodes."*
**Pitfall (doc §2.10):** *"Log inputs and outputs, not just messages; checkpoint before risky steps or resume loses state."*

---

## a) Goal and the document's requirements

The document's requirements for Lab 6 are on lines 671–683 of the extracted
Worklet 2 text. Nothing below is inferred beyond those lines, plus §2.3's
governance clause and the §2.12 guardrails that apply to every lab.

| # | Doc requirement (verbatim) | Where it is met |
|---|---|---|
| R1 | *"Goal: Run the agent inside a controlled runtime with a full audit log."* | `runtime/runtime.py::Runtime.run` is the only entry point to a plan; every action goes through `execute_step`, which is the only place a step body is called. |
| R2 | *"A runtime binding skills + tools + memory behind the loop"* | `KIND_DIMENSION` maps a step's `kind` (`skill`, `tool`, `connector`, `memory`, `llm`) to a budget dimension, and `build_demo_plan()` spans four of the five. One loop, five kinds. |
| R3 | *"enforcing budgets …"* | `runtime/budget.py::BudgetLedger`. `reserve()` checks **and** commits, so a refusal happens before the action rather than after it. 8 dimensions in `DEFAULT_LIMITS`. |
| R4 | *"…and approval gates"* | `runtime/gates.py`: an `ApprovalGate` protocol with `request(req) -> ApprovalDecision`, and five implementations — `PreapprovedGate`, `DenyAllGate`, `ScriptedGate`, `CallbackGate`, and `RecordingGate`, which wraps another gate and records what it was actually asked. Three outcomes, not two. |
| R5 | *"…and logging every step"* | `runtime/audit.py::JsonlAuditLog`. One `AuditRecord` per step, appended with `O_APPEND`, hash-chained, and `inputs`/`outputs` are **required fields on the schema** rather than a message. |
| R6 | *"Deliverable. Runtime wrapping the agent: budgets, gates, checkpoint/resume, audit log."* | The package `course_organizer/runtime/` — 7 modules, 3,775 lines, 51 exported names — plus a 1,053-line lab module. 4,828 lines in total. |
| R7 | *"Accept: Every action is logged with inputs/outputs"* | Acceptance checks 3, 4, 5 and 14. Check 3 requires 5/5 `ok` records to carry **both**; check 5 requires that no record is a bare message; check 14 requires a *refused* action to be on the log too, with the approver's identity. |
| R8 | *"Accept: … a crashed run resumes from its last checkpoint."* | Acceptance checks 6–10, produced by **two real OS processes**: the first calls `os._exit(9)` mid-plan, the second resumes. Exit code 9 is asserted, not simulated. |
| R9 | *"Pitfall … Log inputs and outputs, not just messages"* | `AuditRecord.inputs` and `AuditRecord.outputs` are required `Mapping` fields, so a record without them **cannot be constructed** — `build_audit_record()` computes the digest, and a hand-built record fails validation. Check 5 names the property. |
| R10 | *"Pitfall … checkpoint before risky steps"* | `RUNTIME_ORDER[2]` is the checkpoint, `RUNTIME_ORDER[3]` the approval. `execute_step` writes the state before `_consult_gate`. Check 15 reads it back off the audit record's `checkpoint_id`. |
| R11 | *"…or resume loses state"* | `RunState` carries `cursor`, `completed`, `artifacts`, `approvals`, `spent`, `audit_seq` + `audit_digest`, and `plan_sha256`. §3.3 of the notebook tabulates what each one losing actually costs. |
| R12 | `run = Runtime(budget=B, gates=[publish])` | `Runtime(budget=..., gates=...)`; the default gate is named `DEFAULT_GATE = "publish"`. |
| R13 | `for step in agent.loop():` | `Runtime.run(plan)`; `agent.loop()` becomes the plan. |
| R14 | `audit.log(step.intent, step.action, step.result)` | `AuditRecord.intent` / `.action` / `.outputs` — checked on 5/5 records by check 4. |
| R15 | `if step.risky: wait_for_approval(step)` | `Step.risky` + `Step.gate`; `ApprovalGate.request()` is the wait. |
| R16 | `checkpoint(run.state)` | `SqliteCheckpointStore` on `langgraph-checkpoint-sqlite==3.1.1`, so the checkpoint table is the same one Lab 7's graph will use. |
| R17 | §2.12 *"Bound MAX_ATTEMPTS …; on exhaustion, escalate to course faculty with a diagnosis."* | `BudgetExceeded` before the action, `RunReport.escalated=True`, and `RunReport.diagnosis` — a sentence naming the step, the dimension, the counts, and what to do. |
| R18 | §2.12 *"Keep a full audit log of every grade published and who approved it."* | The chain. Three distinct tampers (edit, remove, forge) are each detected, and the forge is refused **at append**, before it is written. |
| R19 | §2.12 *"Never publish a grade or feedback without faculty approval."* | The refusal demo: `publish` produced no output, no `ok` record, and the run stopped. The refusal itself is on the log with `decided_by=course_faculty`. |
| R20 | Glossary: *"Coordinator | The orchestrator agent that owns run-state, plan, budgets and the audit log."* | The `Runtime` is the Coordinator's runtime, and it owns all four. The principal on every record is the Coordinator, set by the runtime so a step cannot log itself as someone else. |

### Where the lab's own choices are labelled as such

Four things here are **not** the document's, and each says so:

* **The eight budget dimensions and their ceilings** — the document says
  *"enforcing budgets"* without naming a resource. `DEFAULT_LIMITS` is one table,
  and `test_defaults_matches_the_published_table` is what keeps the doc and the
  code agreeing.
* **`wall_clock_ms` as a dimension** — a *post-hoc* check rather than a
  reservation, because you cannot know a step's duration in advance. It exists so
  a runaway loop has a ceiling that is not a count of anything.
* **The three approval outcomes** — `("approved", "refused", "deferred")`. Two
  would have been easier and wrong: "deferred" is *not now, and not no*, and
  `UNANSWERED` is a fourth state that is treated as a refusal, the safe direction.
* **`RuntimeBoundaryError` on a fresh run pointed at an occupied thread** — not a
  document requirement, and it exists because the alternative was a confusing
  error (bug 36 below).

---

## b) Brief concepts

### The document's pseudocode and its pitfall disagree. The pitfall is the correction.

```
DOC (line 682):                       RUNTIME (RUNTIME_ORDER):
  audit.log(...)          <- 1st       reserve budget (refuse before the action)
  if risky: approval      <- 2nd       check the wall clock for an overrun already spent
  checkpoint(state)       <- 3rd       if risky: checkpoint(state)   <- before
                                         if risky: wait_for_approval()
                                         run the step
                                         charge what it actually cost
                                         audit.log(inputs, outputs)  <- inputs AND outputs
                                         checkpoint(state)
```

The document's own order is wrong twice, and the pitfall printed directly beneath
it says so both times. Logging before running means the log records an *intent*,
which is not the acceptance criterion. Checkpointing after the gate means a crash
inside `wait_for_approval()` resumes not knowing the risky step was ever reached.

So `RUNTIME_ORDER` is the specification and `DOC_PSEUDOCODE` is kept verbatim
beside it, and a test asserts the two differ in exactly the two places the pitfall
names. Printing both, rather than quietly implementing the correct one, is
deliberate: a reader can see that the correction came from the document.

### A budget is a reservation, not a report

`reserve()` checks **and commits** in one step, and it runs *before* the step
body. `charge()` exists for the things that are only knowable afterwards —
`records_read` from the connector's answer, `wall_clock_ms` from a clock.

The first version of this had `reserve()` check and `charge()` commit, which is the
more conventional shape and was **wrong**: the runtime reserved, then the step ran,
then the charge landed, and every dimension in every report read `0`. The budget
could never refuse anything, so the whole feature was a set of comments. It is bug
32 and it is the most instructive failure in this lab, because the code looked
correct and read correctly.

### A refusal that never ran is not a record of an action

A gate refusal **is** on the audit log — with empty `outputs`, the approver's
identity, the reason, and the approval request itself in `inputs`. A budget
refusal is **not**, and the asymmetry is the point:

* the gate was asked. Something happened: a human was presented with a question
  and answered it. §2.12 wants *"who approved it"*, and a refusal answers the same
  question with the opposite sign.
* the budget was checked by the runtime before anything was attempted. A record
  claiming an action would be a lie, and §2.12's *"full audit log of every grade
  published"* would become unanswerable — the count of records would no longer be
  the count of things done.

The refusal is still fully accounted for, in the three places that *can* hold it:
`RunReport.diagnosis`, `BudgetLedger.refused`, and the `runtime.run_stopped`
structured log event. "Not on the audit log" and "not recorded" are different
claims, and only the first is true.

### Checkpoint ids must sort, because the query is a string compare

LangGraph's `SqliteSaver.get_tuple` is `ORDER BY checkpoint_id DESC LIMIT 1`. That
is a **string** comparison, so "the latest checkpoint" is only the latest if the
ids happen to sort in write order. The first version derived the id from the audit
digest, and `load()` returned an arbitrary checkpoint from a five-step run — every
time, because hex digests are uniformly distributed and the order is random.

Ids are now `f"{write_seq:012d}-{digest[:12]}"`, with the ordinal allocated from
the SQLite table rather than from `RunState`. `WRITE_SEQ_WIDTH` is exported so the
layout is a checked fact and not a comment:

```
make_checkpoint_id(1,            "a1b2c3d4e5f6") = 000000000001-a1b2c3d4e5f6
make_checkpoint_id(12,           "a1b2c3d4e5f6") = 000000000012-a1b2c3d4e5f6
make_checkpoint_id(1000000000,   "a1b2c3d4e5f6") = 001000000000-a1b2c3d4e5f6
```

### The resume key is a name, and a name can be reused by mistake

`thread_id` is the resume key. A fresh run pointed at a thread that already holds
a checkpoint would start a new `RunState` — `audit_seq=0`, `GENESIS_DIGEST` — and
append to a log that already held five records. The chain check then fires on
record 1, naming the *audit log* as the thing that went wrong, when the log was the
one thing that had done nothing wrong.

Found by running `run_lab06` twice into the same directory, which is exactly what
the notebook does deliberately. Two fixes: each invocation gets its own
`run-<8 hex>` subdirectory, and `governed_run` refuses the ambiguous case by name:

```
RuntimeBoundaryError: thread 't' already holds a checkpoint under …\t.sqlite.
Pass resume=True to continue that run, or a different thread to start a new one.
A fresh run cannot share a resume key with an existing one.
```

### "Escaped" is not a real place in a running process

The crash proof calls `os._exit(9)` inside a step body. No exception, no
`finally`, no `atexit`, no flush of anything the runtime did not flush itself. The
child is a **written script on disk** (`_lab06_crash_child.py`, readable in the
notebook) run via `argv`, not an in-process simulation, because the claim is about
surviving *process death* and `InMemoryCheckpointStore` cannot demonstrate it.

The crash is injected into `report`, the **last** step. That is the only placement
that shows the property: a crash after the risky publish is what proves the
resume neither repeats it nor re-asks about it.

---

## c) The code

### Files

| File | Lines | What it owns |
|---|---:|---|
| `src/course_organizer/runtime/__init__.py` | 166 | 51 re-exports; one import site for the lab and the tests |
| `src/course_organizer/runtime/errors.py` | 248 | 6 error types, one base + five subclasses |
| `src/course_organizer/runtime/budget.py` | 409 | `Budget`, `BudgetLedger`, `BudgetSnapshot`, `DEFAULT_LIMITS` |
| `src/course_organizer/runtime/audit.py` | 619 | `AuditRecord`, `JsonlAuditLog`, `InMemoryAuditLog`, the chain |
| `src/course_organizer/runtime/checkpoint.py` | 602 | `RunState`, `SqliteCheckpointStore`, `InMemoryCheckpointStore`, id layout |
| `src/course_organizer/runtime/gates.py` | 494 | `ApprovalRequest`/`ApprovalDecision`, the protocol, 5 implementations |
| `src/course_organizer/runtime/runtime.py` | 1237 | `Step`, `StepContext`, `StepOutcome`, `RunReport`, `Runtime` |
| `src/course_organizer/labs/lab06_governed_runtime.py` | 1053 | the 5-step demo plan, 4 demonstrations, 16 acceptance checks, the CLI |
| | **4,828** | |

Plus two test files: `tests/test_lab06_runtime.py` (950 lines, 84 tests) and
`tests/test_lab06_lab_module.py` (1,181 lines, 86 tests) — **170 tests**, and
`notebooks/lab06_governed_runtime.ipynb` (64 cells: 28 markdown, 36 code, all 36
executed with 0 error outputs).

### The error taxonomy, and why it is six types

```python
RuntimeBoundaryError(RuntimeError)     # "the runtime refused or could not continue"
├── BudgetExceeded                      # capacity: the Coordinator's own limit
├── GateRefused                         # policy:  a human declined to authorise
├── CheckpointUnavailable               # nothing to resume from
├── ResumeDivergence                    # the thing resumed is not the thing resumed
└── AuditIntegrityError                 # the log no longer proves what it claims
```

`BudgetExceeded` versus `GateRefused` is the distinction that matters most, and it
is not a style choice. Running out of budget is a normal, expected outcome to
report to a human; a gate refusal is a person declining a consequential action and
is never an error to retry silently. `Runtime.run()` treats the two identically
(both stop the run) but *reports* them differently, and only a budget exhaustion
sets `escalated`:

```
-- budget --
status    : stopped
stopped_at: publish
escalated : True
diagnosis : budget refused step 'publish': budget 'steps' exhausted before step
            'publish': 3 of 3 spent, and the next action requests 1. … Nothing
            ran, so there is nothing to undo. Raise the ceiling deliberately or
            shorten the plan; do not lower MAX_ATTEMPTS to get past it.

-- gate --
status    : stopped
stopped_at: publish
escalated : False
diagnosis : step 'publish' was refused at its gate and did not run. step
            'publish' held at the 'publish' gate by course_faculty and was not
            run. Reason: the CS201 rubric needs a second look before grades go
            live …
```

§2.12 asks for an escalation *"with a diagnosis"*, which is a sentence somebody
who was not watching can act on — the step, the dimension, the counts, and the
two legitimate responses. `Runtime.report()` is a mid-run snapshot and therefore
carries no diagnosis and no `stopped_at`; inventing a verdict there would put a
plausible-sounding excuse on every clean run.

### `RUNTIME_ORDER` — the order, and where each correction is marked

```python
RUNTIME_ORDER: tuple[str, ...] = (
    "reserve budget (refuse before the action)",
    "check wall clock for an overrun already spent",
    "if step.risky: checkpoint(run.state)  <- pitfall: before, not after",
    "if step.risky: wait_for_approval(step)",
    "run the step",
    "charge what it actually cost",
    "audit.log(inputs, outputs)  <- pitfall: inputs AND outputs",
    "checkpoint(run.state)",
)
```

This tuple is documentation that is also an assertion:
`test_the_runtime_order_applies_both_pitfall_corrections` checks the checkpoint
precedes the approval and that the audit line names both halves. `execute_step()`
is the only method that calls a step body, and its docstring repeats the same
eight lines in prose — so a change of order has to be made in two places that
disagree visibly.

### The budget table

```python
DEFAULT_LIMITS = {
    "steps": 24,            "risky_steps": 4,
    "tool_calls": 12,       "skill_calls": 8,
    "connector_calls": 12,  "llm_calls": 8,
    "records_read": 200,    "wall_clock_ms": 120000,
}
```

`KIND_DIMENSION` maps a step's declared `kind` to one of the call dimensions —
`{"tool": "tool_calls", "skill": "skill_calls", "connector": "connector_calls",
"llm": "llm_calls", "memory": "skill_calls"}` — so a step cannot choose which
budget pays for it. `memory` maps to `skill_calls` because memory work in this
system *is* a skill call; the merge is asserted rather than left as a comment.

A real snapshot from the clean run:

```
connector_calls          1/12  refused=0
steps                    5/24  refused=0
records_read           16/200  refused=0
wall_clock_ms        5/120000  refused=0
skill_calls               1/8  refused=0
llm_calls                 1/8  refused=0
tool_calls                2/12  refused=0
risky_steps               1/4  refused=0
```

`records_read` is 16, not 8: the step *declares* `cost={"records_read": 8}` and the
runtime charges what the step returned. `wall_clock_ms` is 5, not 0: the runtime
charges `math.ceil(duration_ms)`, so a step that takes 0.4 ms costs 1 ms. A
dimension that rounds to zero never moves, and a budget that never moves is
decoration.

### `AuditRecord` — the schema that cannot be built wrong

```python
class AuditRecord(BaseModel):
    seq: int
    run_id: str
    thread_id: str
    step: str
    intent: str
    action: str
    kind: str
    inputs: Mapping[str, Any]     # required
    outputs: Mapping[str, Any]    # required
    outcome: str
    error: str = ""
    risky: bool = False
    gate: str = ""
    decided_by: str = ""
    reason: str = ""
    principal: str = ""
    role: str = ""
    token_fingerprint: str = ""
    budget_spent: Optional[Mapping[str, int]] = None
    checkpoint_id: str = ""
    duration_ms: float = 0.0
    at: datetime = ...
    digest: str                   # required — and it is derived
    prev_digest: str              # required
```

A frozen model cannot compute a digest over its own content at construction, so
`build_audit_record()` builds the content, computes the digest, and returns the
finished model. The consequence is worth stating because it is the whole
enforcement of the pitfall: **`AuditRecord(**fields)` without a digest does not
construct.** The notebook hits this — its first draft built a record directly and
died with a pydantic `ValidationError` naming `digest` and `prev_digest`. A log
that *can* be built with a hand-written digest is a log that can be built with a
wrong one.

Two real records from the clean run, verbatim. Read them for **shape**: every
action carries its `inputs`, its `outputs`, who did it and on whose authority.
The `duration_ms`, `wall_clock_ms`, `checkpoint_id` suffix and `digest` values are
measurements and derived digests, so they differ on your machine — see the note
on fingerprints below (bug 37).

```json
{
  "seq": 1, "run_id": "lab06-clean", "thread_id": "clean",
  "step": "read_notes",
  "intent": "Read the notes store through the connector, not from memory",
  "action": "read_notes_store", "kind": "connector",
  "inputs":  { "prior_artifacts": [] },
  "outputs": { "course_code": "CS201", "records": 8,
               "titles": ["Trees", "Graphs", "Hashing", "Heaps"] },
  "outcome": "ok", "error": "", "risky": false,
  "principal": "apniLeap AURA Coordinator",
  "budget_spent": { "connector_calls": 1, "records_read": 16,
                    "steps": 1, "wall_clock_ms": 1 },
  "checkpoint_id": "", "duration_ms": 0.002,
  "at": "2026-10-01T15:57:45.627750Z",
  "digest": "db0033062eade470", "prev_digest": "0000000000000000"
}
```

And the refusal, which is on the log with the approver named and the outputs
empty — because it produced nothing. Its `duration_ms` is `0.0`, which is
deterministic: a refused step never ran.

```json
{
  "seq": 4, "step": "publish", "action": "publish_workspace", "kind": "tool",
  "inputs": {
    "approval_request": {
      "gate": "publish", "step_index": 3,
      "summary": "Publish 3 draft grades to the faculty-visible gradebook. This is irreversible without a new version."
    },
    "prior_artifacts": ["draft_grades", "draft_plan", "read_notes"]
  },
  "outputs": {},
  "outcome": "refused",
  "error": "the CS201 rubric needs a second look before grades go live",
  "risky": true, "gate": "publish",
  "decided_by": "course_faculty",
  "principal": "apniLeap AURA Coordinator",
  "budget_spent": { "connector_calls": 1, "llm_calls": 1, "records_read": 16,
                    "risky_steps": 1, "skill_calls": 1, "steps": 4,
                    "tool_calls": 1, "wall_clock_ms": 3 },
  "checkpoint_id": "000000000004-b98b144c8462", "duration_ms": 0.0,
  "digest": "936ecdf47e3d4072", "prev_digest": "b98b144c84626c7b"
}
```

`inputs["approval_request"]` is the question the human was asked, verbatim. §2.12
wants *"who approved it"*; a log that recorded only the verdict could not answer
*what* was authorised, and a re-approval of something different would be
indistinguishable from this one.

### The chain, and the three tampers it catches

```python
def chain_digest(record, previous):
    hasher = hashlib.sha256()
    hasher.update(previous.encode("ascii")); hasher.update(b"\x00")
    hasher.update(canonical_json(record).encode("utf-8"))
    return hasher.hexdigest()[:DIGEST_HEX]     # DIGEST_HEX = 16
```

`canonical_json` uses `sort_keys=True, separators=(",", ":")`. The key sort
matters across processes: the same logical record built twice could otherwise
serialise in a different order, hash differently, and make an honest log look
tampered with. The timestamp `at` is **excluded** from the digest, so a replay
does not break the chain and a genuinely re-executed step can be told from an
edited one.

Every tamper gets the same three-part message and the same closing sentence, so
only the head differs. `<...>` marks a digest that depends on the run; the
sentence after it is identical in all three cases:

> `Doc 2.12 requires a full audit log of what was published and who approved it; a
> log that fails its own integrity check cannot be relied on for that.`

| tamper | head of the message |
|---|---|
| edit an output | `audit chain broken at record 1: the record's content does not match its digest. Expected digest <...>, log carries <...>.` |
| remove a record | `audit chain broken at record 3: the chain does not link to the preceding record; a record was removed, reordered or inserted. Expected digest <...>, log carries <...>.` |
| forge a `prev_digest` | `audit chain broken at record 2: refused at append: prev_digest does not match the log. Expected digest <...>, log carries <...>.` |

Note which record each names. The edit is caught at record 1, because that is the
record whose content no longer matches. A removal is caught at the record *after*
the gap (record 3 here), because that is the first record whose link is wrong.
The chain therefore tells you *where the link broke*, never *how many records went
missing*.

### The one tamper the chain alone cannot see

Drop the **last** record and `verify()` returns `True`. Nothing is inconsistent:
the remaining chain still links, and a hash chain has no anchor for "there should
have been one more".

That is not a gap in the system, because the system does not rely on the chain
alone. The checkpoint stores `audit_digest` and `audit_seq` — *where the chain
left off* — so `Runtime.resume()` cross-checks the log's last digest against the
checkpoint's, and a truncated tail is caught there:

```
resumed run 't' diverged from the audit log the checkpoint was written against at
'audit_digest': expected '<the digest the checkpoint recorded>', got '<the digest
the truncated log ends on>'. ...
```

The `audit_seq` variant fires when the *counts* disagree rather than the digests;
`_assert_same_log` names the field that differs, so the operator is told whether to
look for a missing tail or a wrong file.

So the honest statement of the audit guarantee is two-part: **the chain proves
that no record before the break was edited; the checkpoint proves that no record
after the break was removed.** Neither alone is enough, which is exactly why
Lab 6 refuses to treat the log as its own source of truth for position.
`test_a_truncated_tail_passes_the_chain_and_is_caught_on_resume` holds both
halves at once.

The third is caught **before the write**. `JsonlAuditLog.append()` validates
`prev_digest` against the log's tail and refuses, and the file's size afterwards is
unchanged — the notebook prints that byte count to show the forgery was never
stored. A log that will accept a forged record is not evidence.

### `RunState` — five fields, and a table of what each one costs

```python
thread_id, run_id, course_code, plan_sha256, step_index, cursor,
completed, artifacts, approvals, spent, audit_seq, audit_digest, updated_at
```

| drop it and… |
|---|
| `cursor` | the resume cannot say which step it is on |
| `completed` | completed steps re-run — **a publish happens twice** |
| `artifacts` | the resumed step sees an empty workspace |
| `spent` | the resumed run can overspend, because it cannot see the past |
| `audit_seq` + `audit_digest` | the resumed run re-appends at an existing sequence number and the integrity check fires on an **untampered** log |

That last row is bug 33 and it is the least obvious of the five. A run that
*fails* audits its failing step and then must checkpoint the new chain position;
without that, the next append reuses a sequence number the log already holds. A
failed step that was audited but not checkpointed produced a log that looked
tampered, which is the worst possible failure direction for an integrity check.

`cursor` stores the **next** step, not the current one, and a blank cursor is
rejected as indistinguishable from "the run finished": a crash after step 3 of 5
would otherwise resume as a complete run. `plan_sha256` and `course_code` are what
`ResumeDivergence` checks before adopting a checkpoint, so resuming the wrong
thread or the wrong plan is refused rather than merged.

### `SqliteCheckpointStore` — one session owns the connection

```python
with store.session():
    latest = store.load("demo")
    for ref in store.history("demo"):   # newest first, with write_seq
        …
    store.require("never-seen")         # CheckpointUnavailable
```

Using the store outside its session is a named error rather than a leak:

```
RuntimeError: SqliteCheckpointStore used outside session(). Open it with
'with SqliteCheckpointStore(path).session() as store:' -- the sqlite3
connection is owned by the session, and using the store outside it would
leave the file open to garbage collection.
```

The table is `langgraph`'s own, through `langgraph-checkpoint-sqlite==3.1.1`, so
Lab 7's graph reads what Lab 6 wrote. Methods: `count`, `delete_thread`,
`durable`, `exists`, `history`, `load`, `path`, `require`, `save`, `session`,
`thread_ids`.

### The gates

```python
APPROVAL_OUTCOMES = ("approved", "refused", "deferred")
UNANSWERED = "unanswered"          # treated as a refusal — the safe direction
```

`ScriptedGate` replays a list of decisions in order and raises
`GateScriptExhausted` rather than inventing a fourth answer.
`CallbackGate`'s `on_error` policy is `"defer"` by default: a gate whose callback
crashes must not approve, and it must not crash the run either. A callback
returning the wrong type is a `TypeError`, and an unknown `on_error` policy is a
construction-time `ValueError`.

The runtime sets the `principal` and the `role` on every record, not the step —
`test_the_runtime_sets_the_principal_not_the_step` — because a step that could
name itself in an audit log could log itself under someone else's identity.

### The crash proof, as a written child program

```python
# _lab06_crash_child.py, written to disk and read back in the notebook
phase, root, thread = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
plan = build_demo_plan(crash_at="report" if phase == "crash" else "")
audit = JsonlAuditLog(root / f"{thread}.jsonl", run_id=phase)
store = SqliteCheckpointStore(root / f"{thread}.sqlite", run_id=phase)
runtime = Runtime(budget=Budget.defaults(), gates={"publish": gate},
                  audit=audit, checkpoints=store, run_id=phase,
                  thread_id=thread, course_code="CS201",
                  plan_sha256=plan_digest(plan))

with store.session():
    if phase == "crash":
        runtime.resume()          # adopts the state this process is about to build
        runtime.run(plan)         # os._exit(9) inside `report`, never returns
        print("CHILD=crash-returned-unexpectedly")
    else:
        runtime.resume()
        report = runtime.run(plan)
        print(f"STEPS_RUN={','.join(report.steps_run())}")
        print(f"SKIPPED={','.join(report.skipped)}")
        print(f"GATE_ASKED={','.join(gate.steps_asked())}")
```

Two processes, two `run_id`s (`crash` and `resume`), one `thread_id`, one audit
log, one checkpoint file. The `print("CHILD=crash-returned-unexpectedly")` is the
line that would only run if the crash did not happen — a child that survives its
own proof has to be able to say so.

`Runtime.resume()` restores three things — the **cursor**, the **budget spend**,
and the **audit chain position** — and the third is the one that is easy to miss.
Restoring the cursor without the spend lets a resumed run overspend, because the
crashed process's spending is invisible to it. Restoring the spend without the
chain position starts a second hash chain in the same file. Both failures are
silent and both leave a log that looks complete. `Runtime.resumed_from` names the
`CheckpointRef` it adopted, and `_assert_same_log` verifies the log it is about to
append to is the log that checkpoint was written against — otherwise resume says
"the audit log is not this run's", which was bug 34's confusing message.

---

## d) Setup and run

### Environment (already provisioned)

```powershell
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
```

Lab 6 adds one pin, in `requirements.txt`:

```
langgraph-checkpoint-sqlite==3.1.1   # durable checkpoints, resume after a real crash
```

```powershell
& $py -c "from importlib.metadata import version; print(version('langgraph'), version('langgraph-checkpoint'), version('langgraph-checkpoint-sqlite'), version('langchain-core'))"
# 1.2.12 4.2.0 3.1.1 1.6.6
```

Lab 6 makes **no model calls**. It runs on the `scripted` backend like every other
lab, and needs no API key.

### Run the lab

```powershell
& $py -m course_organizer.labs.lab06_governed_runtime
```

Expected: six sections, **16/16 `[PASS]`**, exit code **0**. This is the shape of
the output; the three values marked `<varies>` below are digests and timings that
cannot recur, so they are shown as placeholders rather than as expected values:

```
-- Uninterrupted governed run --
  status      : completed
  steps       : ('read_notes', 'draft_plan', 'draft_grades', 'publish', 'report')
  gate answers: ('publish: approved by course_faculty -- demo approval',)
  audit       : 5 record(s), fingerprint <varies>
  checkpoints : 6

-- Gate refusal (publish is a hard stop) --
  status      : stopped
  steps       : ('read_notes', 'draft_plan', 'draft_grades')
  publish ran : False
  gate answer : ('publish: refused by course_faculty -- the CS201 rubric needs a second look before grades go live',)

-- Budget refusal (steps=3) --
  status      : stopped
  steps       : ('read_notes', 'draft_plan', 'draft_grades')
  escalated   : True

-- Crash and resume (two real processes) --
  crash exit=9 (expected 9)
    resumed ran    : ('report',)
    resumed skipped: ('read_notes', 'draft_plan', 'draft_grades', 'publish')
    publish reran  : False; gate asked again: False
    ok records with inputs+outputs: 5; missing: 0
    audit chain    : 5 record(s), verified=True, fingerprint <varies>
```

Full transcript with all sixteen checks: `docs/evidence/lab06_cli.txt` (exit 0).

Everything printed in that block is deterministic **except** the fingerprints and
the `wall_clock_ms` total. A fingerprint is a digest over the chain's record
digests, and a record's digest covers every field **except** its timestamp — so two
runs of the same plan agree exactly when they measured the same step durations,
and these steps take single-digit milliseconds. Measuring a tie is therefore
ordinary, not a collision: `JsonlAuditLog.fingerprint` promises *"two logs with the
same fingerprint contain the same chain of content"*, and two runs that did the
same work for the same measured cost did produce the same chain.

Getting that wrong was bug 37 (§f). What is asserted is the **shape**: 5 records,
one chain, `verified=True`, and the same fingerprint read back from the file.

### The 16 acceptance checks

```
  [PASS]  1. clean_run_completes_every_step
  [PASS]  2. the_publish_step_is_the_gated_one
  [PASS]  3. every_action_logged_with_inputs_and_outputs
  [PASS]  4. every_record_carries_the_document_triple
  [PASS]  5. the_log_is_not_merely_messages
  [PASS]  6. crash_really_killed_the_process
  [PASS]  7. resume_runs_only_the_unfinished_step
  [PASS]  8. resume_does_not_repeat_the_risky_publish
  [PASS]  9. resume_does_not_re_ask_the_gate
  [PASS] 10. audit_chain_survives_the_crash
  [PASS] 11. budget_is_enforced_before_the_action
  [PASS] 12. budget_refusal_stops_before_the_over_limit_step
  [PASS] 13. gate_is_a_hard_stop
  [PASS] 14. refusal_is_logged_with_who_and_what
  [PASS] 15. checkpoint_written_before_the_risky_step
  [PASS] 16. audit_chain_verifies
```

Check 15 is the one that reads the pitfall off the evidence rather than trusting
the code's own ordering:

```
  1 gated record(s), each naming the pre-step checkpoint:
  ['000000000004-caee53ccef93']; 6 checkpoint(s) written by the run
```

A `checkpoint_id` on the `publish` record can only be there if the write happened
before the gate was consulted.

### Write the evidence somewhere you can look at it

```powershell
& $py -m course_organizer.labs.lab06_governed_runtime .\lab06-evidence
# writes clean.jsonl / clean.sqlite / gate-*.jsonl / budget-*.jsonl /
# crash-*.jsonl / crash-*.sqlite under a run-<8 hex> subdirectory
```

Each invocation gets its own `run-<8 hex>` directory, so running the lab twice
into the same base cannot merge two runs' audit chains.

### Run the notebook

```powershell
& $py -m jupyter lab notebooks\lab06_governed_runtime.ipynb
```

Executed with `nbconvert --execute --inplace --ExecutePreprocessor.kernel_name=course-organizer`:
all **36** code cells have outputs, **0** error outputs.

### Run the tests

```powershell
& $py -m pytest tests\test_lab06_runtime.py      -o addopts= -v   # 84 tests
& $py -m pytest tests\test_lab06_lab_module.py   -o addopts= -v   # 86 tests
& $py -m pytest                                                    # 548 passed, 1 skipped
```

> Bare `python -m pytest`, or `-o addopts= -v`. An extra `-q` meets
> `addopts = "-q --strict-markers"` and suppresses the summary line.

---

## e) Test cases, expected output, and proof

### The four demonstrations, and what each must show

| demo | what must be true | measured |
|---|---|---|
| clean governed run | every step runs, the gate is asked once and approves, 5 records, 6 checkpoints | `status=completed`, `5 record(s)`, `checkpoints: 6` |
| gate refusal | `publish` does not run; the refusal is on the log with the approver; the run stops | `steps=('read_notes','draft_plan','draft_grades')`, `publish ran: False`, `decided_by=course_faculty` |
| budget refusal (`steps=3`) | the 4th step is refused **before** its body runs; the run escalates with a diagnosis | `steps spent=3/3, refusals=1, escalated=True`, 3 records and no 4th |
| crash + resume, 2 processes | the child dies for real; the resume runs only the unfinished step; publish does not repeat; the gate is not re-asked; one chain | `exit=9`, `resumed ran ('report',)`, `publish reran: False; gate asked again: False`, `5 record(s) verified=True` |

The crash demo is the acceptance criterion, and it is the only one of the four
that could not have been faked by an in-process test. `test_the_crash_proof_survives_process_death`
runs it; `test_the_child_program_is_readable_on_disk` asserts the child source is
a file a reviewer can open, which is what stops the proof from becoming
`exec("os._exit(9)")` inline.

### The suite

**170 Lab 6 tests across two files. 548 passed, 1 skipped in the whole project.**

```
  tests/test_lab06_runtime.py      80 passed
  tests/test_lab06_lab_module.py   86 passed
```

Full transcript: `docs/evidence/lab06_pytest.txt`. The 1 skip is Lab 4's fastembed
opt-in (`LAB04_TEST_FASTEMBED=1`), not Lab 6's.

### The tests that matter, and what each would have caught

**The order, held structurally rather than by value:**

| test | what it proves |
|---|---|
| `test_the_runtime_order_applies_both_pitfall_corrections` | `RUNTIME_ORDER` puts the checkpoint before the approval and names both halves in the audit line |
| `test_a_risky_step_is_checkpointed_before_the_gate_is_consulted` | the recorded order, not the declared one |
| `test_the_audit_record_names_the_pre_step_checkpoint` | the `checkpoint_id` on the gated record is the *pre-step* one |
| `test_steps_run_in_plan_order` | the loop does not reorder, which would break `cursor` |
| `test_the_gate_is_consulted_before_the_risky_step_runs` | the gate, not the step body, is first |

**Budget as a pre-action reservation:**

| test | what it proves |
|---|---|
| `test_reserve_commits_so_a_lone_call_is_enough` | the regression test for bug 32 — reserve alone must move the ledger |
| `test_reserve_refuses_before_the_ceiling_is_passed` | refusal at the boundary, not over it |
| `test_a_refused_reservation_spends_nothing` | a refusal is not a charge |
| `test_a_step_beyond_the_budget_never_runs` | the step body is not called at all |
| `test_the_refusal_is_not_an_audit_record_because_nothing_acted` | §b's asymmetry, asserted in the direction that is easy to get wrong |
| `test_a_step_is_never_double_charged` | reserve and charge are distinct and neither repeats |
| `test_the_wall_clock_is_charged_at_least_one_ms` | a sub-millisecond step still costs 1 ms, so the ceiling can move |
| `test_memory_work_is_charged_to_the_skill_dimension` | a step cannot pick which budget pays |
| `test_each_kind_is_charged_to_its_own_dimension` | all five kinds map as `KIND_DIMENSION` says |
| `test_a_bool_is_not_accepted_as_a_limit` | `Budget(limits={"steps": True})` is refused, not coerced to 1 |
| `test_defaults_matches_the_published_table` | the code and this document quote the same table |

**The audit log's two halves, and the three tampers:**

| test | what it proves |
|---|---|
| `test_a_record_cannot_be_built_without_inputs_and_outputs` | the pitfall, enforced by the schema — a pydantic `ValidationError` is the refusal |
| acceptance check 5, `the_log_is_not_merely_messages` | no delivered record is a bare message |
| `test_editing_a_record_after_the_fact_is_detected` | tamper A |
| `test_removing_a_record_breaks_the_chain` | tamper B |
| `test_appending_with_a_wrong_previous_digest_is_refused` | tamper C, refused *at append* |
| `test_the_timestamp_is_excluded_from_the_digest` | a replay does not read as tampering |
| `test_canonical_json_is_key_order_independent` | cross-process stability, which the chain depends on |
| `test_a_partial_trailing_line_is_dropped_and_reported` | a torn write is survivable and *said*, not silently swallowed |
| `test_the_log_survives_the_process` | durability, against the real file |
| `test_equal_chains_have_equal_fingerprints` | bug 37's fix — the fingerprint is a *function* of content, so equal chains agreeing is the contract, not a collision |
| `test_a_truncated_tail_passes_the_chain_and_is_caught_on_resume` | the audit guarantee in two halves — the chain cannot see a deleted last record, the checkpoint can |

**Resume:**

| test | what it proves |
|---|---|
| `test_resume_runs_only_the_unfinished_steps` | the cursor means something |
| `test_resume_does_not_repeat_the_gated_step` | `completed`, not "resumed" |
| `test_resume_carries_the_artifacts_forward` | the resumed step sees the earlier steps' outputs |
| `test_a_resumed_run_is_stopped_by_the_dead_runs_budget` | `spent` survives, so resume cannot overspend |
| `test_resume_refuses_a_checkpoint_for_a_different_course` | `ResumeDivergence`, not a merge |
| `test_resume_refuses_a_checkpoint_for_a_different_plan` | `plan_sha256` is checked, not stored decoratively |
| `test_resume_refuses_an_audit_log_that_is_not_this_runs` | bug 34's fix, and the message names the actual problem |
| `test_the_cursor_names_the_next_step_not_a_blank` | a blank cursor would resume as a finished run |
| `test_resume_reports_the_checkpoint_it_started_from` | provenance, for the operator |
| `test_a_fresh_run_on_an_occupied_thread_is_refused` | bug 36's fix, at the runtime level |
| `test_the_refusal_leaves_the_first_runs_log_untouched` | refusing does not damage what it refused to adopt |

**Gates:**

| test | what it proves |
|---|---|
| `test_the_three_outcomes_are_the_documented_three` | `APPROVAL_OUTCOMES`, asserted literally |
| `test_deferred_is_not_a_refusal` | the distinction survives into the log |
| `test_a_failing_callback_defers_rather_than_approving` | a broken gate does not approve |
| `test_an_exhausted_script_fails_loudly` | a script that runs out raises, not guesses |
| `test_a_gate_registered_twice_is_refused` | a `Mapping` **or** a sequence of pairs, because a dict cannot express a duplicate key |
| `test_a_non_gate_under_a_gate_name_is_refused` | the registry is typed, not trusted |
| `test_a_step_naming_an_unregistered_gate_raises` | a typo in a step is a construction error, not a runtime surprise |

### Real bugs this lab found

Thirty-seven in total (31–37 in this lab; the rest in earlier labs). The ones that
would have shipped:

* **31** — checkpoint ids derived from the audit digest, selected by
  `ORDER BY checkpoint_id DESC`. `load()` returned an arbitrary checkpoint. Fixed
  with a zero-padded ordinal prefix allocated from the SQLite table.
* **32** — `reserve()` checked, `charge()` committed. Every dimension in every
  report read `0` and **the budget could never refuse anything**.
* **33** — a failing step was audited but not checkpointed, so the next append
  reused a sequence number. The integrity check fired on an untampered log.
* **34** — `resume()` never checked that the audit log belonged to the checkpoint,
  so a wiring mistake surfaced as *"the audit chain is broken"*.
* **35** — four in one pass: the `Budget` limits validator ran in "after" mode so
  pydantic coerced `True` to `1`; `ScriptedGate.remaining` double-subtracted;
  `InMemoryCheckpointStore` had no `write_seq`; and a generated `run_id` derived
  from `id(self)`, which memory-address reuse could make collide with a resume
  thread.
* **36** — a second `run_lab06` into the same directory raised
  `AuditIntegrityError` on its first append, blaming a log that had done nothing
  wrong. Fixed with a per-invocation `run-<8 hex>` directory plus the
  `RuntimeBoundaryError` guard.
* **37** — `test_the_two_runs_logs_do_not_see_each_other` asserted that two runs
  must produce different fingerprints. A fingerprint is a digest over content, so
  two runs that measured the same durations produce the *same* chain and the same
  fingerprint — correctly. The test asserted the opposite of
  `fingerprint()`'s own contract, and failed only under load, when two
  consecutive runs tied on single-digit millisecond durations. It also hid a false
  claim in this write-up and the README, both of which said the fingerprint
  included the timestamp; `at` is the one field excluded from the digest.

Full detail in §f.

### Proof captured

| file | what it is |
|---|---|
| `docs/evidence/lab06_cli.txt` | the full CLI transcript, 16/16, exit 0 |
| `docs/evidence/lab06_pytest.txt` | 548 passed, 1 skipped — the whole project |
| `docs/evidence/lab06_notebook_output.txt` | all 64 notebook cells with outputs, 0 error outputs |

---

## f) Common errors, and the bugs found while building this

### Errors you will hit, and what they mean

**`BudgetExceeded: budget 'steps' exhausted before step 'publish': 3 of 3 spent, and the next action requests 1.`**
R3 working. The step did not run. `RunReport.escalated` is `True`, and the
diagnosis says which dimension, what was spent against what, and names the two
legitimate responses:

```
budget refused step 'publish': the 'steps' budget is exhausted, 3 of 3 spent and
this step requests 1. Nothing ran, so there is nothing to undo. Raise the
ceiling deliberately or shorten the plan; do not lower MAX_ATTEMPTS to get past
it.
```

Do not lower `MAX_ATTEMPTS` to get past it. That sentence is there because it is
the tempting wrong answer: a ceiling reached by a runaway loop should be raised
deliberately or not at all, not lowered to make the symptom disappear.

**`GateRefused: step 'publish' held at the 'publish' gate by course_faculty and was not run.`**
R15/R19 working. Look at `RunReport.gate_answers` for the verbatim reason, and at
the `refused` audit record for the approval request that was presented. The
runtime will not retry it — a retry would be the runtime asking a human a question
it already knows the answer to.

**`CheckpointUnavailable: no checkpoint for thread 'never-seen'. Threads on this store: clean, gate-1a2b3c4d.`**
`resume=True` on a thread with no checkpoint. The message lists the threads that
*do* exist, so a typo is a lookup rather than an investigation. Starting over is
the one behaviour a resume must never have: a resume that silently begins from
zero is the failure §R11 warns about.

**`ResumeDivergence: resumed run 't' diverged from the audit log at 'audit_seq': expected 5, got 0.`**
A wiring mistake, not a result divergence: the audit log you handed the runtime is
not the log the checkpoint was written against. `ResumeDivergence.reason` says
which of the two it is, because both are "the thing you gave me does not go with
the thing you are resuming".

**`RuntimeBoundaryError: thread 't' already holds a checkpoint under …\t.sqlite. Pass resume=True to continue that run, or a different thread to start a new one.`**
Bug 36's fix. You are starting a fresh run with a resume key that is taken. Do not
"fix" this by deleting the checkpoint file — pass `resume=True`, or use a new
`thread`.

**`AuditIntegrityError: audit chain broken at record 1: the record's content does not match its digest.`**
Either the file was edited, or a record was built with a digest that does not match
its contents. The second is a bug in a caller and `seq=1` on the *first* record is
the tell — nothing can have removed a record before it.

**`RuntimeError: SqliteCheckpointStore used outside session().`**
Open it with `with SqliteCheckpointStore(path).session() as store:`. The session
*is* the connection; the store does not keep one open between calls, which is what
stops it leaking a handle per call.

**`GateScriptExhausted: gate 'publish' was asked 2 time(s) but its script had no answer left. This is a test-setup error, not a runtime refusal: either the run performed more gated steps than expected, or the gate is being asked twice for the same step.`**
A `ScriptedGate` ran out of answers. It raises rather than inventing a fourth
outcome, because a gate that guesses is worse than a gate that stops — and the
message says *this is a test-setup error*, because in production the gate is a
`CallbackGate` and this exception means the script and the plan disagree.

**`pydantic_core.ValidationError: 2 validation errors for AuditRecord / digest / prev_digest`**
You tried `AuditRecord(**fields)` directly. `digest` and `prev_digest` are required
and derived — use `build_audit_record()`, which computes them. This error is the
pitfall's enforcement working, and it is worth keeping: a schema that accepted a
hand-written digest would accept a wrong one.

**`ValidationError: 1 validation error for Budget / limits / Value error, budget 'steps' limit must be an int, got True of type bool`**
Bug 35. Coercing `True` to `1` would have produced a one-step budget from a typo,
and the run would have stopped for a reason nobody could reconstruct.

### The development bug log — Lab 6's six

Found by writing the tests and by running the notebook, not by reading the code
afterwards. Each is now covered by a test.

| # | defect | why it mattered |
|---|---|---|
| 31 | checkpoint ids were `digest[:24]`, selected by `ORDER BY checkpoint_id DESC` | A **string** compare over uniformly-distributed hex, so `load()` returned an arbitrary checkpoint from a five-step run. Every time, and randomly. Now `f"{write_seq:012d}-{digest[:12]}"`, with the ordinal allocated from the SQLite table rather than from `RunState` — a counter in the state would not survive a second process |
| 32 | `reserve()` checked, `charge()` committed | Every dimension in every report read `0`, so the budget **could never refuse**. The feature was a set of comments. `reserve()` now checks and commits in one step; `charge()` remains for post-hoc costs (`records_read`, `wall_clock_ms`); `spend()` is a documented alias |
| 33 | a failing step was audited but its new chain position was never checkpointed | The next append reused a sequence number the log already held, and `AuditIntegrityError` fired **on an untampered log**. An integrity check that cries wolf is worse than none. Fixed with `_halt_at` |
| 34 | `resume()` never verified the audit log belonged to the checkpoint | A wiring mistake surfaced as *"audit chain broken"*, naming the log as the culprit. Fixed with `_assert_same_log` and a `reason` on `ResumeDivergence` |
| 35 | four defects in one pass: the `Budget` limits validator ran in "after" mode; `ScriptedGate.remaining` double-subtracted; `InMemoryCheckpointStore` lacked `write_seq`; the generated `run_id` derived from `id(self)` | Pydantic coerced `True` to `1`, so a typo became a one-step budget. `id(self)` is a **memory address**: CPython reuses them, so a second runtime in a long process could be handed a `run_id` that a resume thread already used |
| 36 | a second `run_lab06` into the same directory appended a fresh chain to a full one | Found by the notebook, which runs the lab twice on purpose. The error named the audit log, the one component that had done nothing wrong — the worst order to find a wiring bug in. Fixed at both ends: a per-invocation `run-<8 hex>` directory, and a `RuntimeBoundaryError` that names both ways out |

Bugs 33, 34 and 36 share one shape worth naming: **the safety mechanism was the
thing that misfired, and its error message pointed at an innocent component.** An
integrity check that fires on a good log, a divergence check that cannot say what
diverged, a chain check that blames the log for a `thread_id` collision — each one
costs more in time than the defect it was built to catch, because it sends the
reader to the wrong file. Each is now fixed by making the message name the actual
cause, and each has a test that constructs the confusion deliberately rather than
waiting for it to recur.

Bug 36 is also the one that only the **notebook** could find. The test suite's
`two_lab_runs` fixture uses two separate directories (`a/` and `b/`), so the suite
passed while the notebook — which runs `run_lab06` twice into one base — died on
its second invocation. The lesson is the same one from Lab 5's bug 28: *the
deliverable format is a notebook, so the notebook is a test.*

### A bug the tests could not have found, and why

`wall_clock_ms` was charged from a measured duration, so a step completing in
0.4 ms charged `0`. Over a 24-step run the whole dimension could stay at zero, and
a wall-clock ceiling that never moves is exactly the "runaway loop" guard §2.12
asks for — present, plausible, inert. Fixed with `math.ceil(duration_ms)`, which
makes the minimum cost 1 ms, and pinned by
`test_the_wall_clock_is_charged_at_least_one_ms`.

This is the second time in this project that a *physically correct* implementation
disabled the check it existed to perform (the first was Lab 5's bug 30, a hardcoded
column width). Precision is not correctness when the quantity is supposed to
accumulate.

---

## g) Requirement coverage

| # | Doc requirement | Where met | Evidence | Status |
|---|---|---|---|---|
| R1 | *"Run the agent inside a controlled runtime"* | `Runtime.run` / `execute_step` | CLI §1; 16/16 checks; `test_steps_run_in_plan_order` | done |
| R2 | *"binding skills + tools + memory behind the loop"* | `KIND_DIMENSION`, `build_demo_plan()` | `tool_calls 2/12, skill_calls 1/8, connector_calls 1/12, llm_calls 1/8` in the clean run; `test_the_plan_spans_the_three_families_the_document_names` | done |
| R3 | *"enforcing budgets"* | `BudgetLedger`, `DEFAULT_LIMITS` (8 dimensions) | CLI §"Budget refusal": `steps spent=3/3, refusals=1, escalated=True` | done |
| R4 | *"and approval gates"* | `ApprovalGate` + 5 implementations, 3 outcomes | CLI §"Gate refusal"; §3.4 of the notebook | done |
| R5 | *"and logging every step"* | `JsonlAuditLog`, one `AuditRecord` per step | `audit: 5 record(s)` on the clean run; `test_every_step_gets_exactly_one_audit_record` | done |
| R6 | *"Runtime wrapping the agent: budgets, gates, checkpoint/resume, audit log."* | `course_organizer/runtime/`, 7 modules + lab module, 51 exports | 170 tests; `langgraph-checkpoint-sqlite` on the table | done |
| R7 | *"Accept: Every action is logged with inputs/outputs"* | `AuditRecord.inputs`/`.outputs` required | check 3 (`5/5 ok record(s) carry both`), check 4, check 5 | done |
| R8 | *"Accept: … a crashed run resumes from its last checkpoint"* | `crash_and_resume`, two `os._exit`-real processes | checks 6–10: `exit=9`, `resumed ran ('report',)`, `publish reran: False`, `gate asked again: False`, `5 record(s) verified=True` | done |
| R9 | *"Log inputs and outputs, not just messages"* | required schema fields; `build_audit_record` computes the digest | `test_a_record_cannot_be_built_without_inputs_and_outputs`; check 5 | done |
| R10 | *"checkpoint before risky steps"* | `RUNTIME_ORDER[2]` before `[3]`; `execute_step` | check 15: `1 gated record(s), each naming the pre-step checkpoint: ['000000000004-caee53ccef93']` | done |
| R11 | *"…or resume loses state"* | `RunState`'s five fields | notebook §3.3; `test_resume_carries_the_artifacts_forward`, `test_a_resumed_run_is_stopped_by_the_dead_runs_budget` | done |
| R12 | `run = Runtime(budget=B, gates=[publish])` | `Runtime(budget=..., gates=...)`, `DEFAULT_GATE="publish"` | `test_the_demo_plan_is_the_documented_shape` | done |
| R13 | `for step in agent.loop():` | `Runtime.run(plan)` | `steps: ('read_notes', 'draft_plan', 'draft_grades', 'publish', 'report')` | done |
| R14 | `audit.log(step.intent, step.action, step.result)` | `AuditRecord.intent`/`.action`/`.outputs` | check 4: `intent and action present on 5/5 record(s)` | done |
| R15 | `if step.risky: wait_for_approval(step)` | `Step.risky`/`Step.gate`, `ApprovalGate.request()` | check 2; check 13 (`publish outputs={}`) | done |
| R16 | `checkpoint(run.state)` | `SqliteCheckpointStore` on `langgraph-checkpoint-sqlite==3.1.1` | `checkpoints: 6` on a 5-step run (one extra = the pre-gate write) | done |
| R17 | §2.12 *"…escalate to course faculty with a diagnosis"* | `RunReport.escalated` + `.diagnosis` + `.stopped_at` | CLI §"Budget refusal": `escalated: True`; `TestDiagnosis` (6 tests) | done |
| R18 | §2.12 *"a full audit log of every grade published and who approved it"* | hash-chained JSONL, `decided_by` on every record | checks 14 and 16; three tampers caught, one refused at append | done |
| R19 | §2.12 *"Never publish a grade or feedback without faculty approval"* | the gate is a hard stop before the step body | check 13: `status=stopped, publish outputs={}`; check 14: `decided_by=course_faculty` | done |
| R20 | Glossary: *"Coordinator … owns run-state, plan, budgets and the audit log"* | `Runtime` owns all four; `principal` set by the runtime | `principal: "apniLeap AURA Coordinator"` on every record; `test_the_runtime_sets_the_principal_not_the_step` | done |

**Deliberately deferred, and where each lands**

| limitation | why it is not in this lab | settled by |
|---|---|---|
| **no real `interrupt()`** | §2.12's guardrail is implemented as a *gate protocol* with three outcomes, so it is testable without a person. A `DenyAllGate` is not a human | **Lab 7** — the same gate inside a real LangGraph `interrupt()` / `Command(resume=…)` |
| **no node graph** | Lab 6 owns durability — checkpoints, audit, budgets, gates. Topology is a different claim, and a run loop is the honest substrate for it | **Lab 7** |
| **no parallel steps** | a budget is charged per step and a checkpoint is written per step; concurrency would need a reservation model, which is Lab 8's problem | **Lab 8** |
| **the demo steps are stand-ins** | `draft_grades` returns `{"drafts": 3, …}` rather than calling a model. Lab 6 makes no LLM calls and needs no key | Lab 10 validates the grades |
| **`wall_clock_ms` is a post-hoc check** | a duration is not knowable in advance, so it is checked and charged rather than reserved. The other seven dimensions are reservations | — |
| **SQLite durability is process-level** | the crash proof is a process death, which is the failure mode the acceptance criterion names. Filesystem and power-loss guarantees are not claimed | — |
| **the eight ceilings are defaults** | they are a table, not a finding. §2.12's `MAX_ATTEMPTS` is a Lab 7/10 concern | — |

---

## h) Report and documentation text

The following is written for submission. It states what was built, what was
measured, and what was deliberately left out.

### Summary

Lab 6 implements the document's runtime as a package that makes the document's
**own pitfall** the specification. The pseudocode on line 682 and the pitfall
printed beneath it disagree twice — the pseudocode logs before running, and
checkpoints after asking for approval — and the lab implements the pitfall's
version, with `DOC_PSEUDOCODE` and `RUNTIME_ORDER` printed side by side so the
correction is visible rather than silent. A test asserts the two differ in exactly
the two places the document names, so the runtime cannot quietly drift back to the
order the document got wrong.

The runtime is one loop. `execute_step()` is the only method that calls a step
body, and it does eight things in a fixed order: reserve the budget, check the
wall clock, checkpoint if the step is risky, ask the gate, run, charge what it
actually cost, audit with inputs **and** outputs, checkpoint again. The demo plan
spans the four families the document names as its inputs — a skill, the
connector, an LLM step and tool work — and the clean run charges all four
dimensions, so "binding skills + tools + memory behind the loop" is a measured
outcome rather than a description.

The pitfall's first clause is enforced by a schema rather than by a convention.
`AuditRecord.inputs` and `.outputs` are required, and the digest is derived by
`build_audit_record()`, so a record that lacks them **cannot be constructed** — the
notebook discovered this by trying to build one and receiving a pydantic
`ValidationError` naming both fields. A log format that *accepts* a message-shaped
record is a log format that will eventually contain one. The chain then makes the
log evidence rather than a claim: three distinct tampers are each detected, and the
forged predecessor is refused **at append**, with the file's byte count unchanged
afterwards, because §2.12's *"full audit log"* is not satisfied by a log that will
take a forgery.

The pitfall's second clause is checked on the evidence rather than trusted from the
code. The record for the gated `publish` step carries the id of the checkpoint
written *before* the gate was consulted, and that id can only be there if the write
happened first. The acceptance criterion's crash half is then demonstrated with
**two real OS processes**: the first calls `os._exit(9)` inside the last step of
the plan, and the second resumes. The result is that the resumed process runs
exactly one step, `publish` does not execute twice, the gate is not re-asked, and
all five audit records verify as one chain across the process boundary. The
approval is in the checkpoint, which is *why* the gate is consulted once — and had
the checkpoint been written after the publish, this is precisely the run that would
have asked a human to approve the same thing twice and published the grades twice.

Two refusals show the two ways a governed run stops. A **gate** refusal is on the
audit log with empty outputs, the approver's identity, the verbatim reason, and
the approval request itself — because a human was asked and answered, which is
exactly what §2.12's *"who approved it"* needs recorded. A **budget** refusal
produces **no** audit record, because nothing ran; the refusal is accounted for
instead in `RunReport.diagnosis`, in `BudgetLedger.refused`, and in the structured
`runtime.run_stopped` event. That asymmetry is asserted in the direction that is
easy to get wrong, since logging a refusal as though something happened would make
§2.12's *"every grade published"* unanswerable — the count of records would stop
being the count of things done.

### What was measured

| claim | how | result |
|---|---|---|
| every action is logged with inputs and outputs | check 3, over the clean run | **5/5** `ok` records carry both |
| the log is not a message log | check 5 | 0 records with a missing half |
| the document's triple is present | check 4 | `intent` and `action` on **5/5** records |
| a crash really happened | check 6 | child exit code **9** via `os._exit` |
| resume runs only what is left | check 7 | ran `('report',)`, skipped 4 |
| the publish did not repeat | check 8 | `publish executed in the resumed process: False` |
| the gate was not re-asked | check 9 | `gate consulted again on resume: False` |
| the chain survives a process boundary | check 10 | **5** records, `verified=True` |
| the budget refuses *before* the action | check 11 | `steps spent=3/3, refusals=1, escalated=True` |
| the over-limit step never ran | check 12 | 3 steps executed, `publish`/`report` absent |
| the gate is a hard stop | check 13 | `status=stopped, publish outputs={}` |
| the refusal names who and what | check 14 | `decided_by=course_faculty`, inputs recorded |
| the checkpoint preceded the gate | check 15 | `['000000000004-caee53ccef93']` on the gated record |
| the chain verifies | check 16 | 5 records, 1 fingerprint |

Suite: **170 Lab 6 tests** (84 runtime + 86 lab module); **548 passed, 1 skipped**
for the whole project. 4,828 lines across 8 modules, and a 64-cell notebook
(28 markdown, 36 code, all executed, 0 error outputs).

### Honest limitations

* **The gate is a protocol, not a person.** `DenyAllGate` is a refusal with a
  reason attached. It proves the runtime *obeys* a decision; it does not prove a
  human would make a good one. Lab 7 puts the same gate inside a real
  `interrupt()`.
* **`wall_clock_ms` is the one post-hoc dimension.** Seven are reservations made
  before the action; the eighth is measured and charged, because a duration is not
  knowable in advance. It is the only ceiling that a runaway loop meets late.
* **The ceilings are defaults, not findings.** `DEFAULT_LIMITS` is a table that
  the tests keep in sync with this document, and §2.12's `MAX_ATTEMPTS` is a
  Lab 7/10 concern.
* **Process death, not power loss.** The crash proof is `os._exit(9)`. That is the
  failure mode the acceptance criterion names. Filesystem and disk guarantees are
  not claimed.
* **The steps are stand-ins.** `draft_grades` returns three drafts; it does not
  call a model. Lab 6 makes no LLM calls and needs no API key, which is the point
  of a runtime layer.
* **No parallelism.** One run, one thread, one log, sequential steps. Lab 8
  changes that and will need a reservation model the current ledger does not have.
* **The audit log's `token_fingerprint` is empty** on every record here, because
  Lab 6 calls no credentialed connector. The field is carried so the log does not
  have to change shape when a connector step arrives.

### Reproducing this

```powershell
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
$env:PYTHONPATH = "src"

# the whole lab, self-checking: prints 16/16 and exits 0
& $py -m course_organizer.labs.lab06_governed_runtime

# the lab's own tests, including the two-process crash proof
& $py -m pytest tests\test_lab06_runtime.py tests\test_lab06_lab_module.py -o addopts= -v

# the whole project
& $py -m pytest

# the notebook, executed
& $py -m jupyter nbconvert --to notebook --execute --inplace `
    --ExecutePreprocessor.kernel_name=course-organizer `
    notebooks\lab06_governed_runtime.ipynb
```

No API key, no network, no cloud account. Two figures in the output will differ on
your machine: the `wall_clock_ms` total and, usually, the audit fingerprints. A
fingerprint covers every field of every record **except** its timestamp, so two
runs agree exactly when they measured the same durations — and these steps take
single-digit milliseconds, so a tie is ordinary rather than a collision. Neither
figure is a threshold; both are printed as evidence.
