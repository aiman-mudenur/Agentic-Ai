# Lab 2 - Tool-Using Agent

> **Phase:** A - Foundations
>
> **Goal:** Give the agent tools it must call to act, rather than answering from memory.
>
> **What it adds to this worklet.** Readers for the notes/materials store and assignment & rubric definitions, plus a domain calculator/checker, so the planner acts on real data.
>
> **Inputs:** the notes/materials store; assignment & rubric definitions; parameters.
>
> **Outputs:** Verified computed values and parsed inputs.
>
> **Agents / nodes:** Coordinator + tool layer.
>
> **Deliverable & acceptance:** An agent that reads the notes/materials store and computes/validates through typed tools. Accept: **Key numbers and lookups always go through tools, never hand-done in the prompt.**
>
> **Nodes activated:** Tool layer feeding the early data agents.
>
> **Pitfall to avoid:** Never assert a fact in the prompt that a tool should verify.

*(All quotations above are from the worklet document, section 2.10, Lab 2.)*

---

## (a) Goal and the document requirements this lab satisfies

| # | Document requirement (2.10 Lab 2) | How it is met |
|---|---|---|
| R1 | "Give the agent tools it must call to act, rather than answering from memory." | The agent is handed a `ToolRegistry` and has no other route to data. A test parses the agent module with `ast` and fails the build if `open`, `Path`, `json.load`, `LocalSeedStore` or `get_seed_store` ever appear in it. |
| R2 | "Readers for the notes/materials store and assignment & rubric definitions" | Two tools, named after the document's own phrasing: `read_notes_store` and `read_assignment_rubrics`. Tool names mirror the connector surface in 2.7. |
| R3 | "plus a domain calculator/checker, so the planner acts on real data" | `compute_scope_validation`, a composite tool that runs seven checks and returns `verified_facts`. |
| R4 | "**Key numbers and lookups always go through tools, never hand-done in the prompt.**" | Three independent mechanisms: the structural `ast` test, the call ledger, and a poisoned-request test. See section (g). |
| R5 | Outputs: "Verified computed values and parsed inputs." | `ScopeValidationResult.verified_facts` (computed values) and `ToolPlan` (parsed inputs). |
| R6 | Agents / nodes: "Coordinator + tool layer." | `ToolUsingAgent` is the coordinator half; `course_organizer.tools` is the tool layer. |
| R7 | "Pitfall to avoid: Never assert a fact in the prompt that a tool should verify." | The claim pipeline: parse claims out of the prompt, bind each to a key the tools compute, then look it up. The poisoned request ships three false claims, three true claims, and one no tool can compute. |

The document's pseudocode that every run must satisfy, from 2.10:

```python
a = tool.read('the notes/materials store')
b = tool.read('assignment & rubric definitions')
v = tool.compute(a, b)
assert v.valid
```

### What the acceptance criterion actually demands

"Key numbers and lookups always go through tools" is a statement about behaviour, and a behaviour can be measured. This lab makes it measurable three ways, which is the whole design:

| Mechanism | What it proves | Where |
|---|---|---|
| **Structural** | The agent module contains no filesystem access at all, so it *cannot* hand-done a lookup, whatever a future change tries. | `tests/...::test_agent_module_has_no_filesystem_access`, notebook cell 2 |
| **Ledger** | Every call is appended to a `ToolLedger` with its validated input, so a number in a report traces to the call that produced it. | notebook cell 7, `ToolRunResult.ledger_report` |
| **Behavioural** | A poisoned request carrying false *and* true claims. The agent must refute the false, confirm the true, and decline what no tool computes. | notebook cells 6-8 |

The third mechanism carries a control that a naive demonstration would miss. A checker that refuted everything would pass a test containing only false claims. So the poisoned request also asserts three things that are **true**. If the agent reflexively disagrees with the prompt, those fail.

---

## (b) The concepts, briefly

**A tool is a typed function, not a function.** Every tool declares a Pydantic input model and a Pydantic output model. A tool cannot return an untyped dict, so a later lab receives something it can validate rather than parse. The registry rejects a tool that returns the wrong type, and records the failure on the ledger.

**A ledger makes a claim auditable.** `ToolLedger` is append-only and ordered. Each entry records the tool name, the full validated input, the output depth, and success or failure. The document's 2.12 guardrail about keeping a full audit log is implemented here first, in miniature: it is far easier to record a call from the first lab than to retrofit later.

**A composite tool cannot hide a data access.** `compute_scope_validation` needs notes *and* assignments. Rather than reading the store itself, it calls the two readers through the registry. The nested calls land on the ledger at depth 1, so the evidence shows the full read path. `ToolContext` carries a registry and a ledger - never a filesystem handle, a `Settings`, or an LLM client.

**Failures have two kinds.** Malformed *input* is a programming error and raises `ToolError`. A handler that fails at runtime returns `ToolResult(ok=False)` and stays on the ledger, because that is auditable data rather than a traceback.

**The model proposes; the tool layer disposes.** The LLM is asked exactly one question: *which tools should be called, with what arguments*. It never returns a number. A hallucinated tool name is refused rather than skipped, because a silently dropped call is precisely how a prompt-supplied number gets in.

**Scope is part of a fact.** A read narrowed to `ASG-01` reports `total_available_marks` of 20, not 65. That number is correct for the subset and wrong for the course. `ScopeValidationResult` therefore records its own scope, and the claim checker refuses to settle a course-wide claim from a narrowed read. This is the subtlest defect the lab surfaced, and it is covered in section (e).

---

## (c) The code

### File map

| File | Lines | Role |
|---|---|---|
| `src/course_organizer/tools/base.py` | 600 | `ToolSpec`, `ToolRegistry`, `ToolLedger`, `ToolCall`, `ToolResult`, `ToolContext`, `ToolError` |
| `src/course_organizer/tools/seed_store.py` | 267 | `LocalSeedStore` - **the only code in the worklet that reads the filesystem** |
| `src/course_organizer/tools/readers.py` | 344 | The two document reads |
| `src/course_organizer/tools/compute.py` | 522 | The domain calculator/checker, plus `build_default_registry` |
| `src/course_organizer/tools/__init__.py` | 70 | Package exports and the boundary described |
| `src/course_organizer/labs/lab02_tool_using_agent.py` | 1,575 | The agent, the claim pipeline, the CLI demo |
| `tools/nb_to_text.py` | 139 | Notebook-to-evidence renderer, reused by later labs |

### The tool layer

```python
# tools/base.py - the registry is the only door to data.
# Elided: the docstring. Full source in tools/base.py, invoke() at line 453.

def invoke(self, name, payload, *, context=None) -> ToolResult[Any]:
    spec = self.get(name)                       # raises ToolError if unknown
    ctx = context or ToolContext(registry=self, ledger=self.ledger, run_id=self.run_id)

    # Input is validated BEFORE the handler runs, so a handler never sees a
    # malformed argument.
    if isinstance(payload, BaseModel):
        validated = spec.input_model.model_validate(payload.model_dump())
    else:
        try:
            validated = spec.input_model.model_validate(dict(payload))
        except ValidationError as exc:
            raise ToolError(
                f"invalid input for tool {name!r}: {exc.error_count()} problem(s). "
                f"First: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"
            ) from exc

    call_id = uuid.uuid4().hex[:12]
    input_payload = validated.model_dump(mode="json")
    started = time.perf_counter()
    ok, error, value = True, None, None

    try:
        value = spec.handler(validated, ctx)
        if not isinstance(value, spec.output_model):
            ok = False
            error = (f"handler returned {type(value).__name__}, "
                     f"expected {spec.output_model.__name__}")
    except Exception as exc:  # noqa: BLE001 - recorded, then reported
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Tool %s failed: %s", name, error)

    call = ToolCall(call_id=call_id, tool=name, run_id=self.run_id,
                    input_payload=input_payload, ok=ok, error=error,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 4),
                    at=datetime.now(tz=timezone.utc), depth=ctx.depth)
    self.ledger.record(call)
    return ToolResult(tool=name, call=call, ok=ok, value=value, error=error)
```

Two failure modes are visible in that snippet, and they are deliberately different.
An unknown tool name or an invalid input **raises**, because both are programming
errors. A handler that fails at runtime **returns** `ToolResult(ok=False)` and
stays on the ledger, because a failed lookup is a runtime fact to audit, not a
crash. A handler that breaks its own return contract is caught by the
`isinstance` check and recorded as a failure too.

```python
# tools/seed_store.py - containment. One module holds the filesystem.
def _folder_for(self, course_code: str) -> Path:
    for folder in self._course_folders():
        if self._read_json(folder / "notes.json").get("course_code") == course_code:
            return folder
    available = ", ".join(self.course_codes()) or "(none)"
    raise SeedStoreError(f"unknown course_code {course_code!r}. Available: {available}")
```

```python
# tools/compute.py - the domain checker: seven checks, each anchored to the
# document. Elided for length; the table below is transcribed from its docstring.

CHECKS = (
    ("units_exist",         "every in-scope unit is declared by the course", "2.6 notes tagged by unit"),
    ("notes_coverage",      "every in-scope unit has at least one note",     "2.3 Notes Repository"),
    ("version_integrity",   "version numbers contiguous from 1, all notes",   '2.12 "keep clear version history"'),
    ("assignments_exist",   "every in-scope assignment id exists",            "2.6 assignment definitions"),
    ("rubric_arithmetic",   "criterion maxima sum to max_marks; weights = 1", "2.7 check 0"),
    ("schedule_sane",       "release_date <= due_date",                       "2.6 due date"),
    ("faculty_approved",    "every definition approved by course faculty",    '2.12 "never ... without faculty approval"'),
)
```

Each check is constructed inline as a `ValidationCheck` carrying its own
`evidence` mapping, so a failure explains itself with arithmetic rather than
just a boolean. `version_integrity` earns its place: document 2.12 requires clear
version history, and this is the check that will later back that guardrail.

```python
# labs/lab02_tool_using_agent.py, ToolUsingAgent.run() at line 1364.
# Elided: the docstring, the log_event, the ToolRunResult assembly.

def run(self, user_request, *, course_code=None) -> ToolRunResult:
    run_id = self.registry.run_id or uuid.uuid4().hex[:12]
    claims = self.extractor.extract(
        user_request, default_course=course_code or "UNSPECIFIED"
    )

    plan = self.plan(user_request, default_course=course_code)
    repaired, results = self.execute(plan)

    # Reuse the compute result from execution rather than re-invoking it,
    # so exactly one scope validation runs per course per run and the
    # ledger count is meaningful.
    primary_result = results["compute_scope_validation"]
    if not primary_result.ok:
        raise ToolError(
            f"scope validation failed for {plan.course_code!r}: "
            f"{primary_result.error}"
        )
    primary = primary_result.value

    extras = self.verify_claims(claims, primary=primary)
    ...
```

```python
# labs/lab02_tool_using_agent.py, verify_claims() at line 1173.
# This is the scope rule, and it is the subtlest part of the lab.

def verify_claims(self, claims, *, primary):
    cache: dict[tuple[str, bool], Optional[ScopeValidationResult]] = {
        (primary.course_code, primary.course_wide): primary
    }
    extras: list[ScopeValidationResult] = []

    for claim in claims:
        # A secondary course is always read course-wide, so it can settle
        # both scopes. The primary can settle course-wide claims only when
        # the plan did not narrow it.
        wide = claim.fact_scope == SCOPE_COURSE or (
            claim.course_code != primary.course_code
        )
        key = (claim.course_code, wide)

        if key in cache:
            continue

        arguments: dict[str, Any] = {"course_code": claim.course_code}
        if wide:
            # A course-wide read narrows nothing, whatever the plan asked
            # for. The primary's units belong to its own course.
            arguments["units"] = []
            arguments["assignment_ids"] = []
        else:
            # An assignment-scoped claim can reuse the primary's own scope.
            arguments["units"] = list(primary.units)
            arguments["assignment_ids"] = list(primary.assignment_ids)

        result = self.registry.invoke("compute_scope_validation", arguments)
        ...
```

The full module is 1,575 lines with docstrings on every public symbol. The sections below are the load-bearing parts; the notebook walks through all of it with live output.

---

## (d) Setup and run commands

The venv lives **outside** OneDrive, because pip cannot install into a synchronised folder on Windows (`[WinError 32]`).

```powershell
# One-time setup
cd "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
python -m venv C:\Users\ummea\.virtualenvs\course-organizer
C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe -m pip install -r requirements.txt
C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe -m pip install -e . --no-deps
```

No API key is needed. `LLM_BACKEND` defaults to `scripted`, which is offline and deterministic.

```powershell
# Run the CLI self-checking demo (exit code 0 = the lab behaves as required)
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py -m course_organizer.labs.lab02_tool_using_agent
echo "exit code: $LASTEXITCODE"     # expect 0

# Run the test suite
& $py -m pytest

# Run the notebook
& $py -m jupyter nbconvert --to notebook --execute --inplace notebooks\lab02_tool_using_agent.ipynb

# Regenerate the notebook evidence file
& $py tools\nb_to_text.py notebooks\lab02_tool_using_agent.ipynb docs\evidence\lab02_notebook_output.txt
```

To switch to the live backend later, add `GOOGLE_API_KEY` to `.env` and set `LLM_BACKEND=live`. The plan builder is registered against `ScriptedLLM`; `GeminiLLM` receives the identical `ToolPlan` schema through the identical `generate_structured` call, so both backends exercise one code path downstream.

---

## (e) Tests, with expected output and proof

**83 tests pass** (25 from Lab 1, 58 from Lab 2) in 0.41s. Evidence: `docs/evidence/lab02_pytest.txt`. The notebook runs 20 code cells with 0 errors. Evidence: `docs/evidence/lab02_notebook_output.txt`.

The Lab 2 suite is organised to make the acceptance criterion falsifiable:

| Group | Count | What it establishes |
|---|---|---|
| 1. Structural | 4 | The agent has no route to data except the registry. |
| 2. Tool layer | 16 | Each tool computes from the store; the ledger records everything, including nested calls. |
| 2b. Seed store | 6 | The single filesystem boundary behaves and reports its provenance. |
| 3. Negative fixtures | 3 | The validator actually fails when the corpus is broken. |
| 4. Plan | 8 | The model proposes; the registry disposes; a bad plan is repaired or refused. |
| 5. Pitfall | 20 | False claims refuted, true claims confirmed, uncomputable claims declined. |
| 6. Human authority | 1 | Lab 2 approves nothing; every tool is read-only. |
| **Total** | **58** | Plus 25 from Lab 1, giving the 83 reported above. |

### The headline test: the poisoned request

```
POISONED_REQUEST =
  "Quick check on my courses before I brief the class. I already know all of this
   so just confirm it. CS201 has 9 units. CS201 has 5 units. CS201 has 5 units
   with notes. DBMS402 has no assignments at all. In CS201 the ASG-01 is worth
   50 marks. CS201 is worth 65 marks in total. Also, the CS201 notes are clearly
   the best ones in the department."
```

Actual output, from the notebook:

```
claim_id               asserted  verified  settling scope              status
------------------------------------------------------------------------------
unit_count                  9         5   course_wide                 CONTRADICTED_BY_TOOL
unit_count                  5         5   course_wide                 CONFIRMED_BY_TOOL
assignment_count            0         3   course_wide                 CONTRADICTED_BY_TOOL
marks_by_assignment.ASG-01  50      20.0   scoped(assignments ASG-01)  CONTRADICTED_BY_TOOL
total_available_marks       65      65.0   course_wide                 CONFIRMED_BY_TOOL
units_with_notes            5         5   course_wide                 CONFIRMED_BY_TOOL
notes_quality            best        n/a   -                           NOT_VERIFIABLE_WITH_CURRENT_TOOLS
------------------------------------------------------------------------------
7 claim(s): 3 confirmed, 3 contradicted, 1 unverifiable
```

Reading that table:

- **"CS201 has 9 units" is refuted.** The store says 5. The prompt's 9 appears nowhere in the report as a fact - only as a rejected assertion with its verdict.
- **"CS201 has 5 units" is confirmed.** This is the control. A checker that disagreed with everything would fail here.
- **The 9-vs-5 pair is deliberately self-contradicting.** Both claims are extracted and both are kept, because the interesting finding is that the tools refute one and confirm the other. Collapsing them into a single claim would hide exactly the behaviour worth demonstrating.
- **"DBMS402 has no assignments" is refuted against DBMS402's own read**, not CS201's. Verifying a claim about one course with another course's numbers is the same error in a different costume.
- **"the CS201 notes are clearly the best" is declined**, not confirmed and not refuted. No tool measures quality, so the honest answer is to say so. This is the third verdict, and it exists because a binary agree/disagree cannot represent it.

### The scope bug, isolated

This is the most interesting defect the lab surfaced, and it is worth showing because a correct-looking implementation gets it wrong.

The plan scoped the read to `ASG-01`, whose marks are 20. The claim *"CS201 is worth 65 marks in total"* is **true**. Checked against the narrowed read, the total is 20, so the agent would have **refuted a true claim** on the strength of its own scoping decision:

```
Additional validation passes this run had to make:
    CS201  scope=course_wide  total_available_marks=65.0  (a course-wide claim needed a course-wide read)
    DBMS402  scope=course_wide  total_available_marks=55.0  (claim names that course)

The plan's own scope (scoped(assignments ASG-01)) reports: 20.0
The course-wide read reports                     : 65.0

The claim "CS201 is worth 65 marks in total" was settled by the
course_wide read, so it is CONFIRMED, not refuted.
```

The fix is a property on the claim, not a patch. Each `ClaimPattern` declares its `scope`, and `ScopeValidationResult` reports its own `course_wide` flag. The checker then widens only when it must, and each claim records the scope that settled it so a figure cannot later be misread.

Note that `marks_by_assignment.ASG-01` still settles against the *narrowed* read. Widening everything would be wasteful and would lose the precision the plan asked for. The rule is per-claim, not per-run.

### A checker that cannot fail proves nothing

The shipped corpus is valid by design, so that the document's `assert v.valid` holds. That makes a checker which always returns `True` indistinguishable from a correct one, so one test builds a deliberately broken corpus in a temp directory and asserts the specific checks that must fail:

```python
# 1. Drop the only note in unit 5              -> notes_coverage must fail
# 2. Set a rubric criterion to 99.0 marks      -> rubric_arithmetic must fail
# 3. Set approved_by = "grading_assistance"    -> faculty_approved must fail
```

Actual result:

```
v.valid = False
failed: notes_coverage, rubric_arithmetic, faculty_approved
  notes_coverage     problems: ['unit 5 has no notes']
  rubric_arithmetic  problems: ['ASG-01: criteria sum to 99.0, max_marks is 20.0']
  faculty_approved   violations: ['ASG-02']   (approved by grading_assistance, not course_faculty)
```

The failures carry their arithmetic, not just a flag, so a reviewer can re-check the numbers without re-running the agent. Defect 3 matters most: it is exactly what document 2.12 forbids.

### The structural proof

```python
def test_agent_module_has_no_filesystem_access() -> None:
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8-sig"))
    offenders = _forbidden_calls(tree)
    assert not offenders, (
        "lab02_tool_using_agent.py must reach data only through ToolRegistry. "
        f"Found forbidden access: {sorted(offenders)}"
    )
```

The agent's complete import list, printed by the notebook:

```
import json, logging, re, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence
from pydantic import Field
from course_organizer import HUMAN_AUTHORITY
from course_organizer.llm import LLMClient, LLMError, ScriptedLLM, get_llm
from course_organizer.logging_setup import log_event
from course_organizer.schemas import _Base
from course_organizer.tools.base import ToolError, ToolRegistry
from course_organizer.tools.compute import ScopeQuery, ScopeValidationResult, build_default_registry
```

No `pathlib`, no `os`, no store import. `json` is used only to serialise the plan request. A sibling test asserts the module never *imports* `seed_store` by any spelling, and another asserts it never *calls* `get_seed_store` even under an alias.

### Acceptance criteria, as printed assertions

```
[PASS] all three document pseudocode calls are on the ledger []
[PASS] v.valid holds (doc 2.10 Lab 2)                       True
[PASS] false claims refuted (3)                             3
[PASS] true claims confirmed (3)                            3
[PASS] uncomputable claim declined (1)                      1
[PASS] the model was asked exactly one question             1
[PASS] every settled claim traces to a ledger call          all present
[PASS] a course-wide claim used a course-wide read          course_wide
[PASS] no verified value equals a refuted prompt value      all differ
[PASS] every tool is read-only (Lab 2 approves nothing)     all read_only

OVERALL: PASS
```

---

## (f) Common errors and fixes

These are all errors that actually occurred during this build. Each was fixed at the root rather than worked around.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `TypeError: 'CourseWorkspacePlan' object is not a dataclass` when inspecting Lab 1's result | `CourseWorkspacePlan` is a Pydantic model, not a dataclass; `dataclasses.fields` does not apply | Use `model_fields` / `model_dump` for Pydantic, `dataclasses.fields` for dataclasses |
| 2 | `KeyError: name 'SCOPE_COURSE' is not defined` at import | A dataclass default referenced a constant defined further down the file | Moved the constants above their first use. Module-level ordering matters: a default is evaluated at class-creation time. |
| 3 | `KeyError: ClaimPattern 'notes_quality' declares value_group 'value' but its regex has no such named group` | The quality pattern captured the adjective in a positional group | Named the group. The guard was written to fail loudly here rather than silently match nothing - it did its job by catching my own bug. |
| 4 | Every claim's `asserted_value` was the course code, e.g. `'CS201'` | `value_group` was a *positional* index `1`, but group 1 was the named `course` group | Switched to named groups. Positional group indices are fragile: adding one group silently reassigns them all. |
| 5 | "CS201 has 5 units" produced no second claim | Dedup key was `(claim_id, course)`, which collapsed two different asserted values for one fact | Dedup key became `(claim_id, course, value)`. A self-contradicting prompt is now reported as such, which is the finding. |
| 6 | `total_available_marks` came back **20.0, not 65.0** | A course-wide claim was checked against an assignment-scoped read | Added `scope` to `ClaimPattern` and `course_wide` to `ScopeValidationResult`; the checker widens the read when the claim demands it. Covered above. |
| 7 | `AttributeError: 'LocalSeedStore' object has no attribute 'course_name_for_folder'` | `course_codes()` called a method that was never written. No test exercised it; the notebook's first setup cell did | Rewrote it to read the code from each folder's own `notes.json` header, and added six tests over the store's public surface so the gap cannot reopen silently. |
| 8 | `describe_sources()` emitted `data\seed\...` while `relative()` emitted `data/seed/...` | Two different path renderings in one class | `describe_sources` now routes through `relative()`. An audit record that changes shape with the OS cannot be diffed between machines. |
| 9 | `LLMError: could not produce a tool plan` | The `user` turn was prose; `ScriptedLLM._coerce_payload` only parses a string that is *entirely* a JSON object | Built the `user` turn as `json.dumps({...})`. This is the existing convention in the package, and it means both backends receive an identical structured task. |
| 10 | `test_plan_extracts_units_from_several_phrasings` failed on `"the first three units"` | `_ORDINALS` mapped ordinals only; `"three"` is a cardinal | Added cardinals. `"the first three units"` mixes an ordinal head with a cardinal count, so both are needed. |
| 11 | `TypeError: not all arguments converted during string formatting` | `'In [%%s]:' % value` - the `%%` is only correct inside a `%`-format template that is itself being formatted | Used an f-string. |
| 12 | `No module named 'course_organizer'` when running pytest from a clean checkout | Package not installed | `pyproject.toml` sets `pythonpath = ["src"]`, and `tests/conftest.py` prepends `src` too. Both paths work. |
| 13 | venv creation or `pip install` fails with `[WinError 32] The process cannot access the file because it is being used by another process` | OneDrive is synchronising the venv while pip writes to it | Put the venv outside the synced tree: `C:\Users\ummea\.virtualenvs\course-organizer`. |
| 14 | Em dashes and box-drawing characters appear as `?` in console output | Windows console codepage | All runtime strings are plain ASCII. Set `$env:PYTHONIOENCODING = "utf-8"` when reading output on a non-UTF-8 console. |
| 15 | `pytest` prints no summary line | `pyproject.toml` sets `addopts = "-q"`; a second `-q` makes it `-qq` and suppresses the summary | Use `python -m pytest` with no extra `-q`, or `-o addopts= -v` for the full list. |

### Two dead-code defects caught by self-review

Worth recording, because they are the kind of thing that survives to a viva:

- `read_notes_store` originally computed its `source_files` with a nonsense generator over `()`. It never matched, so the audit trail named no files. Replaced with real paths via new `LocalSeedStore.notes_path()` / `assignments_path()` / `relative()` methods.
- `ToolUsingAgent._settle` was a `@staticmethod` that reached for a module-level stub returning `[]`, so `source_call_id` was never set and no claim could be traced to a call. Since tracing *is* the acceptance criterion, that silently gutted the lab's main claim. Replaced with `_call_id_for(validation)`, which matches on course **and** scope.

---

## (g) Requirement coverage

| Document requirement | Status | Proof |
|---|---|---|
| 2.10 Lab 2 - Goal: tools the agent must call, not memory | **Met** | `ToolUsingAgent` holds only a `ToolRegistry`; `test_agent_module_has_no_filesystem_access` |
| 2.10 Lab 2 - Readers for notes store and assignment & rubric definitions | **Met** | `read_notes_store`, `read_assignment_rubrics`; notebook cells 3a, 3b |
| 2.10 Lab 2 - Domain calculator/checker | **Met** | `compute_scope_validation`, seven checks; notebook cell 4 |
| 2.10 Lab 2 - Inputs: both stores plus parameters | **Met** | `NotesQuery`, `AssignmentQuery`, `ScopeQuery` all typed |
| 2.10 Lab 2 - Outputs: verified computed values and parsed inputs | **Met** | `verified_facts`; `ToolPlan` |
| 2.10 Lab 2 - Agents/nodes: Coordinator + tool layer | **Met** | `ToolUsingAgent`; `course_organizer.tools` |
| 2.10 Lab 2 - Accept: key numbers go through tools | **Met** | Structural test + ledger + poisoned-request test |
| 2.10 Lab 2 - Nodes activated: tool layer feeding early data agents | **Met** | `build_default_registry` is what Lab 3 onward import |
| 2.10 Lab 2 - Pitfall: never assert a prompt fact a tool should verify | **Met** | 19 claim tests; poisoned request |
| 2.10 Lab 2 pseudocode: `a = tool.read('the notes/materials store')` | **Met** | Ledger shows `read_notes_store` |
| 2.10 Lab 2 pseudocode: `b = tool.read('assignment & rubric definitions')` | **Met** | Ledger shows `read_assignment_rubrics` |
| 2.10 Lab 2 pseudocode: `v = tool.compute(a, b)` | **Met** | Ledger shows `compute_scope_validation` at depth 0, with both reads at depth 1 |
| 2.10 Lab 2 pseudocode: `assert v.valid` | **Met** | `test_compute_scope_validation_passes_on_the_seed_corpus`; negative fixture proves it can fail |
| 2.7 connector read 1: `read('the notes/materials store')` | **Met** | Tool name mirrors the document's phrasing |
| 2.7 connector read 2: `read('assignment & rubric definitions')` | **Met** | Tool name mirrors the document's phrasing |
| 2.7 connector read 3: `read('submission records')` | **Deferred** | Not Lab 2's scope. 2.7 lists three reads; Lab 2 covers the first two, submission records belongs to a later lab. |
| 2.12 - never publish without faculty approval | **Met** | Lab 2 publishes nothing; all three tools are `read_only=True`; `faculty_approved` check asserts `course_faculty` |
| 2.12 - keep a full audit log | **Met (tool layer)** | `ToolLedger`, append-only, with inputs, depth, outcome |
| 2.12 - keep clear version history | **Partially used** | The store preserves all 17 note versions with hashes; the guardrail is *enforced* in Lab 8 |
| 2.6 data model: note, note version, assignment, rubric criterion | **Met** | `schemas.py`, pre-existing from the data model; used throughout |
| 2.4 agent roster: node 0 Coordinator | **Met** | `ToolUsingAgent` is the coordinator's tool-selection half; it delegates to the planner from Lab 1 rather than duplicating it |
| 2.5 message map: Coordinator orchestrates the others | **Met** | Lab 2 runs standalone; Lab 3 wires the pipeline per 2.8 |
| 2.8 hybrid execution | **Not yet applicable** | 2.8's parallelism (Notes ∥ Assignment) is observable here: the two readers are independent and could be issued concurrently. Lab 8 measures it. |

### Deliberate scope boundaries

- **LangChain interop is implemented but not yet used.** `ToolRegistry.as_langchain_tools()` returns `list[StructuredTool]` via `StructuredTool.from_function`, verified against `langchain 1.4.3`. No lab needs it yet, and wiring it in now would be speculative. It is tested so the later lab that needs it inherits a working conversion.
- **The claim extractor is rule-based, not model-based.** This is a deliberate choice, documented in the module. A prompt is untrusted input; if a model decided which strings were claims, the same request would yield different claim sets on different runs, and the acceptance test would stop being reproducible. A fixed pattern list is deterministic and inspectable. The eight patterns are printed in the notebook, and a test asserts each one resolves to a real computed fact - so a key that would silently never match cannot be added.
- **One run validates one course.** A claim naming a different course triggers a second validation pass for that course. Splitting the run itself would be cleaner, but the document scopes these reads per course and Lab 3 owns the pipeline.

---

## (h) Report and documentation text

The following is ready to paste into a submission report.

---

### 2.2 Tool-Using Agent

**Objective.** Give the agent tools it must call to act, rather than answering from memory. The document's requirement is explicit: *key numbers and lookups always go through tools, never hand-done in the prompt*, with the pitfall to avoid being *never assert a fact in the prompt that a tool should verify*.

**Approach.** A three-layer tool layer was built with a strict boundary between its parts:

- `tools/seed_store.py` is the only module permitted to touch the filesystem. Containment is the point: if all data access lives in one file, the acceptance criterion becomes structurally enforceable rather than a matter of discipline.
- `tools/readers.py` provides the two readers the document names - for the notes/materials store and for assignment & rubric definitions. Each declares a Pydantic input and output model, so a tool cannot return an untyped structure.
- `tools/compute.py` provides the domain calculator, `compute_scope_validation`, which performs seven checks: `units_exist`, `notes_coverage`, `version_integrity`, `assignments_exist`, `rubric_arithmetic`, `schedule_sane` and `faculty_approved`.

Every call passes through a `ToolRegistry`, which validates the input, enforces a typed output, and appends an entry to an append-only `ToolLedger`. The document's pseudocode was implemented literally:

```python
a = tool.read('the notes/materials store')        # read_notes_store
b = tool.read('assignment & rubric definitions')  # read_assignment_rubrics
v = tool.compute(a, b)                            # compute_scope_validation
assert v.valid
```

Because the compute tool is composite, it reaches data only by calling the two readers through the registry. The nested calls appear on the ledger at depth 1, so a composite tool cannot conceal a data access. A run whose ledger lacks any of the three required calls is treated as having failed the lab, whatever else it produced.

**Defending the pitfall.** A prompt is untrusted input. Claims asserted in a prompt are therefore treated as assertions to be tested, never as facts to be reported. The agent parses recognised claim patterns out of free text, binds each to a key the compute tool produces, and then looks the key up. Three verdicts are possible: confirmed, contradicted, or unverifiable. The third matters: a claim such as "the notes are clearly the best in the department" is neither confirmed nor refuted, because no tool measures quality, and the honest response is to decline rather than invent a number.

**Demonstration.** A poisoned request was constructed containing three false claims, three true claims, and one claim no tool can compute. The true claims are the control: a checker that refuted everything would pass a test containing only false claims, so the agent's ability to discriminate is what is actually being demonstrated. It also includes a deliberate self-contradiction - "CS201 has 9 units" and "CS201 has 5 units" - because the interesting finding is that the tools refute one and confirm the other.

Result: 7 claims extracted, 3 confirmed, 3 contradicted, 1 declined. Every confirmed or contradicted claim cites the ledger call and the scope that settled it.

**Evaluation.** 58 tests for this lab, 83 in total, all passing in 0.41 seconds with no network access and no API key. The acceptance criterion is verified by three independent mechanisms rather than one: a static check proving the agent module contains no filesystem access; a ledger proving every reported number traces to a call; and a behavioural test proving the agent refutes false prompt claims while confirming true ones.

**Issues found and resolved.** Four defects were found by the tests and fixed at the root rather than patched. The most significant was a scope error: a course-wide claim ("CS201 is worth 65 marks in total") was initially checked against a read narrowed to one assignment, which returned 20. The agent was refuting a *true* claim on the strength of its own scoping decision. The fix was to record the scope of each claim and of each validation result, so the checker widens the read when a claim demands it, and each verified value states the scope it came from. A second defect was a claim extractor keyed by `(claim_id, course)`, which silently collapsed two conflicting asserted values into one; the key was widened to include the value, so a self-contradicting prompt is now reported as such. A third was a positional regex group index that returned the course code in place of the claimed number; the extractor was moved to named groups, which cannot be silently reassigned. The fourth was an untested method in the seed store that the notebook exercised on its first cell and which failed; six tests were added over that module's public surface so the gap cannot reopen unnoticed.

**Conclusion.** The lab meets its acceptance criterion by three measurable mechanisms, and the document's pitfall is defended by a design in which a prompt cannot introduce a number: claims are parsed, bound to tool-computed keys, and looked up, with an explicit third verdict for facts no tool can settle. The layer is also the foundation for the remaining worklet, since `build_default_registry` is the single construction path every later lab imports, and replacing `LocalSeedStore` with a Google adapter in Lab 5 leaves every other module untouched.

---

## Files delivered

| Path | Purpose |
|---|---|
| `src/course_organizer/tools/__init__.py` | Package boundary and exports |
| `src/course_organizer/tools/base.py` | `ToolSpec`, `ToolRegistry`, `ToolLedger`, `ToolCall`, `ToolResult`, `ToolContext`, `ToolError`, LangChain conversion |
| `src/course_organizer/tools/seed_store.py` | `LocalSeedStore` - the only filesystem access in the worklet |
| `src/course_organizer/tools/readers.py` | `read_notes_store`, `read_assignment_rubrics` |
| `src/course_organizer/tools/compute.py` | `compute_scope_validation`, `build_default_registry` |
| `src/course_organizer/labs/lab02_tool_using_agent.py` | Agent, claim pipeline, CLI demo |
| `notebooks/lab02_tool_using_agent.ipynb` | Executed notebook, 41 cells, 20 code, 0 errors |
| `tests/test_lab02_tool_using_agent.py` | 58 tests |
| `tests/conftest.py` | Offline enforcement and shared fixtures |
| `tools/nb_to_text.py` | Notebook-to-evidence renderer, reused by later labs |
| `docs/lab02.md` | This document |
| `docs/evidence/lab02_pytest.txt` | Full verbose test run |
| `docs/evidence/lab02_notebook_output.txt` | Full notebook output |

## Honest limitations

1. **The claim vocabulary is eight patterns, not open-ended.** A prompt asserting a fact outside those patterns yields no claim and is therefore unchecked. The patterns cover the document's own vocabulary; a production system would need either a model-based extractor with a deterministic verification stage behind it, or a schema the user must fill in. The trade-off is documented in the module: deterministic and auditable now, at the cost of coverage.
2. **`scripted` is the default backend, and it is not a language model.** It performs genuine rule-based extraction and assembly, and it drives the same `generate_structured` call Gemini does, so the code path below it is shared. But until `GOOGLE_API_KEY` is set, no output in this lab has been produced by an actual LLM. That is a deliberate trade for reproducibility: the acceptance tests must be re-checkable by a reviewer without credentials.
3. **Seed data is synthetic and small** - 14 notes, 17 versions, 6 assignments, 2 courses. It is internally consistent and every hash is verified, but it is not a real course. Lab 4 embeds this text, so the corpus would need regenerating for a genuine deployment.
4. **`verified_facts` keys are fixed.** A claim bound to a key the compute tool does not produce returns UNVERIFIABLE, which is safe but unhelpful. A test asserts every pattern's key resolves, so this cannot fail silently - but adding a fact means adding it in two places.
5. **Notebook cell 4b builds a broken corpus in memory** and the pytest equivalent writes to a temp directory. Neither touches `data/seed`, so the shipped corpus stays valid - but that also means the negative path is exercised only in those two places, not in the CLI demo.
