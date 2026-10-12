# Lab 5 — MCP connector: one typed, governed front door
**Phase:** A — Foundations
**Goal (doc §2.10, Lab 5):** *"Expose every data source through one typed connector."*
**What it adds (doc §2.10):** *"A single connector fronting the notes/materials store, assignment & rubric definitions and submission records so every agent reaches data the same governed way."*
**Nodes activated (doc §2.10):** *"Connector used by all data-facing agents. Connector under the data agents."*
**Pitfall (doc §2.10):** *"No agent should touch a source directly; a bypass defeats governance later."*

---

## a) Goal and the document's requirements

Every requirement below is quoted from Worklet 2 (Lab 5, lines 657-669). Nothing here
is inferred beyond those lines, plus §2.3's governance clause and the §2.12 guardrails
that apply to every lab.

| # | Doc requirement (verbatim) | Where it is met |
|---|---|---|
| R1 | *"Goal: Expose every data source through one typed connector."* | `EndpointRegistry` — four endpoints, each declaring an `input_model` and an `output_model` from §2.6. `ConnectorContract` validates both directions. |
| R2 | *"Deliverable. A connector server with typed endpoints and auth; agents call only through it."* | A **real** MCP stdio server, `course_organizer.connector.mcp_server`, built on `mcp.server.fastmcp.FastMCP`. Auth is `ROLE_SCOPES` checked inside the registry, so it holds for the in-process client and the wire client alike. |
| R3 | *"Accept: All data access goes through the connector"* | An **AST** test over the whole package: only `course_organizer.connector` may import `tools.seed_store`. One module in the project opens a file. |
| R4 | *"Accept: … swapping a source needs no agent change."* | `measure_source_swap()` runs one four-line agent tuple against `SOURCE_ADAPTER=local` and `=in_memory`, and compares the agent-facing surface. Same tools, same query keys, same result type; different course name, record count and provenance. |
| R5 | *"Pitfall to avoid. No agent should touch a source directly; a bypass defeats governance later."* | The pitfall is **measured, not demonstrated by breaking something**: as `course_faculty` the connector serves 8 submission records; as `student` the identical request is refused with the missing scope named, and the refusal is on the ledger. `BypassingReader` exists only in the test module and a test forbids its name appearing anywhere in the package. |
| R6 | `read('the notes/materials store')` | `read_notes_store` |
| R7 | `read('assignment & rubric definitions')` | `read_assignment_rubrics` |
| R8 | `read('submission records')` | `read_submission_records` |
| R9 | `agents.use(connector)   # no agent touches a source directly` | `tools/readers.py::connector()` is the single seam; `memory/` and `tools/` both reach data through it. The `BypassingReader` audit asserts the alternative is absent. |
| R10 | §2.3 *"Governance layer: access control per role, version history, audit log of grades published."* | Access control: `ROLE_SCOPES`, enforced before the handler runs. Read-side audit log: `ConnectorLedger`, one row per call **including refusals**, each carrying principal, role, scope, outcome, and a 16-hex token fingerprint. Lab 6 makes it durable. |

### Where the lab's own choices are labelled as such

Three things in this lab are **not** the document's, and each says so on its own row:

* `ROLE_SCOPES` — the document says *"access control per role"* (§2.3) without
  naming roles. Stated in one table, asserted closed in both directions by a test.
* `list_courses` — the document names three reads; a lab that never enumerates
  anything cannot tell you which course codes exist. Reported as
  *"added by Lab 5, not a documented requirement."*
* `InMemoryAdapter` — the swap partner. `SOURCE_ADAPTER=in_memory` serves a
  different corpus so the swap is demonstrable with no credentials.

`EndpointSpec.documented` is decided by matching the endpoint's `doc_read` against the
document's three verbatim `read('...')` strings (`DOCUMENT_READ_PHRASES`), **not** by
an `added_by_lab` label. Using the implementing lab as a proxy for the document's
requirements inverted the traceability: all four endpoints reported themselves as
additions, including the three the document names.

---

## b) Brief concepts

### A connector is a process boundary with a policy in front of it

```
agent
  -> ConnectorClient          holds the credential, never the model
  -> MCP transport (optional) a real MCP server over stdio
  -> EndpointRegistry         auth, scope check, typed in/out, ledger
  -> SourceAdapter            the swappable half
  -> LocalSeedStore           the only code that opens data/
```

The middle column is where the governance lives. A wrapper class with the same
methods would let an agent bypass it by calling the adapter directly; the reason this
is a boundary rather than a layer is that **no code outside the package can reach the
bottom row**, and that is checked with `ast` rather than by review.

### The credential is held, not passed

The client interface has no credential parameter:

```
ConnectorClient.read(endpoint, payload)
```

A `read(endpoint, payload, token=...)` signature would let any call site present any
credential, which puts "who is calling" back in the hands of the caller — and the
caller is, in the general case, a model. `McpConnectorClient` adds the token to the
**child process's environment** at spawn time (`MCP_CONNECTOR_TOKEN`,
`MCP_CONNECTOR_ROLE`), and `server_command()` has no `--token` flag to pass, so a
transcript of the command line is not a credential leak.

That design has a cost worth stating: because the role arrives through the
environment, **serving a second role requires a second process**. Measured: 1936.4 ms
for the student's refusal spawn, 1847.3 ms for anonymous's. Reported separately from
the warm-call figures, where folding them in would make one endpoint look
pathologically slow.

### The exception type does not survive MCP on its own

`mcp/server/fastmcp/tools/base.py` formats a tool failure as text —
`ToolError(f"Error executing tool {self.name}: {e}")` — so the client receives a
string and the class is gone. A policy decision that arrives as opaque text cannot be
distinguished from a crash, and would be logged as one.

So the connector tags its own errors (`[ConnectorPolicyError] …`) and the client
decodes the tag back into the class. `WIRE_ERROR_RE` is searched rather than
anchored, because the server wraps the tagged text first. The tag is then stripped, so
the exception a caller reads is human-readable while its **type** still arrives. Both
directions are asserted: a tagged message reaching the caller would mean the decode
never ran, and a decoded type with a stripped body would throw away the explanation
with the tag.

### "Identical payloads" needs field paths, not one bit

Comparing two nested payloads with `!=` yields *"they differ"*, which does not say
where. The one field that legitimately differs between two processes — a timestamp —
then makes every endpoint look broken with nothing to point at. So both payloads are
flattened to leaf paths (`provenance.sources[0]`, `notes[3].body`) and **exactly one**
path is excluded: `provenance.fetched_at`. The exclusion is named in the output,
because "identical" achieved by comparing fewer fields than it appears to is the
failure mode worth worrying about.

### A refusal is not a failure, and the distinction must survive the wire

`ConnectorPolicyError` means *"you may not do this"* and is the policy working.
`ConnectorTransportError` means *"I could not tell you"*. Conflating them turns a
correct refusal into an incident report. The ledger records `refused` and `failed`
separately, and `test_a_misbehaving_server_is_caught_by_the_contract` checks the
boundary in the other direction too: an **untagged** error is never guessed into a
policy failure.

### Session reuse is a design decision, not an optimisation

`langchain_mcp_adapters`' `get_tools()` closes its listing session, and every
`call_tool` re-enters `create_session` (`tools.py:460-471`) — so the library's default
is a respawn per call. The correct persistent pattern is
`client.session(name)`, which `McpConnectorClient` holds open for the client's whole
life. Measured: **4 calls in 122.3 ms against a 4224.3 ms spawn**; a respawn would have
cost about 16897 ms. The comparison is the aggregate of four calls, not their mean,
because one slow call in four is enough to make a respawn look healthy.

---

## c) The code

### Files

| file | lines | what it holds |
|---|---:|---|
| `src/course_organizer/connector/errors.py` | 187 | the error hierarchy, plus `tag_for_wire` / `untag_from_wire` |
| `src/course_organizer/connector/port.py` | 253 | `SourceAdapter` protocol and `SourceDescriptor` — the swappable half's shape |
| `src/course_organizer/connector/auth.py` | 510 | `ROLE_SCOPES`, `PrincipalRegistry`, `ensure_token` (ephemeral; never writes a file) |
| `src/course_organizer/connector/core.py` | 910 | `EndpointRegistry`, `EndpointSpec`, `ConnectorLedger`, `ConnectorCall`, `DOCUMENT_READ_PHRASES` |
| `src/course_organizer/connector/endpoints.py` | 661 | the document's three reads plus `list_courses`, with provenance on every result |
| `src/course_organizer/connector/adapters.py` | 1144 | `LocalSeedAdapter`, `InMemoryAdapter`, `GoogleWorkspaceAdapter`, `build_source_adapter` |
| `src/course_organizer/connector/client.py` | 1221 | `InProcessConnectorClient`, `McpConnectorClient`, `ConnectorContract`, `server_command`, `_LoopThread` |
| `src/course_organizer/connector/mcp_server.py` | 546 | the stdio server itself: `FastMCP` + explicit tool wrappers |
| `src/course_organizer/connector/__init__.py` | 288 | the package's public surface |
| `src/course_organizer/labs/lab05_mcp_connector.py` | 2066 | the run, the measurements, and the 18 acceptance checks |
| `tests/test_lab05_mcp_connector.py` | 1608 | 66 tests — the boundary audits and the wire behaviour |
| `tests/test_lab05_lab_module.py` | 987 | 44 tests — the lab module's evidence and its honesty |
| `notebooks/lab05_mcp_connector.ipynb` | 2023 | 54 cells, 36 code, all executed with outputs |

10,381 lines of Python in the connector package, the lab module and its two test
files. Line counts are physical lines (`wc -l`), not statements — the number is here to
be checked, not to impress.

### The boundary — only the connector may name a source

The document's pitfall, held with `ast` rather than by convention:

```python
def test_no_module_outside_connector_imports_a_source_store() -> None:
    """No module outside course_organizer.connector names the store."""
```

Four shapes are checked, because this can come back four ways: an `import` of
`seed_store`, a **constructed** adapter, a hardcoded corpus path, and the string
`data/seed` in a non-docstring literal. Two of these were real defects during the
build:

* `tools/__init__.py` re-exported `get_seed_store`, `LocalSeedStore` and
  `SeedStoreError` — three names that made a bypass a one-line affair.
* `memory/stores.py` reached the store directly to build its allowed-provenance
  allow-list. Worse than an agent's bypass, because **that set is the allow-list
  `MemoryStore.append()` enforces**: a layer able to name its own allow-list could
  name a wider one and quietly weaken the check it exists to perform. It now resolves
  lazily through `tools.readers.connector()`.

The literal scan excludes docstrings, deliberately. `memory/stores.py`'s module
docstring quotes `data/seed/...` because it describes the Lab 4 requirement it
implements; a test that failed on its own documentation would push the next author to
delete the explanation.

### `ROLE_SCOPES` — one table, closed in both directions

| role | scopes |
|---|---|
| `course_faculty` | `assignments:read`, `notes:read`, `submissions:read` |
| `teaching_assistant` | `assignments:read`, `notes:read`, `submissions:read` |
| `student` | `assignments:read`, `notes:read` |
| `anonymous` | *(none)* |

Asserted closed twice: no scope in the table is outside `KNOWN_SCOPES`, and no scope
the connector defines is unreachable from every role. A scope nobody can hold is dead
configuration; a scope outside the vocabulary is a typo that would silently deny.

### The refusal, before the read

```python
def access_matrix(registry, course_code, endpoints) -> tuple[AccessRow, ...]:
    """Every endpoint as every role -- called, not predicted from the table."""
```

16 cells (4 endpoints × 4 roles), each **actually invoked**, so the verdict is derived
by asking the connector rather than by reading its own table back. Measured: **11
allowed, 5 refused.** Every refused cell cost `0.0 ms` and every refused row in the
ledger shows `records=0` — the scope check runs before the handler, so a refusal never
pays for a read of data the caller was not going to see. The run reports the single
slowest cell and, on every run so far, it is an **allowed** one (whichever endpoint
happens to touch the most records); that identity and its millisecond value change
between runs, which is why the doc does not quote them.

The invariant, which is what the suite asserts
(`test_a_refusal_costs_nothing_like_reading_everything`), is an inequality rather than
an exact figure:

```python
assert max(r.elapsed_ms for r in refusals) < max(r.elapsed_ms for r in allowed)
```

The claim is about ordering — a refusal must not be slower than the reads it refused
to perform — not about how many milliseconds a laptop takes to refuse.

The student row is the one that carries the argument, and it is a *pair*:

```
  anonymous           none                                            R    R    R    R
  course_faculty      assignments:read, notes:read, submissions:read  2    3    8    8
  student             assignments:read, notes:read                    2    3    8    R
  teaching_assistant  assignments:read, notes:read, submissions:read  2    3    8    8
```

A student may read the notes store but not the cohort's submissions. Both halves
matter: a policy that refused everything to a student would satisfy *"students cannot
read submissions"* while being broken, so the tests assert that the refusal
**discriminates on scope** rather than merely that it exists.

### `ConnectorLedger` — a row for every call, including the ones that failed

Doc §2.3 asks for an *"audit log of grades published"*; this is the read-side half.

```python
class ConnectorCall(BaseModel):
    index: int; endpoint: str; principal: str; role: str
    token_fingerprint: str; scope: str; outcome: str; error: str
    source: str; transport: str; records: int; duration_ms: float; at: datetime
```

Two properties are load-bearing:

* **A log that only records successes cannot answer "what was attempted."** A row exists
  for refusals, naming the principal. Measured: 23 calls, 6 refused, 0 failed, 92
  records, fingerprint `d0c917b9850376bd`.
* **The token is never in the log.** 16 hex characters of SHA-256, so an auditor can
  tell *which* credential was used without the log containing something that would
  leak it.

`test_the_refusal_adds_exactly_one_named_ledger_row` asserts the individual rows by
principal and count rather than the total — a ledger that gained an unrelated row
cannot pass on the total alone, and a double-count (once at the scope check, again as
the exception propagates) would make the audit untrustworthy in the other direction.

### The wire — a real subprocess, and what it cost

```
  one server spawn, one held-open session, four calls:
    spawn                     :    4224.3ms
    list_courses                    77.9ms
    read_notes_store                13.9ms
    read_assignment_rubrics         14.5ms
    read_submission_records         16.1ms
    total of all calls             122.3ms
```

Four calls cost less than one spawn. A per-call respawn — which is what
`langchain-mcp-adapters` does by default — would have cost about **16897 ms** for the
same four calls, so the measurement is the whole argument for holding the session.

Payload parity, in-process vs over the wire, field path by field path:

| endpoint | leaves compared | verdict |
|---|---:|---|
| `list_courses` | 11 | identical |
| `read_notes_store` | **159** | identical |
| `read_assignment_rubrics` | 74 | identical |
| `read_submission_records` | 85 | identical |

The count is printed because "identical" alone is a claim with no magnitude — three
leaves compared and three hundred produce the same word. Only
`provenance.fetched_at` is excluded, and the run says so: *"a wall-clock reading taken
in a different process; every other field is compared."*

### The Jupyter fix, and why it is narrow

`mcp.client.stdio.stdio_client` is declared
`async def stdio_client(server, errlog: TextIO = sys.stderr)` — the default is
evaluated at **import** — and it hands that stream to `subprocess.Popen(stderr=…)`,
which needs a real file descriptor. `langchain_mcp_adapters.sessions` calls it with one
argument, so the default is the only seam.

Under `ipykernel` it is the wrong stream: `ipykernel/kernelapp.py` replaces
`sys.stderr` with an `OutStream` that forwards to the kernel's iopub channel and has no
OS-level file. A spawn from a notebook cell died with
`io.UnsupportedOperation: fileno` before the server ran a line.

`_fd_backed_stderr()` points the default at the interpreter's original stderr for the
duration of one block, then puts it back. Three properties, each asserted:

1. **A no-op in every ordinary process.** Otherwise every code path — CLI, tests,
   server-side use — would depend on a workaround for a kernel where the problem does
   not exist.
2. **It targets the right object.** `stdio_client` is wrapped by
   `@asynccontextmanager`, and `functools.wraps` copies `__name__`, `__doc__` and
   `__dict__` but **not** `__defaults__` — so the wrapper's defaults are `None` and
   the bound stream lives on `__wrapped__`. Patching the wrapper looks like it worked
   and changes nothing.
3. **It does not hide a failure it cannot fix.** With no descriptor 2 anywhere, the
   block yields `None`, the spawn proceeds, and `Popen` raises its own `fileno` error —
   which the client already reports as a transport failure naming the command.

### The swap — one tuple, two legs

```python
SWAP_AGENT_CODE = (
    'registry = build_default_connector(source=source, token=token)',
    'readers.use_connector(registry, token)',
    'tools = build_lab04_registry(run_id=f"{run_id}/{source}")',
    'tools.invoke("read_notes_store", {"course_code": course})',
)
```

Four lines, executed once per leg, with **no branch on `source` inside them**. Measured:

| | `SOURCE_ADAPTER=local` | `SOURCE_ADAPTER=in_memory` |
|---|---|---|
| course name | `Data Structures` | `In-memory exchange course` |
| notes | 8 | 1 |
| provenance | `data/seed/data_structures/notes.json` | `in-memory://ZZ999/notes` |
| tools | the same 4 | the same 4 |
| query keys | `('course_code',)` | `('course_code',)` |
| result type | `NotesReadResult` | `NotesReadResult` |

The course **code's value** differs between legs (`CS201` vs `ZZ999`), and that is
configuration rather than agent code: the two sources serve different corpora, so each
is asked about a course it actually has, and a deployment supplies that id alongside
`SOURCE_ADAPTER`. What the agent contributes is the query *shape*. The docstring in
`measure_source_swap` says so, because it is the first thing that looks like a hole.

### The pitfall — what governance withheld

The lab module cannot import the store to prove a bypass works; that is the property
under test, and an AST test is what holds it. So the pitfall is measured instead:

```
  as course_faculty : 8 submission record(s) for CS201
  as student       : REFUSED ConnectorPolicyError
                     principal 'student@example.edu' (role 'student') may not call
                     'read_submission_records': it needs the scope 'submissions:read'
                     and holds assignments:read, notes:read.
  same student, read_notes_store : ALLOWED, 8 note(s)
  so the refusal is scope-based, not a blanket deny

  what the governed path leaves behind:
    ledger rows for read_submission_records : 3
    names the caller                        : True
    names the missing scope                : True
```

The data a bypass would have produced is real, and obtained legitimately two lines
above. The cost of skipping the check is not that the read fails — it is that
**nothing records who asked**, which is what makes the document's *"defeats governance
later"* arguable rather than a warning.

---

## d) Setup and run

### Environment (already provisioned)

```powershell
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
```

Lab 5's dependencies are pinned in `requirements.txt` and were install-tested:

```
mcp==1.30.0                    # MUST be 1.x
langchain-mcp-adapters==0.3.2  # requires mcp<2.0.0,>=1.24.0
```

```powershell
& $py -c "from importlib.metadata import version; print(version('mcp'), version('langchain-mcp-adapters'))"
# 1.30.0 0.3.2
```

`mcp==2.2.0` makes pip raise `ResolutionImpossible`. That is a property of the
adapter's pin, not a preference.

### Run the lab, with the real MCP subprocess

```powershell
& $py -m course_organizer.labs.lab05_mcp_connector
```

Expected: eight sections, **18/18 `[PASS]`, 0 skipped**, exit code **0**.

```
==============================================================================
8. Acceptance
==============================================================================

  [PASS] the document's three reads are exposed as endpoints            read_notes_store, read_assignment_rubrics, read_submission_records
  [PASS] every documented read returned data to a full-scope caller     read_notes_store=8, read_assignment_rubrics=3, read_submission_records=8
  [PASS] every read reports where its data came from                    4 result(s), each with provenance.sources
  [PASS] the added endpoint is labelled as this project's, not the document's list_courses: added by Lab 5, not a documented requirement
  [PASS] every endpoint declares the scope it needs                     read_notes_store->notes:read, read_assignment_rubrics->assignments:read, read_submission_records->submissions:read, list_courses->notes:read
  [PASS] auth is enforced per role, not per caller                      11 allowed, 5 refused of 16 call(s)
  [PASS] every refusal arrived as a typed policy failure                ConnectorPolicyError
  [PASS] a student may read the notes but not the cohort's submissions  the refusal discriminates on scope
  [PASS] a real MCP subprocess served every call                        4 tool(s) over stdio, names=([], [])
  [PASS] the wire and the in-process client return identical payloads   list_courses: 11 leaf field(s) identical, read_notes_store: 159 leaf field(s) identical, read_assignment_rubrics: 74 leaf field(s) identical, read_submission_records: 85 leaf field(s) identical
  [PASS] a refusal survives the wire as a typed policy failure          student->ConnectorPolicyError, anonymous->ConnectorPolicyError
  [PASS] the MCP session is reused rather than respawned per call       4 calls in 122.3ms against a 4224.3ms spawn (a per-call respawn would cost about 16897ms)
  [PASS] each refused role needed its own server spawn                  2 spawn(s) for 2 role(s): the role travels in the server's environment, never on the wire
  [PASS] swapping the source needed no agent change                     same 4 tool(s), same query keys ('course_code',), same NotesReadResult
  [PASS] the swap really changed the data's origin                      local:'Data Structures' -> in_memory:'In-memory exchange course'
  [PASS] the pitfall withheld real data from the unauthorised role      8 record(s) withheld; student got ConnectorPolicyError
  [PASS] the refusal is auditable: the ledger names caller and scope    3 row(s), caller=True, scope=True
  [PASS] there is no parameter a caller could use to escalate           read(endpoint, payload)

OVERALL: PASS  (18 checks, 16 access cells, 4 MCP call(s), 8228.0ms)
```

Verbatim from `docs/evidence/lab05_cli.txt` (`--run-id lab05-evidence`). The two
timing figures will differ on your machine; the checks are not timing thresholds.

### The fast path, and what it is honest about

```powershell
& $py -m course_organizer.labs.lab05_mcp_connector --no-mcp
```

Runs in 52.7 ms and reports the five transport checks as `[SKIP]` with
`NOT RUN (--no-mcp); not evidence for this claim`, then prints a closing note saying
so again:

```
OVERALL: PASS  (18 checks, 5 skipped, 16 access cells, 0 MCP call(s), 52.7ms)
  note: the skipped checks are the transport ones. This run is not evidence for them;
        re-run without --no-mcp.
```

A fast mode that printed `OVERALL: PASS` while the deliverable it exists to
demonstrate had never been exercised would be the most dangerous feature in the file.

### All the flags

```powershell
& $py -m course_organizer.labs.lab05_mcp_connector `
  --no-mcp                # skip the subprocess; transport checks become SKIP
& $py -m course_organizer.labs.lab05_mcp_connector --course CS201
& $py -m course_organizer.labs.lab05_mcp_connector --source local
& $py -m course_organizer.labs.lab05_mcp_connector --swap-source in_memory
& $py -m course_organizer.labs.lab05_mcp_connector --run-id my-run-id
& $py -m course_organizer.labs.lab05_mcp_connector --log-level DEBUG

# the server on its own, to see what it advertises
& $py -m course_organizer.connector.mcp_server --describe
& $py -m course_organizer.connector.mcp_server --source local --seed-dir data/seed
```

### The optional live source

```bash
# .env
SOURCE_ADAPTER=google
GOOGLE_CLOUD_PROJECT=...
GOOGLE_APPLICATION_CREDENTIALS=...
```

The adapter is built and mock-tested, and **the default stays `local`** so every
number above is re-checkable offline with no cloud project. `SourceDescriptor` reports
`requires_source_credentials=True` for it, so the choice is visible rather than
implicit. `SOURCE_ADAPTER=google` with no credentials is refused with the reason, not
with a stack trace.

### Run the notebook

```powershell
& $py -m jupyter lab notebooks\lab05_mcp_connector.ipynb
```

Executed with `nbconvert --execute --inplace`; all 36 code cells have outputs, 0 error
outputs.

### Run the tests

```powershell
& $py -m pytest tests\test_lab05_mcp_connector.py -o addopts= -v   # 66 tests
& $py -m pytest tests\test_lab05_lab_module.py     -o addopts= -v   # 44 tests
& $py -m pytest                                                     # 377 passed, 1 skipped
```

> Bare `python -m pytest`, or `-o addopts= -v`. An extra `-q` meets
> `addopts = "-q --strict-markers"` and suppresses the summary line.

---

## e) Test cases, expected output, and proof

### The 16 access cells, and what each must produce

Every row is a real measured run. "Expected" is fixed by the role's scopes: a caller
either holds the endpoint's scope or does not, and the matrix must agree cell for cell
with `ROLE_SCOPES` — `test_every_cell_matches_the_role_scope_table` predicts from the
table and compares against what the connector actually did.

| role | `list_courses` | `read_assignment_rubrics` | `read_notes_store` | `read_submission_records` |
|---|---|---|---|---|
| `anonymous` | R | R | R | R |
| `course_faculty` | 2 | 3 | 8 | 8 |
| `student` | 2 | 3 | 8 | **R** |
| `teaching_assistant` | 2 | 3 | 8 | 8 |

**11 allowed, 5 refused.** The refused set is exactly `{student × read_submission_records} ∪ {anonymous × all four}`.

### The pitfall, expected to withhold real data

```
  as course_faculty : 8 submission record(s) for CS201
  as student       : REFUSED ConnectorPolicyError
                     principal 'student@example.edu' (role 'student') may not call 'read_submission_records': it needs the scope 'submissions:read' and holds assignments:read, notes:read. Doc 2.3 requires access control per role; the policy table is course_organizer.connector.auth.ROLE_SCOPES.
  same student, read_notes_store : ALLOWED, 8 note(s)
  so the refusal is scope-based, not a blanket deny

  what the governed path leaves behind:
    ledger rows for read_submission_records : 3
    names the caller                        : True
    names the missing scope                : True

  where a caller would put a credential to escalate:
    ConnectorClient.read(endpoint, payload)
    -> nothing. The token is held by the client, not the call.
```

Both halves of the student row are the acceptance criterion. A blanket deny would
satisfy "students cannot read submissions" and fail everything else.

### The suite

**110 tests across two files; 377 passed, 1 skipped in the whole project (40.65 s).**

```
  tests/test_lab05_mcp_connector.py  66 passed, 20.55 s   (with a live MCP subprocess)
  tests/test_lab05_lab_module.py     44 passed,  9.12 s   (with a live MCP subprocess)
```

Full transcript: `docs/evidence/lab05_pytest.txt`. The 1 skip is Lab 4's fastembed
opt-in (`LAB04_TEST_FASTEMBED=1`), not Lab 5's.

### The tests that matter, and what each would have caught

Grouped by what they protect, because "109 tests" is not a claim about quality.

**The boundary is structural, not conventional:**

| test | what it proves |
|---|---|
| `test_no_module_outside_connector_imports_a_source_store` | only the connector package names the store — the whole package, by AST |
| `test_no_module_outside_connector_constructs_a_source_adapter` | and none of them constructs an adapter either |
| `test_the_readers_module_does_not_reach_for_a_store` | the seam module itself is clean, so a bypass cannot hide behind a re-export |
| `test_memory_names_its_allow_list_through_the_connector_not_the_store` | `memory/stores.py`: no seed-store import, no adapter call, **no hardcoded corpus path** — the allow-list must arrive from the connector's report |
| `test_the_archives_allow_list_is_exactly_what_the_connector_reported` | …and that the rerouting still *works*: the allow-list is the union of the three reads' provenance, no wider and no narrower |
| `test_the_bypass_class_does_not_escape_into_the_package` | `BypassingReader` is confined to the test module — a demonstration that could leak is a bypass |

**`documented` and `added_by_lab` are different questions:**

| test | what it proves |
|---|---|
| `test_documentation_is_decided_by_the_documents_words_not_by_a_label` | `documented` comes from matching `doc_read` against `DOCUMENT_READ_PHRASES` |
| `test_which_lab_wrote_an_endpoint_is_not_the_same_question` | `added_by_lab` is `"Lab 5"` for all four, and is *not* used to decide `documented` |
| `test_only_the_three_document_reads_are_labelled_documented` | exactly three, and the mapping is one-to-one with the pseudocode lines |

**The wire, in both directions:**

| test | what it proves |
|---|---|
| `test_the_server_advertises_exactly_the_contract` | the spawned server exposes 4 tools, and no more — a server with extra tools would be an undocumented surface |
| `test_mcp_and_in_process_return_the_same_payload` | parity end to end, field by field |
| `test_a_datetime_survives_the_wire_as_a_datetime` | pydantic round-trips `fetched_at` as a datetime, not a string |
| `test_a_refusal_keeps_its_type_across_the_process_boundary` | `ConnectorPolicyError` arrives as that class |
| `test_an_unknown_endpoint_over_the_wire_is_a_not_found` | an unknown name is `ConnectorNotFoundError`, not a generic error |
| `test_the_credential_is_never_a_wire_argument` | no token in the argv, no token in any tool's arguments, no token in `server_names()` |
| `test_a_misbehaving_server_is_caught_by_the_contract` | a lying server — right shape, one corrupted field — is rejected by `ConnectorContract` |

**The Jupyter fix, and its limits:**

| test | what it proves |
|---|---|
| `test_the_errlog_default_lives_under_the_asynccontextmanager_wrapper` | the wrapper's `__defaults__` is `None`; the bound stream is on `__wrapped__` |
| `test_the_stderr_default_is_only_touched_when_it_has_to_be` | a no-op in an ordinary process — no unconditional rebinding |
| `test_a_stderr_without_a_descriptor_is_replaced_and_then_restored` | substituted when forced, and the library's own default is put back |
| `test_an_unusable_bound_default_and_no_descriptor_two_is_reported_not_hidden` | with nothing to substitute, the block yields `None` and changes nothing |
| `test_a_spawn_from_a_notebook_kernel_serves_the_connector` | end to end: with `sys.stderr` descriptor-less, a real server still starts and serves a call with provenance intact |

**The report is evidence, not decoration:**

| test | what it proves |
|---|---|
| `test_the_documents_fields_are_quoted_exactly` | every `DOC_*` constant in the lab module equals the document's text, character for character (parametrised over all four) |
| `test_the_pseudocode_is_quoted_with_its_own_comment` | including the trailing `# no agent touches a source directly` |
| `test_a_parity_verdict_names_how_much_was_compared_and_where_it_differs` | the verdict renders a magnitude and names the differing paths |
| `test_the_real_parity_rows_render_a_magnitude_not_just_a_verdict` | every delivered row compares ≥ 10 leaves — a single-digit count would mean the walk stopped at a top-level key |
| `test_a_populated_list_is_not_equal_to_an_absent_one` | the union is what counts, or a wire that dropped a list would look *identical* |
| `test_the_matrix_renders_as_a_table_rather_than_a_run_on_line` | every column width is measured from its data |
| `test_each_refused_role_cost_its_own_spawn` | spawn-scale, not call-scale — anchored on the warm calls rather than on `startup_ms`, because spawn time is not stable across a cold `.pyc` cache |
| `test_a_failed_check_makes_the_run_fail` | a synthetic failing check makes the run fail; the verdict is computed, not printed |

### Real bugs these tests and the notebook found

Twenty-nine in total (22-29 in this lab; the rest in earlier labs). The ones that would
have shipped:

* `tools/__init__.py` re-exported `get_seed_store`, `LocalSeedStore`, `SeedStoreError`
  — three names that made a bypass a one-liner.
* `config.py::get_or_create_connector_token` wrote a **generated token into `.env`**.
  Every run would have left a credential on disk that no one chose. Now ephemeral and
  in-memory only.
* `memory/stores.py` was a second front door: it built its allowed-provenance set from
  `LocalSeedStore.archive_sources()`. That set is what `MemoryStore.append()` enforces.
* `EndpointSpec.added_by_lab` defaulted to `"Lab 5"` everywhere and was being used as a
  proxy for `documented`, inverting the traceability.
* `list_courses` reported **no provenance**, and `LocalSeedAdapter.backing` returned
  `[]` for `kind="courses"`, so the provenance was empty even once it was asked for.
* A spawn from a notebook cell died with `io.UnsupportedOperation: fileno`.
* `_flatten` emitted `notes.[3].body`, so a difference inside a list was unnameable in
  the report it exists to make nameable.
* The access matrix padded its scope column to a hardcoded 9 characters while its
  longest value is 45, so the faculty and TA rows ran their scope list into the first
  record count.

### Proof captured

| file | what it is |
|---|---|
| `docs/evidence/lab05_pytest.txt` | 377 passed, 1 skipped, verbose — the whole project, not just Lab 5 |
| `docs/evidence/lab05_cli.txt` | full CLI transcript with the real MCP subprocess, exit 0 |
| `docs/evidence/lab05_cli_no_mcp.txt` | the fast path, 5 SKIP, exit 0 |
| `docs/evidence/lab05_notebook_output.txt` | all 54 notebook cells with outputs, 0 error outputs |

---

## f) Common errors, and the bugs found while building this

### Errors you will hit, and what they mean

**`ConnectorPolicyError: principal 'student@example.edu' (role 'student') may not call 'read_submission_records': it needs the scope 'submissions:read' and holds assignments:read, notes:read.`**
R2 working. If you did not expect it, you are calling with the wrong principal. Check
`ROLE_SCOPES[role]` against the endpoint's declared scope — and note the message names
the policy table so the fix is a lookup, not an investigation.

**`ConnectorPolicyError: principal 'anonymous' (role 'anonymous') may not call 'list_courses': it needs the scope 'notes:read' and holds none.`**
Also R2. `anonymous` is in `CONNECTOR_ROLES` so the matrix covers it, and it holds
nothing. A `list_courses` that only a signed-in caller can reach is intentional: the
course list is itself information.

**`ConnectorContractError: 'read_notes_store' returned a payload that does not match NotesResult — …`**
The *output* half of "typed". Either an adapter changed its result shape, or a
tampered/misbehaving server answered. The message names the endpoint, the model and
the offending field, because a contract failure with no field named is a
stack-trace hunt.

**`ConnectorContractError: unexpected argument 'current_only'`**
Deliberate. The tools layer accepts `current_only`; the connector does not, and passes
it nothing. The connector's surface is narrower on purpose — an agent cannot ask the
connector for something the connector has not agreed to expose.

**`SourceUnavailableError: no unit 'unit-99' in CS201; the course has: Unit 1, … `**
An unknown filter names the real values. "No notes for unit 9" is a claim a course
author might act on, so the refusal lists what does exist.

**`ConnectorTransportError: could not start the MCP connector server (…)`**
Three causes, in order of likelihood: the credential is not in the spawned server's
environment (check `MCP_CONNECTOR_TOKEN` and `MCP_CONNECTOR_ROLE`), the child is
spawned by a different interpreter than the client — which is why `server_command`
returns `sys.executable` rather than the bare string `"python"` — or the host has no
usable descriptor 2, in which case `Popen` raises `fileno` and this is the message it
arrives wrapped in.

**`io.UnsupportedOperation: fileno`, from a notebook cell**
Already fixed by `_fd_backed_stderr()`; if you hit it, you are on a path that does not
go through `McpConnectorClient._ensure_tools`, or the host has no descriptor 2 at all.
The second case is not fixable from here and is reported rather than swallowed.

**`NotImplementedError` from `async with MultiServerMCPClient(...)`**
`MultiServerMCPClient` forbids it (client.py:355). The pattern is
`async with client.session(name) as live: tools = await load_mcp_tools(live, ...)`,
which also happens to be the only way to keep the session — and so the subprocess —
alive across calls.

**`AttributeError: 'ConnectorContract' object has no attribute 'endpoints'`**
`ConnectorContract` exposes `names()`, `model_for()`, `validate()` and the `outputs`
dict. It has no endpoint list; use `registry.describe_all()` or
`registry.schemas()`.

**`ModuleNotFoundError: No module named 'mcp'`**
The connector is not optional at import time. `mcp==1.30.0`,
`langchain-mcp-adapters==0.3.2`, both in `requirements.txt`.

### The development bug log — Lab 5's nine

Found by writing the tests and by running the notebook, not by reading the code
afterwards. Each is now covered by a test.

| # | defect | why it mattered |
|---|---|---|
| 22 | `tools/__init__.py` re-exported `get_seed_store`, `LocalSeedStore`, `SeedStoreError` | Three names that made the document's pitfall a one-line affair. A re-export is an affordance |
| 23 | `config.py::get_or_create_connector_token` wrote a generated token into `.env` | Every run left a credential on disk that nobody chose, and a second run reused it |
| 24 | `memory/stores.py` resolved its allowed-provenance set from `LocalSeedStore.archive_sources()` | A second front door, and a worse one than an agent's: that set is the allow-list `MemoryStore.append()` enforces, so the layer could widen the check it exists to perform |
| 25 | `EndpointSpec.added_by_lab` defaulted to `"Lab 5"` and stood in for `documented` | Every endpoint reported itself as an addition, including the three the document names. A traceability matrix built on it inverts the requirement mapping |
| 26 | `list_courses` reported no provenance, and `LocalSeedAdapter.backing` returned `[]` for `kind="courses"` | Two causes stacked: the call omitted `adapter=`, and the adapter had no answer. Either alone would have produced an empty provenance |
| 27 | Test-side: `_python_files()` was not BOM-tolerant, and the lying-MCP-server fixture did not start from a real payload | An AST test that dies with `SyntaxError: U+FEFF` reports an encoding artefact as a structural violation; a fixture that fabricates a payload from scratch can pass for a server that would never return one |
| 28 | A spawn from an `ipykernel` cell raised `io.UnsupportedOperation: fileno` | The deliverable format is a notebook. `mcp`'s `errlog` default is bound at import from `sys.stderr`, which a kernel has replaced with a stream that has no descriptor |
| 29 | `_flatten` emitted `notes.[3].body` | A difference *inside a list* was unnameable in the report that exists to name differences. Now `notes[3].body` |
| 30 | The access matrix padded its scope column to 9 characters against a 45-character value | The faculty row's scope list ran straight into its first record count, so the table a reviewer reads to answer *"what does a student get?"* printed `submissions:read2` |

On 30, the fix is worth more than the bug. Every column width is now measured from
the data it holds, and the regression test asserts the **property** rather than a
snapshot — all lines end at the same offset — because the tempting next "fix" for a
mis-sized column is another hardcoded constant, which is how the original defect
arrived. I verified the test fails against the reintroduced defect before restoring
the fix:

```
E  AssertionError: ragged table, widths [131, 150, 168]
```

A note on a class of bug this lab produced five of (14, 24, 25, 28, 30): in every one,
something added defensively **disabled the check it appeared to support**. A hardcoded
column width, a re-exported name, a default `added_by_lab`, a patch on the wrong
object — none of them raise, and all of them make a test pass while the property is
gone. That is why each of these is held by a structural assertion (`ast`, offset
equality, `__defaults__ is None`) instead of by a value check.

On 28 specifically, the first attempt at the fix **silently did nothing**:
`stdio_client` is wrapped by `@asynccontextmanager`, and `functools.wraps` does not
copy `__defaults__`, so rebinding the wrapper's defaults changed nothing while looking
like it had worked. The unit test that catches it is
`test_the_errlog_default_lives_under_the_asynccontextmanager_wrapper`, which asserts
the wrapper's own defaults are `None`.

---

## g) Requirement coverage

| # | Doc requirement | Where met | Evidence | Status |
|---|---|---|---|---|
| R1 | *"Goal: Expose every data source through one typed connector."* | `EndpointRegistry`, `EndpointSpec.input_model`/`output_model`, `ConnectorContract.validate` | CLI §2 prints all four; `test_mcp_and_in_process_return_the_same_payload`, `test_a_datetime_survives_the_wire_as_a_datetime` | done |
| R2 | *"A connector server with typed endpoints and auth; agents call only through it."* | `mcp_server.py` (`FastMCP`), `ROLE_SCOPES`, `ConnectorLedger` | CLI §4: 4 tools advertised over stdio; §8: *"a real MCP subprocess served every call"* | done |
| R3 | *"Accept: All data access goes through the connector"* | AST audits over the package; `tools/readers.py` is the only seam | `test_no_module_outside_connector_imports_a_source_store`, `test_no_module_outside_connector_constructs_a_source_adapter`, `test_the_readers_module_does_not_reach_for_a_store`, `test_memory_names_its_allow_list_through_the_connector_not_the_store` | done |
| R4 | *"Accept: … swapping a source needs no agent change."* | `SWAP_AGENT_CODE`, `measure_source_swap()` | CLI §5 + *"swapping the source needed no agent change: same 4 tool(s), same query keys ('course_code',), same NotesReadResult"*; `test_the_swap_changed_the_data_and_not_the_agent`, `test_the_swap_agent_tuple_is_the_code_that_actually_runs` | done |
| R5 | *"Pitfall to avoid. No agent should touch a source directly; a bypass defeats governance later."* | `demonstrate_bypass()`, `BypassingReader` (test-only) | CLI §6: 8 records withheld, refusal on the ledger naming caller and scope; `test_the_bypass_class_does_not_escape_into_the_package` | done |
| R6 | `read('the notes/materials store')` | `read_notes_store`, scope `notes:read` | CLI §2: 8 records, `data/seed/data_structures/notes.json`; `test_each_document_line_maps_to_one_endpoint` | done |
| R7 | `read('assignment & rubric definitions')` | `read_assignment_rubrics`, scope `assignments:read` | CLI §2: 3 records, `data/seed/data_structures/assignments.json` | done |
| R8 | `read('submission records')` | `read_submission_records`, scope `submissions:read` | CLI §2: 8 records; §6: 8 withheld from the student | done |
| R9 | `agents.use(connector)   # no agent touches a source directly` | `readers.connector()` / `use_connector()` / `reset_connector()` | `test_the_escalation_seam_is_computed_from_names_not_from_a_string`; the client has no credential parameter | done |
| R10 | §2.3 *"Governance layer: access control per role, version history, audit log of grades published."* | `ROLE_SCOPES`; `ConnectorLedger` | CLI §3: 11 allowed / 5 refused of 16; §7: 23 calls, 6 refused, 0 failed, fingerprint `d0c917b9850376bd` | done (audit durability → Lab 6) |

**Deliberately deferred, and where each lands**

| limitation | why it is not in this lab | settled by |
|---|---|---|
| the audit log is **in memory** | `ConnectorLedger` is per-process. §2.12 wants a durable record that survives a crash | Lab 6 |
| the connector **does not grade** | reading a cohort's submissions is not marking them, and no endpoint decides a score. §2.3's *"grades published"* half is the grade boundary | Lab 6-7 |
| refusals cost **one spawn each** | the role arrives in the server's environment, so a second role needs a second process. Reported separately rather than hidden | — |
| the swap partner is **`in_memory`**, not Google | `SOURCE_ADAPTER=local` by default so every number above is re-checkable offline | — |
| no **cache, no retries** | every call re-reads the adapter. Correctness first; latency is a deployment problem | — |
| in-process and MCP are **two code paths** | parity between them is *measured* (11/159/74/85 leaves per endpoint), not assumed | — |
| `ROLE_SCOPES` is a **project choice** | the document says *"access control per role"* without naming roles. Stated in one table, asserted closed | — |

---

## h) Report and documentation text

The following is written for submission. It states what was built, what was measured,
and what was deliberately left out.

### Summary

Lab 5 implements the document's connector as a package that makes the boundary
**checkable** rather than merely intended. Every data source is reached through one
typed, authorised front door, and the claim *"agents call only through it"* is held by
an AST audit over the whole package: only `course_organizer.connector` may import the
filesystem module, construct a source adapter, or name a corpus path. That audit caught
four real bypasses during the build, the worst of them in Lab 4's own memory layer —
which resolved its allowed-provenance set directly from the seed store. That set is
what `MemoryStore.append()` enforces, so the layer could have widened the check it
exists to perform.

The four endpoints are typed in both directions. The registry validates a caller's
input against the declared query model; the client validates the server's *answer*
against the declared output model, which is where the word "typed" earns its keep
across a process boundary — a server that returned a convincingly-shaped wrong payload
is caught there and nowhere else.

Auth is the connector's, not the caller's, and that is a shape decision rather than a
policy one: `ConnectorClient.read(endpoint, payload)` has no credential parameter, and
over the wire the token and role travel in the *child process's environment*, never as
a tool argument. Sixteen cells (4 endpoints × 4 roles) are called rather than predicted
from the policy table, and the result is **11 allowed, 5 refused**. The student row is
the argument: a student may read 8 notes but not the 8 cohort submission records. A
blanket deny would satisfy *"students cannot read submissions"* while being broken, so
the tests assert the refusal *discriminates on scope*.

Everything above was then repeated over a **real MCP subprocess** — stdio, the MCP
protocol, `mcp.server.fastmcp` on the far side — because a policy that only holds
in-process is a policy that holds only in your own tests. Two findings are worth
recording because both were properties of the libraries rather than of this project.
`FastMCP` formats a tool failure as text, so the exception *type* does not survive the
wire; the connector therefore tags its errors with `[ClassName]` and the client decodes
the tag back into the class, then strips it so the exception a caller reads is
human-readable while its type still arrives. And `langchain-mcp-adapters` closes its
listing session and re-enters `create_session` on every call, so its default is a
respawn per call — `McpConnectorClient` holds one session open instead, and the
measured difference is **4 calls in 122.3 ms against a 4224.3 ms spawn**.

Payload parity between the two transports is compared **field path by field path**, not
as one boolean: 11, 159, 74 and 85 leaf fields respectively, with exactly one path
excluded (`provenance.fetched_at`, a wall-clock reading taken in a different process)
and the exclusion named in the output.

The pitfall — *"No agent should touch a source directly; a bypass defeats governance
later"* — is measured rather than staged. The lab module cannot import the store to
prove a bypass works, because that is the property under test, so the demonstration
class lives in the test module and a test forbids its name appearing anywhere in the
package. What the lab shows instead is what governance withholds: 8 submission records
for faculty, a typed refusal for the student naming the missing scope, and a ledger
row that names the caller. The data a bypass would have produced is real, and obtained
legitimately two lines earlier; the cost of skipping the check is not that the read
fails, it is that nothing records who asked.

### What was measured

The full run, with the real MCP subprocess, in **8228.0 ms**, 18/18 checks, exit 0.

| | value |
|---|---|
| endpoints | 4 (3 documented, 1 labelled as this project's) |
| access cells | 16 = 11 allowed, 5 refused; every refused cell 0.0 ms, `records=0` |
| in-process ledger | 23 calls, 6 refused, 0 failed, 92 records, 34.8 ms, fingerprint `d0c917b9850376bd` |
| Lab 2's tool layer, reading through the connector | 3 calls, 12.35 ms, 0 failed |
| CS201 corpus | 8 notes, 10 note versions, 5 units, 3 assignments, 9 rubric criteria, 8 submissions, 1 late, 0 scored |
| server spawn | 4224.3 ms |
| four warm calls | 122.3 ms total (77.9 / 13.9 / 14.5 / 16.1) |
| a per-call respawn would cost | ~16897 ms |
| refusal spawns | student 1936.4 ms, anonymous 1847.3 ms (one server per role, by design) |
| payload parity | 11 / 159 / 74 / 85 leaf fields identical; only `provenance.fetched_at` excluded |
| source swap | `local` → `'Data Structures'`, 8 notes, `data/seed/data_structures/notes.json`; `in_memory` → `'In-memory exchange course'`, 1 note, `in-memory://ZZ999/notes`; same tools, same query keys, same result type |
| `--no-mcp` path | 18 checks, 5 SKIP, 52.7 ms — and it says it is not evidence for them |
| project suite | 377 passed, 1 skipped, 40.65 s |

Every figure above is quoted from `docs/evidence/lab05_cli.txt` (and
`lab05_cli_no_mcp.txt`), both of which exit 0. The corpus row was measured by reading
`data/seed/data_structures/` directly, so it can be checked without running anything.

One number is worth isolating, because it is the one a reviewer should look at twice:
**a student is refused the cohort's submissions while being allowed 8 notes.** A
connector that refused everything to a student would pass a weaker test and be useless.

### Honest limitations

* **The audit log does not survive a crash.** `ConnectorLedger` is per-process.
  §2.3's *"audit log of grades published"* and §2.12's durable-record requirement are
  Lab 6's work; this is the read-side half, correctly shaped for it to extend.
* **Nothing here grades.** Reading submissions is not marking them, and no endpoint
  decides a score. The grade boundary is Lab 6-7, and §2.12's *"never publish without
  faculty approval"* is untouched by this lab.
* **The live Google adapter is built and mock-tested, not run against a real
  account.** `SOURCE_ADAPTER=local` is the default, so every number above is
  re-checkable with no credentials and no network. `requires_source_credentials=True`
  is reported on the descriptor, so the choice is visible.
* **Two refusals cost two process spawns.** That is the price of putting the role in
  the environment rather than on the wire, and it is the right trade for a lab whose
  pitfall is escalation. It is reported separately rather than folded into the warm-call
  figures, where it would read as a slow endpoint.
* **The connector does not cache or retry.** Every call re-reads the adapter.
  Correctness first; latency is a deployment concern.
* **There are two transport implementations**, in-process and MCP, and parity between
  them is measured rather than assumed — 329 leaf fields compared in total. A future
  edit to one that broke parity would fail
  `test_the_wire_and_the_direct_client_agree_on_every_compared_leaf`.
* **29 product defects were found and fixed while building the project, eight in this
  lab.** Two are worth naming: `get_or_create_connector_token` wrote a generated
  credential into `.env`, and the Jupyter spawn failure hid behind a patch that
  appeared to work while rebinding a function's `__defaults__` on the wrong object.
  Both would have passed review; both are listed in section f.

### Reproducing this

```powershell
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"

# the full lab, real MCP subprocess, exit 0 means all 18 checks passed
& $py -m course_organizer.labs.lab05_mcp_connector

# the fast path; transport checks report SKIP and say they were not run
& $py -m course_organizer.labs.lab05_mcp_connector --no-mcp

# what the server advertises, on its own
& $py -m course_organizer.connector.mcp_server --describe

# tests
& $py -m pytest tests\test_lab05_mcp_connector.py -o addopts= -v
& $py -m pytest tests\test_lab05_lab_module.py     -o addopts= -v
& $py -m pytest

# the notebook, with real outputs already in the file
& $py -m jupyter lab notebooks\lab05_mcp_connector.ipynb
```