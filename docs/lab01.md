# Lab 1 — Agent vs Chatbot: the organiser planner loop

**Worklet 2 · Chapter 2 · Phase A · Foundations**

---

## a) Goal and the document's requirements

> **§2.10 Lab 1 — Agent vs Chatbot (the organised course workspace planner loop)**
>
> - **Phase:** A · Foundations
> - **Goal:** "Establish the agent loop (clarify → plan → revise) instead of a one-shot chatbot answer."
> - **What it adds:** "A minimal planner that clarifies the request, proposes a plan for producing the organised course workspace (notes, assignments, grades), and revises on feedback — the seed this worklet grows from."
> - **Inputs:** "Free-text request; core parameters."
> - **Outputs:** "A clarified request + an editable outline plan for the organised course workspace (notes, assignments, grades)."
> - **Agents/nodes:** "Coordinator (loop only). No specialist nodes yet."
> - **Deliverable:** "A notebook agent that clarifies, plans and revises an organised course workspace outline."
> - **Accept:** "Given a vague request it asks **at least two clarifying questions** and yields a coherent, **editable** plan."
> - **Nodes activated:** "Coordinator loop (no specialist nodes yet)."
> - **Pitfall to avoid:** "Don't answer in one shot — without clarify/revise it's a chatbot, not an agent."
> - **Pseudocode:**
>
>       req  = clarify(user_request)         # ask until inputs complete
>       plan = propose_plan(req)             # outline for the organised course workspace
>       while user.wants_changes(plan):
>           plan = revise(plan, user.feedback)
>       return plan                          # not final — a structured plan

**Requirement IDs used for traceability**

| ID | Requirement |
|----|-------------|
| R1-1 | Goal: establish the agent loop (clarify → plan → revise) |
| R1-2 | Inputs: free-text request + core parameters |
| R1-3 | Output: a clarified request |
| R1-4 | Output: an editable outline plan for the workspace |
| R1-5 | Nodes: Coordinator loop only, no specialist nodes |
| R1-6 | Deliverable: a notebook agent |
| R1-7 | **Accept:** ≥2 clarifying questions from a vague request |
| R1-8 | **Accept:** the plan is coherent and editable |
| R1-9 | **Pitfall:** not a one-shot answer |
| R1-10 | Pseudocode order: clarify → propose_plan → while revise → return |
| R1-11 | Human authority is course faculty; agents draft, a human approves |

---

## b) Concepts you must understand

### 1. Chatbot vs agent

A chatbot maps text to text. An agent maps text to **a sequence of state changes
with its own termination condition**. Concretely, Lab 1 differs in three ways,
and each is a separate test:

| | Chatbot | Agent (Lab 1) |
|---|---|---|
| Detects its own missing inputs | No — it invents them | Yes, and asks |
| Returns | prose | a typed object |
| Can be revised | No | Yes, and the revision actually changes the plan |

### 2. "ask until inputs complete" is a termination condition, not a vibe

The doc's comment `# ask until inputs complete` is the whole specification. It
means: define the required parameters explicitly, test each one against what is
actually known, and keep asking until the set is closed. Two corollaries:

- **Complete detection needs a definition.** Lab 1 defines four *required*
  parameters: `course_code`, `units`, `assignments`, `as_of`. Everything else is
  optional and is assumed — but **recorded as an assumption**, because the
  document forbids silent guessing (Lab 3's pitfall, applied early).
- **Asking has a bound.** `MAX_CLARIFY_ROUNDS` guards against a user who never
  answers. Unanswered fields are recorded as `assumed=True`, not invented.

### 3. Structured output is what makes it a pipeline

If the plan were a paragraph, Lab 2 could not use it. `CourseWorkspacePlan` is a
Pydantic model with `extra="forbid"` and `validate_assignment=True`, so a typo
fails loudly instead of being silently accepted. Lab 7 will wire this exact
object into a LangGraph state.

### 4. Draft vs final

The pseudocode returns `plan  # not final — a structured plan`. So `PlanStatus`
is a real enum with three values and Lab 1 only ever sets `DRAFT`:

- `DRAFT` — Lab 1 (this lab)
- `LOCKED` — Lab 3 (`plan_skill.lock()`)
- `APPROVED` — only after the faculty gate

### 5. Determinism for testability

An acceptance criterion a reviewer cannot re-check is not an acceptance
criterion. The `ScriptedLLM` backend makes Lab 1 reproducible on any machine
with no key, so the tests below are the deliverable's proof.

---

## c) Implementation

### Files created

| File | Role |
|------|------|
| `requirements.txt` | Install-tested pins |
| `.env.example` | Every key with a safe default |
| `.gitignore` | Secrets and runtime output excluded |
| `pyproject.toml` | Package + pytest config |
| `src/course_organizer/__init__.py` | Package doc, `HUMAN_AUTHORITY = "course_faculty"` |
| `src/course_organizer/config.py` | `Settings` (pydantic-settings). No hardcoded keys |
| `src/course_organizer/logging_setup.py` | `run_id`-stamped logging, JSON run log |
| `src/course_organizer/schemas.py` | §2.6 Data Model + Lab 1 plan objects |
| `src/course_organizer/llm.py` | `LLMClient` protocol, `ScriptedLLM`, `GeminiLLM` |
| `src/course_organizer/labs/lab01_planner_loop.py` | The agent |
| `notebooks/lab01_planner_loop.ipynb` | **The deliverable** |
| `tests/conftest.py` | Fixtures; forces the offline backend |
| `tests/test_lab01_planner_loop.py` | 25 tests, incl. the two acceptance tests |

### The required parameters

```python
REQUIRED_FIELDS = (
    ("course_code",  "Which course is this for? ...",           "<why it matters>", "CS201"),
    ("units",        "Which units or topics should be in scope?", "...",             "units 1-3"),
    ("assignments",  "Which assignment(s) are in scope?",       "...",             "all three"),
    ("as_of",        "What date should I treat as 'now'?",      "...",             "2026-10-15"),
)
```

Each entry carries **why_it_matters**. A clarifying question that cannot justify
itself is an interrogation, not an agent.

### The loop, in the doc's order

```python
def run(self, user_request, provider):
    trace = ["clarify"]
    request = self.clarify(user_request, provider)     # req  = clarify(...)
    trace.append("propose_plan")
    plan    = self.propose_plan(request)              # plan = propose_plan(req)
    trace.append("revise_loop")
    revision_count = 0
    while provider.wants_changes(plan) and revision_count < self.max_revisions:
        feedback = provider.get_feedback(plan)
        if not feedback.strip():
            break
        plan = self.revise(plan, feedback)            # plan = revise(plan, feedback)
        revision_count += 1
        trace.append(f"revise:{revision_count}")
    return PlannerResult(request=plan.request, plan=plan, ...)
```

### The `FeedbackProvider` seam

The `user` in the pseudocode is behind one abstract interface, with two
implementations:

- `ScriptedFeedback` — a list of answers and a list of feedback turns. Used by
  tests and the demo. **The agent code is identical.**
- `InteractiveFeedback` — prompts a real person on stdin.

This is what makes the acceptance test deterministic without making the
interactive path a fake.

### The LLM boundary

```python
class LLMClient(ABC):
    def generate_structured(self, *, system, user, schema, step="unspecified"): ...
    def generate_text(self, *, system, user, step="unspecified"): ...
```

Agents never construct a provider client. This is the same discipline the
document demands of the *data* layer in Lab 5 ("no agent touches a source
directly"), applied to the model layer.

- `ScriptedLLM` — deterministic. Registered builders per schema do real
  rule-based extraction (regex for course codes, unit ranges, assignment ids,
  ISO dates) and deterministic plan assembly. Not a stub that echoes strings.
- `GeminiLLM` — `ChatGoogleGenerativeAI.with_structured_output(schema)`, plus a
  `RateLimiter` (free tier ≈ 15 rpm) and a `BudgetExceeded` guard.

### Guardrails encoded structurally, not by convention

```python
# SubmissionRecord: there is deliberately NO is_plagiaristic: bool.
similarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)

# submitted_at must be timezone-aware, or lateness is uncomputable.
@field_validator("submitted_at")
def _require_timezone(cls, v): ...

# Version history must be contiguous from 1, no duplicate numbers.
@field_validator("versions")
def _versions_strictly_increasing(cls, versions): ...

# The gate is a hard stop: plan step S10 depends on S9, and publish depends on S10.
```

---

## d) Setup and run

```powershell
python -m venv $HOME\.virtualenvs\course-organizer      # OUTSIDE OneDrive
& "$HOME\.virtualenvs\course-organizer\Scripts\python.exe" -m pip install -r requirements.txt
& "$HOME\.virtualenvs\course-organizer\Scripts\python.exe" -m pip install -e .
Copy-Item .env.example .env                             # optional
```

```powershell
python -m course_organizer.labs.lab01_planner_loop
python -m course_organizer.labs.lab01_planner_loop --interactive
python -m course_organizer.labs.lab01_planner_loop `
  --answer course_code=DBMS402 --answer "units=units 2-4" `
  --answer "assignments=all three" --answer "as_of=2026-11-01" `
  --revise "also cover unit 5"

jupyter lab notebooks/lab01_planner_loop.ipynb
python -m pytest tests/ -v
```

---

## e) Test cases and expected output

25 tests. The two named `test_doc_acceptance_*` are the document's own words.

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_doc_acceptance_vague_request_asks_two_or_more_questions` | **≥2 questions**; each is interrogative, justified and answered |
| 2 | `test_doc_acceptance_yields_coherent_editable_plan` | **Draft**; topological order acyclic; gate before publish; every step has acceptance criteria |
| 3 | `test_concrete_request_asks_no_questions` | Control: a complete request gets **0** questions (no nagging) |
| 4 | `test_clarify_extracts_core_parameters_from_free_text` | `CS201`, units `[1,2,3]`, `ASG-01/02`, `2026-10-15` |
| 5 | `test_clarify_answers_become_the_plan_scope` | Answers change the plan, not just the record |
| 6 | `test_clarify_rejects_empty_request` | `ValueError` |
| 7 | `test_vague_request_never_guesses_without_saying_so` | Every assumption recorded |
| 8 | `test_plan_covers_all_in_scope_streams` | Notes, assign, collect, similarity, grade, milestone all present |
| 9 | `test_plan_scales_with_scope` | "no assignments" removes the grading stream, keeps the governance spine |
| 10 | `test_plan_never_marks_itself_final` | Status is `DRAFT` |
| 11 | `test_plan_id_is_stable_and_course_scoped` | `PLAN-CS201-...` |
| 12 | `test_plan_outline_renders_dependency_order` | Dependents printed after their dependency |
| 13 | `test_revise_applies_a_real_change` | Structural change, not a log append |
| 14 | `test_revise_can_remove_a_step` | Deletion works |
| 15 | `test_revise_can_move_the_deadline` | `as_of` moves |
| 16 | `test_revise_can_widen_scope` | Units widen |
| 17 | `test_revise_never_loses_the_users_words` | Unrecognised feedback still recorded |
| 18 | `test_revise_rejects_empty_feedback` | `ValueError` |
| 19 | `test_revise_loop_is_bounded` | Stops at `MAX_REVISIONS` and escalates |
| 20 | `test_one_shot_chatbot_is_not_an_agent` | Anti-pattern returns prose, asks nothing, is unrevisable |
| 21 | `test_run_respects_the_llm_budget` | `BudgetExceeded` |
| 22 | `test_settings_default_to_offline_scripted_backend` | Runs with no `.env` |
| 23 | `test_require_live_llm_explains_the_fix` | Actionable error message |
| 24 | `test_result_summary_reports_the_acceptance_metrics` | Metrics surfaced |
| 25 | `test_trace_records_the_loop_order` | `clarify → propose_plan → revise:1` |

### Captured proof

`docs/evidence/lab01_pytest.txt`:

```
============================= test session starts =============================
collected 25 items

tests\test_lab01_planner_loop.py .........................               [100%]

============================= 25 passed in 0.07s =============================
```

`docs/evidence/lab01_notebook_output.txt` — the fully executed notebook
(13 code cells, 0 errors).

---

## f) Common errors and fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ERROR: Could not install packages due to an OSError: [WinError 32] The process cannot access the file...` | The venv is inside a OneDrive / Google Drive synced folder; the sync client holds file locks | Create the venv **outside** the sync root: `python -m venv $HOME\.virtualenvs\...` |
| `ResolutionImpossible` mentioning `mcp` | `mcp==2.2.0` conflicts with `langchain-mcp-adapters==0.3.2`, which requires `mcp<2.0.0,>=1.24.0` | Pin `mcp==1.30.0` |
| `ImportError: cannot import name 'StateGraph' from 'langgraph'` | (Lab 7, noted now) `StateGraph` is not on the `langgraph` top level | `from langgraph.graph import StateGraph, START, END` |
| `ModuleNotFoundError: No module named 'course_organizer'` | Ran from the repo root without installing | `pip install -e .`, or add `src` to `sys.path` (the notebook does this) |
| `RuntimeError: LLM_BACKEND=live but GOOGLE_API_KEY is empty` | You flipped to `live` before adding a key | Set the key, or set `LLM_BACKEND=scripted` |
| `BudgetExceeded: LLM call budget exhausted` | Run needed more calls than `RUNTIME_BUDGET_CALLS` | Raise it in `.env`, or reduce the fan-out |
| `ValueError: duplicate version numbers in note history` | Two versions claimed the same number | `NoteMaterial.versions` must be contiguous from 1 |
| `ValueError: submitted_at must be timezone-aware` | A naive `datetime` was passed | `datetime(..., tzinfo=timezone.utc)` |
| `ValueError: plan steps contain a dependency cycle among: ...` | A step depends on itself | `depends_on` must reference the *previous* step; `add()` now returns its own id to make chaining safe |
| `AssertionError: not a question: 'Which course is this for? (e.g. ...)'` | A test asserted `endswith("?")` but questions carry a parenthetical example | Assert `"?" in question` — interrogative, not terminated |
| Plan output shows `â€”` / `â€"` | Windows console code page mangling UTF-8 | All runtime strings in this project are plain ASCII; if you add typographic characters, run with `PYTHONIOENCODING=utf-8` |
| `ValueError: user_request must be a non-empty string` | Passing `""` or whitespace | Guard the call site; this is a programming error, not a vague request |

---

## g) Requirement coverage

| ID | Requirement | Where satisfied | Test | Status |
|----|-------------|------------------|------|--------|
| R1-1 | Agent loop clarify → plan → revise | `PlannerAgent.run` | 25 | ✅ |
| R1-2 | Inputs: free text + core parameters | `REQUIRED_FIELDS`, `clarify` | 3, 4, 5 | ✅ |
| R1-3 | Output: a clarified request | `ClarifiedRequest` | 4, 5 | ✅ |
| R1-4 | Output: an editable outline plan | `CourseWorkspacePlan.outline()` | 2, 12 | ✅ |
| R1-5 | Coordinator loop only | No specialist node is invoked; specialists appear as *plan entries* | 2 | ✅ |
| R1-6 | Deliverable: a notebook agent | `notebooks/lab01_planner_loop.ipynb`, executed, 0 errors | — | ✅ |
| R1-7 | **≥2 clarifying questions** | `clarify` | **1** | ✅ |
| R1-8 | **Coherent, editable plan** | Acyclic topological order, `DRAFT`, real edits | **2** | ✅ |
| R1-9 | Not a one-shot answer | `one_shot_chatbot` contrast | 20 | ✅ |
| R1-10 | Pseudocode order | `PlannerResult.trace` | 25 | ✅ |
| R1-11 | Human authority = course faculty | `HUMAN_AUTHORITY`; gate step S10 precedes publish S11 | 2 | ✅ |

**Deliberately not in Lab 1** (each is a later lab, per the doc's own phasing):

| | Belongs to |
|---|---|
| Tools / typed operations | Lab 2 |
| Skills, locked plan | Lab 3 |
| Memory / retrieval | Lab 4 |
| Connector | Lab 5 |
| Durable audit, budgets | Lab 6 |
| Executable node graph | Lab 7 |
| Parallel swarm | Lab 8 |

---

## h) Report text

### Lab 1 — Agent vs Chatbot: the organiser planner loop

**Phase:** A · Foundations · **Nodes activated:** Coordinator loop only

#### Objective

Establish the agent loop — clarify, plan, revise — instead of producing a
one-shot chatbot answer. This is the seed the whole worklet grows from: the
loop, its typed contracts and its bounded termination become the coordinator's
runtime in Labs 6–8.

#### Implementation

The lab implements the document's pseudocode directly and in order. A
`PlannerAgent` exposes `clarify`, `propose_plan` and `revise`; `run` executes
them as `clarify → propose_plan → while wants_changes: revise → return`.

*Clarification* defines four required parameters (`course_code`, `units`,
`assignments`, `as_of`). Each is tested against what is actually known; anything
absent becomes a question carrying its justification and a sample answer. The
loop is bounded by `MAX_CLARIFY_ROUNDS`. Anything the agent fills in itself is
recorded in `assumptions` — the document forbids silent guessing, and Lab 1
obeys that rule before Lab 3 introduces it.

*Planning* produces a `CourseWorkspacePlan`: a Pydantic model whose steps carry
intent, inputs, outputs, dependencies and acceptance criteria. The steps cover
the document's three work streams (§2.3) plus the governance spine that §2.7 and
§2.12 make non-optional — validate, assemble, human gate, publish. Steps are
dependency-chained, so the human gate provably precedes publication. The plan
status is `DRAFT`, matching the pseudocode's `# not final` annotation.

*Revision* is a genuine edit. Feedback is interpreted into structural changes —
add a step, remove a step, move the anchor date, change a default priority, widen
or narrow scope — and each pass is recorded in `PlanRevision` with the user's
verbatim words and what changed. Unrecognised feedback is still recorded, so the
reviewer's input is never silently dropped. The loop is capped at
`MAX_REVISIONS`; on reaching the cap it stops **and escalates** via
`open_questions`, applying the document's Lab 7 warning that an unbounded
regenerate loop can spin forever.

The human in the loop sits behind a `FeedbackProvider` interface, with a scripted
implementation for tests and an interactive one for the demo. The agent code is
identical in both, so the acceptance test is deterministic without the
interactive path being a mock.

The document's pitfall — *"Don't answer in one shot — without clarify/revise it's
a chatbot, not an agent"* — is implemented as a runnable anti-pattern
(`one_shot_chatbot`) and compared side by side with the agent in the notebook.
The difference is demonstrated, not asserted.

#### The LLM boundary

The document is vendor-neutral. `LLMClient` is a protocol with two
implementations: `ScriptedLLM` (deterministic, offline, free, rule-based
extraction and plan assembly) and `GeminiLLM`
(`ChatGoogleGenerativeAI.with_structured_output`, with a rate limiter for the
free tier's ≈15 rpm and a hard call budget). The default is `scripted`, which is
why the project needs no API key and why the acceptance criteria can be
re-checked by a reviewer without credentials.

#### Results

Twenty-five tests pass. Given the deliberately vague request *"I need to get my
course organised but honestly I have not even started thinking about the
details. Sort it out please."* the agent asked **4** clarifying questions (≥2
required) and returned an **11-step draft plan**, topologically ordered, with the
human gate immediately before publish. Given a request that already names a
course, units, assignments and a date, it asked **0** questions. The one-shot
anti-pattern asked 0 questions, returned prose, and could not be revised.

The executed notebook runs 13 code cells with 0 errors.

#### Guardrails honoured

Course faculty is the named authority and appears as a hard-stop plan step
before publication. `DraftGrade` cannot exist in an approved state. A submission
carries a similarity score but no plagiarism verdict field. Submission timestamps
must be timezone-aware and are never rewritten. Note version history must be
contiguous from 1 with no duplicates.

#### Conclusion

Lab 1 satisfies both halves of its acceptance criterion — ≥2 clarifying questions
from a vague request, and a coherent editable plan — and avoids the one-shot
pitfall with a runnable demonstration rather than an assertion. The four required
parameters, the `FeedbackProvider` seam and the `LLMClient` protocol are
reused unchanged by every later lab.