# Agentic Course Organizer

**Worklet 2 — Course Project, Notes & Assignment Organizer**
Agentic AI build book, Chapter 2, Labs 1–8.

One coordinator plus specialist worker agents, where **agents draft and flag, and a
named human approves**. Every grade, every similarity flag and every published
version is gated by course faculty. That is not decoration — it is the
architecture, and each lab adds one piece of it.

---

## Status

| Lab | Phase | Topic | State |
|-----|-------|-------|-------|
| **1** | A · Foundations | Agent vs Chatbot — the organiser planner loop | **Complete, 25/25 tests pass** |
| **2** | A · Foundations | Typed tools — read, compute, verify | **Complete, 59/59 tests pass** |
| **3** | A · Foundations | Skills & the locked plan | **Complete, 121/121 tests pass** |
| **4** | A · Foundations | Memory & retrieval — state vs stored | **Complete, 62/62 tests pass (1 skipped)** |
| **5** | A · Foundations | Governed MCP connector — one typed front door | **Complete, 111/111 tests pass; lab exits 0 on 18/18 checks** |
| **6** | A · Foundations | Governed runtime — budgets, gates, checkpoint/resume, audit | **Complete, 170/170 tests pass; lab exits 0 on 16/16 checks** |
| **7** | B · Orchestration | Agentic node graph — the full pipeline, one gate | **Complete, 133 test cases pass; lab exits 0 on 18/18 checks** |
| **8** | B · Orchestration | Parallel swarm + comparison | **Complete, 104/104 tests pass; lab exits 0 on 27/27 checks** |
| **9** | C · Applied reliability | Grounding and evidence — typed evidence tables, 5th validation check | **Complete, 24/24 tests pass; Lab 7 CLI 18/18, Lab 8 CLI 27/27, exit 0** |
| **10** | C · Applied reliability | Self-healing validation — typed findings, targeted repair, budgeted escalation | **Complete, 16/16 tests pass; Lab 7 CLI 18/18 exit 0** |
| **11** | C · Applied reliability | Tested build factory — typed verdicts, plan identity, hard gate; authorized plan governs execution scope | **Complete, 23/23 tests pass; Lab 7 CLI 18/18 exit 0** |

**858 tests pass, 1 skipped** across Labs 1–11 (no network, no API key). See
[`docs/lab01.md`](docs/lab01.md) … [`docs/lab11.md`](docs/lab11.md) for the full
write-ups, and [`docs/evidence/`](docs/evidence/) for captured proof.

---

## Zero-setup by design

The project runs **end to end with no API key, no cloud account and no paid tier.**

| Concern | Default | Why |
|---------|---------|-----|
| LLM | `LLM_BACKEND=scripted` | Deterministic, offline, free, no rate limit. Tests must be re-runnable by a reviewer without credentials. |
| Data source | `SOURCE_ADAPTER=local` | Reads `data/seed`. No Google Cloud project needed. |
| Embeddings | `EMBEDDING_BACKEND=hashing`, CPU-only | 256-dim blake2b character n-grams. No download, no network, fully deterministic — so a reviewer re-checks every acceptance criterion offline. `fastembed` (`BAAI/bge-small-en-v1.5`, 384-dim, ~69 MB, **no PyTorch**) is one flag away and scores far better |

To use real Gemini later, add one line to `.env` and flip one variable:

```bash
GOOGLE_API_KEY=...        # free key from https://aistudio.google.com
LLM_BACKEND=live
```

No agent code changes. `ScriptedLLM` and `GeminiLLM` implement the same
`LLMClient` protocol.

---

## Setup

> **Windows note:** create the virtualenv **outside** OneDrive/Google Drive.
> A venv inside a synced folder fails `pip install` with
> `[WinError 32] The process cannot access the file because it is being used
> by another process`, because the sync client holds locks on the files.

```powershell
# 1. venv OUTSIDE any synced folder
python -m venv $HOME\.virtualenvs\course-organizer
& "$HOME\.virtualenvs\course-organizer\Scripts\python.exe" -m pip install --upgrade pip

# 2. dependencies (~450 MB, no PyTorch)
& "$HOME\.virtualenvs\course-organizer\Scripts\python.exe" -m pip install -r requirements.txt

# 3. install the package in editable mode
& "$HOME\.virtualenvs\course-organizer\Scripts\python.exe" -m pip install -e .

# 4. config (every key has a safe default - this step is optional)
Copy-Item .env.example .env
```

macOS / Linux:

```bash
python3 -m venv ~/.virtualenvs/course-organizer
source ~/.virtualenvs/course-organizer/bin/activate
pip install --upgrade pip
pip install -r requirements.txt && pip install -e .
cp .env.example .env
```

Verified on **Windows 11 · Python 3.14.7 · win_amd64**.

---

## Role-Based Web Portal (Labs 1–8 + Identity Layer)

The project now includes a **complete role-based web portal** wrapping the Labs 1–8 backend:

| Role | Capabilities |
|------|--------------|
| **Student** | View enrolled courses, submit assignments (write-once), download notes, see **only APPROVED grades**, track progress graphs |
| **Faculty** | Create assignments, upload notes (versioned), view submission dashboard, enter **draft grades** with similarity flags, **approve & publish** grades |
| **HoD** | Read-only overview: submission counts, late rates, similarity flags, **approve any draft grade**, audit log viewer |

### 1. Seed Demo Data (run once, idempotent)

```powershell
# Seed 1 HoD, 2 Faculty, 6 Students across 2 courses
python -m scripts.seed_demo
```

This creates the database and these accounts:

| Role | Email | Password |
|------|-------|----------|
| HoD | `hod@university.edu` | `hod123` |
| Faculty 1 (CS201) | `prof.alex@university.edu` | `faculty123` |
| Faculty 2 (CS202) | `dr.ravi@university.edu` | `faculty123` |
| Student 1 (CS201) | `alice@student.edu` | `student123` |
| Student 2 (CS201) | `bob@student.edu` | `student123` |
| Student 3 (CS201) | `carol@student.edu` | `student123` |
| Student 4 (CS202) | `david@student.edu` | `student123` |
| Student 5 (CS202) | `eve@student.edu` | `student123` |
| Student 6 (CS202) | `frank@student.edu` | `student123` |

> Seeding is **explicit and idempotent** — it never runs automatically on app start or login. Run it whenever you want a fresh demo database.

### 2. Run the API + Web Portal (Two Terminals)

**Terminal 1 — API Server:**
```powershell
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```
API docs at `http://127.0.0.1:8000/docs`

**Terminal 2 — Streamlit UI:**
```powershell
cd ui_roles
python -m streamlit run Home.py --server.port 8501
```
UI at `http://localhost:8501` — navigate via sidebar: **Faculty Dashboard**, **Student Dashboard**, **HoD Dashboard**

### 3. Quick Flow Test

1. **Faculty** logs in → **Faculty Dashboard** → **Assignments** → Create assignment for CS201
2. **Student** (CS201) logs in → **Student Dashboard** → **Assignments** → Submit file
3. **Faculty** → **Submissions** → Auto-grade or manual grade → **Approve**
4. **Student** refreshes → Sees **approved grade** in dashboard + progress graph
5. **HoD** → **HoD Dashboard** → Sees overview, can approve any pending draft grade

### 4. Run Isolation Tests (proves all requirements)

```powershell
python scripts/test_isolation.py
```

Verifies: student isolation, faculty isolation, no draft grades to students, HoD read-only.

### 5. Full Test Suite (785 tests)

```powershell
python -m pytest -o addopts= -q
```

See [`docs/traceability.md`](docs/traceability.md) for the complete requirement → code → test mapping.

### 6. Traceability Matrix

See [`docs/traceability.md`](docs/traceability.md) for requirement → implementation → test mapping.

### 7. Realtime Enrollment, Google Login & Classroom Import

- **Course catalog & self-enrollment:** students browse `Course Catalog` page and register for any number of courses; faculty create courses freely (page + `POST /courses/`).
- **Submit → faculty instantly:** submit needs enrollment (clear 403 otherwise); every submit runs the background pipeline (Lab 8 swarm + Lab 2 auto-grade draft), so the grading queue populates in realtime; faculty approves to publish.
- **Sign in with Google:** dashboards have a Google block (consent → paste code → JWT). Needs `credentials.json` + redirect URI `http://127.0.0.1:8000/google/callback` registered in Google Cloud Console (Classroom API enabled for import).
- **Classroom import:** faculty `Classroom Import` page — connect, pick a Classroom course, import coursework as assignments and materials as notes (provenance in audit log).
- **Fresh DB after schema change:** `python -m scripts.reset_db` (deletes `course_organizer.db`, recreates, reseeds). Required once after pulling this update.
- **Login:** use the **Login** page, pick Student / Faculty / HoD / Admin — you land on that role's dashboard. Auth button (identity + Logout) is top-right on every page. No demo accounts exist; sign up or use Google.
- **Gmail code by email:** set `SMTP_USER` + `SMTP_APP_PASSWORD` (Gmail App Password) in `.env`; Google-login codes are then emailed, otherwise shown on-screen.
- **Optional AI Course Assistant (OpenClaw):** off by default. Set `OPENCLAW_ENABLED=true` + `OPENCLAW_GATEWAY_URL` + `OPENCLAW_GATEWAY_TOKEN`, restart the API, then open **Faculty → AI Course Assistant**. Only allowlisted read tools are exposed, every call is audited, and grade/publish requests need explicit human approval. Full setup and troubleshooting: [`docs/openclaw_integration.md`](docs/openclaw_integration.md).

---

### Why `mcp` is pinned to 1.x

```
langchain-mcp-adapters==0.3.2  requires  mcp<2.0.0,>=1.24.0
mcp (latest)                                        2.2.0
```

Installing `mcp==2.2.0` makes pip raise `ResolutionImpossible`. Pin `mcp==1.30.0`.

---

## Run a lab

```powershell
# Lab 1 - the planner loop
python -m course_organizer.labs.lab01_planner_loop
python -m course_organizer.labs.lab01_planner_loop --interactive
python -m course_organizer.labs.lab01_planner_loop `
  --answer course_code=DBMS402 `
  --answer "units=units 2-4" `
  --revise "also cover unit 5"

# Lab 2 - the tool-using agent (self-checking: exit code 0 means the lab passed)
python -m course_organizer.labs.lab02_tool_using_agent

# Lab 3 - skill creation (self-checking: exit code 0 means the lab passed)
python -m course_organizer.labs.lab03_skill_creation
python -m course_organizer.labs.lab03_skill_creation --case cs-narrow --template faculty-pack

# Lab 4 - memory & retrieval (self-checking: exit code 0 means the lab passed)
python -m course_organizer.labs.lab04_memory_retrieval
python -m course_organizer.labs.lab04_memory_retrieval --embedding-backend fastembed
python -m course_organizer.labs.lab04_memory_retrieval --run-id my-run-id

# Lab 5 - the governed MCP connector (self-checking: exit code 0 means 18/18 passed)
python -m course_organizer.labs.lab05_mcp_connector
python -m course_organizer.labs.lab05_mcp_connector --no-mcp      # fast; transport checks report SKIP
python -m course_organizer.labs.lab05_mcp_connector --course CS201 --source local
python -m course_organizer.connector.mcp_server --describe        # what the server advertises

# Lab 6 - the governed runtime (self-checking: exit code 0 means 16/16 passed)
python -m course_organizer.labs.lab06_governed_runtime
python -m course_organizer.labs.lab06_governed_runtime .\lab06-evidence  # keep the jsonl/sqlite

# Lab 7 - the node graph (self-checking: exit code 0 means 18/18 passed)
python -m course_organizer.labs.lab07_node_graph
python -m course_organizer.labs.lab07_node_graph --keep        # keep the runs and the sqlite
python -m course_organizer.labs.lab07_node_graph --json        # write lab07_checks.json

# Lab 8 - the parallel swarm (self-checking: exit code 0 means 27/27 checks)
python -m course_organizer.labs.lab08_parallel_swarm

# E2E (auto-approve at gate)
python scripts/run_full_e2e.py
python scripts/run_swarm_e2e.py

# Lab 14 capstone demo - holds at the real faculty gate; approve/refuse explicitly
python -m scripts.run_lab14_demo --domain lab
python -m scripts.run_lab14_demo --resume demo-lab-<id> --decision approve
```

See [docs/lab14_phase4_demo.md](docs/lab14_phase4_demo.md) for the full
end-to-end walkthrough (CLI, API, and faculty dashboard).

```powershell
# notebooks (the deliverable format)
jupyter lab notebooks/lab01_planner_loop.ipynb
jupyter lab notebooks/lab02_tool_using_agent.ipynb
jupyter lab notebooks/lab03_skill_creation.ipynb
jupyter lab notebooks/lab04_memory_retrieval.ipynb
jupyter lab notebooks/lab05_mcp_connector.ipynb
jupyter lab notebooks/lab06_governed_runtime.ipynb
jupyter lab notebooks/lab07_node_graph.ipynb

# tests
python -m pytest

# regenerate the notebook evidence files
python tools/nb_to_text.py notebooks/lab02_tool_using_agent.ipynb docs/evidence/lab02_notebook_output.txt
python tools/nb_to_text.py notebooks/lab03_skill_creation.ipynb docs/evidence/lab03_notebook_output.txt
python tools/nb_to_text.py notebooks/lab04_memory_retrieval.ipynb docs/evidence/lab04_notebook_output.txt
python tools/nb_to_text.py notebooks/lab05_mcp_connector.ipynb docs/evidence/lab05_notebook_output.txt
python tools/nb_to_text.py notebooks/lab06_governed_runtime.ipynb docs/evidence/lab06_notebook_output.txt
python tools/nb_to_text.py notebooks/lab07_node_graph.ipynb docs/evidence/lab07_notebook_output.txt
```

> If `pytest` prints no summary line, `pyproject.toml`'s `addopts = "-q"` met
> an extra `-q` you passed and the pair became `-qq`. Run bare `python -m pytest`,
> or `-o addopts= -v` for the full test list.

---

## Layout

```
agentic-course-organizer/
├── requirements.txt              # every pin install-tested on Py 3.14.7 / Windows
├── .env.example                  # copy to .env; every key has a default
├── pyproject.toml
├── data/seed/                    # deterministic corpus: 2 courses, 14 notes, 17 versions, 6 assignments
├── src/course_organizer/
│   ├── config.py                 # pydantic-settings; API keys never hardcoded
│   ├── logging_setup.py          # run_id-stamped logging + JSON run log
│   ├── schemas.py                # §2.6 Data Model, transcribed exactly
│   ├── llm.py                    # LLMClient protocol: Scripted | Gemini
│   ├── labs/
│   │   ├── lab01_planner_loop.py # clarify -> propose_plan -> revise
│   │   ├── lab02_tool_using_agent.py  # plan -> execute -> verify claims
│   │   ├── lab03_skill_creation.py    # the two skills, four cases, drift demo
│   │   ├── lab04_memory_retrieval.py  # four cases, both backends, pitfall demo
│   │   ├── lab05_mcp_connector.py     # 4 endpoints x 4 roles, a real MCP subprocess, 18 checks
│   │   ├── lab06_governed_runtime.py  # 5 steps, 2-process crash/resume, 16 checks
│   │   └── lab07_node_graph.py        # the graph end to end, 4 scenarios, 18 checks
│   ├── tools/                    # §2.10 Lab 2: the typed tool layer
│   │   ├── base.py               # ToolSpec, ToolRegistry, ToolLedger, ToolResult
│   │   ├── seed_store.py         # LocalSeedStore - the ONLY filesystem access
│   │   ├── readers.py            # connector() seam: read_notes_store, read_assignment_rubrics
│   │   └── compute.py            # compute_scope_validation, build_default_registry
│   ├── skills/                   # §2.10 Lab 3: the typed skill layer
│   │   ├── base.py               # SkillSpec, SkillRegistry, SkillLedger, SkillResult
│   │   ├── plan_skill.py         # expand(inputs).lock() -> LockedPlan + DriftReport
│   │   └── format_skill.py       # format_organised(result, template) -> Document
│   ├── memory/                   # §2.10 Lab 4: the two-layer memory + retrieval
│   │   ├── base.py               # MemoryStore (both invariants), MemoryRegistry, ledger
│   │   ├── stores.py             # RunState, ArchiveStore - the only MemoryRecord builder
│   │   ├── embeddings.py         # Embedder protocol, hashing | fastembed, cosine
│   │   └── retrieval.py          # TagQuery, SimilarityQuery, SimilarityFlag, raise_flag
│   └── connector/                # §2.10 Lab 5: the one governed, typed front door
│       ├── port.py               # SourceAdapter protocol, SourceDescriptor - the swappable half
│       ├── errors.py             # the error hierarchy + tag_for_wire / untag_from_wire
│       ├── auth.py               # ROLE_SCOPES, PrincipalRegistry, ensure_token (ephemeral)
│       ├── core.py               # EndpointRegistry, EndpointSpec, ConnectorLedger, ConnectorCall
│       ├── endpoints.py          # the document's 3 reads + list_courses, provenance on every result
│       ├── adapters.py           # LocalSeedAdapter | InMemoryAdapter | GoogleWorkspaceAdapter
│       ├── client.py             # InProcess | McpConnectorClient, ConnectorContract, server_command
│       └── mcp_server.py         # the real stdio MCP server (FastMCP)
│   ├── runtime/                  # §2.10 Lab 6: the governed runtime the later labs plug into
│   │   ├── errors.py             # 6 error types; budget vs gate vs integrity
│   │   ├── budget.py             # Budget, BudgetLedger, BudgetSnapshot, DEFAULT_LIMITS
│   │   ├── audit.py              # AuditRecord, JsonlAuditLog - hash-chained, inputs AND outputs
│   │   ├── checkpoint.py         # RunState, SqliteCheckpointStore - ids sort in write order
│   │   ├── gates.py              # ApprovalRequest/Decision + 5 gate implementations
│   │   └── runtime.py            # Step, RunReport, Runtime - the one loop
│   └── graph/                    # §2.10 Lab 7: the explicit node graph + the real gate
│       ├── state.py              # CourseState (42 keys), the models, doc 2.7's checks
│       ├── validation.py         # the four checks, explain_failure, next_route
│       ├── specialists.py        # the six Phase-A agents, the trigram similarity measure
│       ├── context.py            # RunContext + the typed query factories
│       ├── nodes.py              # the eight nodes, including interrupt() at the gate
│       ├── escalation.py         # the diagnosis, with the failing draft attached
│       ├── builder.py            # the topology, the routes, the recursion backstop
│       └── runner.py             # run_pipeline, resume_run, the sqlite checkpointer
├── notebooks/
│   ├── lab01_planner_loop.ipynb
│   ├── lab02_tool_using_agent.ipynb
│   ├── lab03_skill_creation.ipynb
│   ├── lab04_memory_retrieval.ipynb
│   ├── lab05_mcp_connector.ipynb
│   ├── lab06_governed_runtime.ipynb
│   └── lab07_node_graph.ipynb
├── tools/nb_to_text.py           # notebook -> plain-text evidence
├── tests/
└── docs/
    ├── lab01.md                  # Lab 1 write-up, sections a-h
    ├── lab02.md                  # Lab 2 write-up, sections a-h
    ├── lab03.md                  # Lab 3 write-up, sections a-h
    ├── lab04.md                  # Lab 4 write-up, sections a-h
    ├── lab05.md                  # Lab 5 write-up, sections a-h
    ├── lab06.md                  # Lab 6 write-up, sections a-h
    ├── lab07.md                  # Lab 7 write-up, sections a-h
    └── evidence/                 # captured proof (test + notebook output)
```

### The Lab 2 tool boundary

```
agent  ->  ToolRegistry  ->  ToolSpec.handler  ->  LocalSeedStore
            (ledger)         (typed in / out)     (the only disk access)
```

Lab 5 replaces `LocalSeedStore` with a Google Drive / Classroom adapter and
leaves every other module untouched. That containment is also what makes Lab 2's
acceptance criterion *enforceable*: because all filesystem access lives in one
file, a test can assert with `ast` that the agent module has none.

### The Lab 3 skill boundary

```
agent  ->  SkillRegistry  ->  SkillSpec.handler  ->  LockedPlan | Document
            (ledger)         (typed in / out,        (pure functions:
                             case_agnostic)            no IO, no clock,
                                                        no LLM, no network)
```

`skills/` imports **no** lab module and touches **no** filesystem — both
enforced by AST scans, not by review. A skill transforms; data access belongs to
the tool layer, which has a connector boundary and a ledger entry behind it.

The same scans assert that `skills/` contains **no course identifier at all**,
in any file, including docstrings and comments. That scan found three real
violations during the build — all illustrative examples in docstrings, which is
exactly where a "temporary" constant survives a deadline.

### The Lab 4 memory boundary

```
agent  ->  MemoryRegistry  ->  RetrieverSpec  ->  MemoryStore
             (ledger)         (typed in / out)   (two independent checks)
                                                 |
                    RunState  <-- draft           |  append() refuses RUN_STATE
                    ARCHIVE  <-- notes + history  |  append() refuses forged paths
```

Both invariants live in **one** method, `MemoryStore._check_appendable()`, on
purpose: one place to look, therefore one place to bypass. That is also what lets
`ContaminatedArchive` demonstrate the document's pitfall honestly — it overrides
exactly that method, so the corruption it causes is the real one, not a staged
approximation.

`memory/` loads its corpus **through the tool layer**, so the only filesystem
boundary is still the single module Lab 2 established. Lab 5 then swapped
`LocalSeedStore` for a `SourceAdapter` and `memory/` did not change — which is the
forward claim above, kept honest.

Lab 5 did find one thing here. `stores.py` had been resolving its
allowed-provenance set from `LocalSeedStore.archive_sources()` directly. That set is
what `MemoryStore.append()` enforces, so the layer could have named its own
allow-list. It now goes through the connector's provenance report instead.

### The Lab 5 connector boundary

```
agent  ->  ConnectorClient  ->  MCP transport  ->  EndpointRegistry  ->  SourceAdapter
            (no credential   (optional; real    (auth + scope check,   (the swappable
             parameter)       stdio subprocess)  typed in/out, ledger)   half)
                                                                |
                                                   LocalSeedStore / InMemory / Google
```

Three things about this shape are load-bearing, and each is enforced by a test
rather than by review:

* **The credential is held, not passed.** `ConnectorClient.read(endpoint, payload)`
  has no token argument — over the wire the token and role travel in the *child
  process's environment* (`MCP_CONNECTOR_TOKEN`), and `server_command()` has no
  `--token` flag, so the command line cannot leak it.
* **The exception type survives MCP.** `FastMCP` formats tool errors as text, so the
  class is gone by the time the client sees it. The connector tags its own errors
  `[ClassName]` and the client decodes the tag, then strips it — so a `refusal`
  and a `crash` do not both arrive as `RuntimeError`.
* **Only `connector/` may name a source.** Four AST scans over the whole package:
  no import of `seed_store`, no constructed adapter, no corpus path outside
  `connector/`, and no `data/seed` in a non-docstring literal. Two real bypasses
  were caught this way, including the one in `memory/stores.py` above.

Measured: 4 endpoints × 4 roles = 16 cells, **11 allowed / 5 refused**. A student
gets 8 notes and is refused the cohort's 8 submissions with the missing scope named,
and the refusal is on the ledger. Over a real stdio MCP subprocess the payloads
matched the in-process client on 329 leaf fields, excluding only
`provenance.fetched_at`.

### The Lab 6 runtime boundary

```
plan  ->  Runtime.run(plan)  ->  Runtime.execute_step(step)   <- the only place a step body is called
                                    1. reserve the budget      (refuse BEFORE the action)
                                    2. check the wall clock    (an overrun already spent)
                                 3. checkpoint               <- pitfall: BEFORE a risky step
                                 4. ask the gate
                                 5. run the step
                                 6. charge what it cost
                                 7. audit.log(inputs, outputs)  <- pitfall: inputs AND outputs
                                 8. checkpoint
                                    |
                    JsonlAuditLog (hash-chained)   SqliteCheckpointStore
                    every action, both halves     on langgraph's own table
```

Four things about this shape are load-bearing, and each is held by a test rather
than by review:

* **The document's own pitfall is the specification.** The pseudocode on line 682
  and the pitfall beneath it disagree twice — the pseudocode logs before running
  and checkpoints after the approval. `DOC_PSEUDOCODE` and `RUNTIME_ORDER` are
  both printed, and a test asserts the two differ in exactly the two places the
  pitfall names, so the runtime cannot drift back to the order the document got
  wrong.
* **`inputs` and `outputs` are required schema fields.** `AuditRecord` cannot be
  constructed without both, and its digest is derived by `build_audit_record()` —
  so a message-shaped record is not a representable state, rather than a
  convention nobody enforces.
* **Checkpoint ids must sort.** LangGraph's `get_tuple` is
  `ORDER BY checkpoint_id DESC` — a **string** compare. Ids are
  `f"{write_seq:012d}-{digest[:12]}"` with the ordinal allocated from the SQLite
  table, because "the latest checkpoint" is otherwise arbitrary.
* **Two refusals, handled differently.** A *gate* refusal is on the audit log with
  the approver's identity and the approval request that was presented. A *budget*
  refusal is **not**, because nothing ran — it is in `RunReport.diagnosis`, in
  `BudgetLedger.refused`, and in the structured log. Logging a refusal as though
  something happened would make §2.12's *"every grade published"* unanswerable.

Measured: **16/16 acceptance checks**, 170 tests. The crash half is two real OS
processes — the first calls `os._exit(9)` inside the last step, the second
resumes — and the resumed process runs exactly one step, `publish` does not
execute twice, the gate is not re-asked, and all **5** audit records verify as one
chain across the process boundary.

### The Lab 7 node-graph boundary

```
ingest -> plan -> workers -> validate --fail--> workers   (bounded: attempts < MAX_ATTEMPTS)
                              |                                |
                              | pass                           | exhausted
                              v                                v
                           assemble -> gate --approve/edit/override--> publish
                                        \
                                         --refuse--------------> escalate -> END
                                                                  (diagnosis + the failing draft)
```

Five things about this shape are load-bearing, and each is held by a test rather
than by review:

* **The back-edge is two edges, not one.** `validate --fail--> workers` only
  cycles because `workers -> validate` is *also* present. Both directions are
  asserted off the **compiled** graph, not off the declared constants — the
  constants would still agree with each other if the wiring were wrong.
* **The bound is a counter, not the recursion limit.** `RECURSION_BACKSTOP = 32`
  exists so a miswired graph cannot run forever, but it raises
  `GraphRecursionError` — a crash, not an outcome. The bound that does the work is
  the explicit `attempts` counter, which routes to `escalate` with a written
  diagnosis and the failing draft attached. The diff of an exhausted run and a
  converged one is measured, not asserted.
* **The gate is a real `interrupt()`.** `invoke()` returns with the payload under
  `__interrupt__`; `Command(resume=…)` carries the decision back in. On resume the
  gate node re-enters from the top and `interrupt()` returns immediately, so the
  pre-gate nodes **do not re-run** — verified across a process boundary by the
  same two-interpreter technique Lab 6 uses.
* **Similarity is measured, then flagged — never adjudicated.** The first draft of
  this lab hashed the submission id to produce a score, while every real
  `similarity_score` in the corpus is `None`. That was fabrication dressed as
  evidence, and it was replaced by a real lexical measure: word-trigram shingles
  after stopword removal, Jaccard overlap against peers in the same assignment,
  flag at `0.30`. The measure is honest about what it proves: `DS-A1-S001` and
  `DS-A1-S003` are a genuine near-duplicate (identical shingle sets, overlap
  **1.000**), and every other pair is at or below 0.023.
* **A published document must not contradict itself.** Lab 3's formatter is
  hard-wired to a draft and stamps `Approved : no` / `Published: no`. The `publish`
  node stamps the pre-gate lines rather than editing Lab 3's tested module, and a
  test opens the artifact and asserts it names the approver. The published file is
  4,695 ASCII characters containing `## Approval Recorded`.

Measured: **18/18 acceptance checks**, 133 test cases. One command —
`python -m course_organizer.labs.lab07_node_graph` — runs four scenarios (clean,
retry, exhaustion, refusal) and reaches the gate with a validated draft. Nothing
in the transcript is typed by hand, so the checks and the printed run cannot
disagree.

---

## The document's rules, encoded

From Chapter 2. These are not conventions; they are in the code and in the tests.

**Guardrails (§2.12) — non-negotiable**

| Rule | How it is enforced |
|------|--------------------|
| Never publish a grade or feedback without faculty approval | `DraftGrade.status` starts `draft`; the plan's human gate is a hard stop that precedes publish. **Lab 7** makes it executable: a real LangGraph `interrupt()` gate, and the `publish` node re-checks the approval locally so the invariant does not depend on the graph being wired correctly |
| Never make a final plagiarism determination automatically | `SubmissionRecord` has `similarity_score` (float) and deliberately **no** `is_plagiaristic: bool`. `SimilarityFlag` goes further: the 19 names in `PLAGIARISM_FORBIDDEN_FIELDS` are asserted **absent from its 14 fields**, and `raise_flag` refuses an archive-layer candidate outright. **Lab 7** measures the score and only ever sets `similarity_flagged`; no mark is changed by a flag |
| Never alter a submission timestamp | `submitted_at` must be timezone-aware, validated at construction, derived lateness only. **Lab 7**'s late check reads the store's timestamp and copies it, and its remedy for a mismatch is *"re-read the submission timestamps from the store"* |
| Keep clear version history | `NoteMaterial.versions` must be contiguous from 1 and reject duplicates |
| Keep a full audit log of every grade published and by whom approved | `run_id` on every log record; **Lab 6** makes it durable and hash-chained — `JsonlAuditLog`, one record per action with `inputs` **and** `outputs` required, `decided_by` on every gated record, and a forged predecessor refused *at append*. **Lab 7** appends a node-level `audit_trail`, stamps `approved_by`/`approved_at` on every published grade, and names the approver in the published document |

**Validation checks (§2.7) — Lab 7 implements all four; Lab 8 extends them**

0. A draft grade never exceeds the rubric maximum — `draft_grade_within_rubric_maximum`
1. Late submissions are flagged accurately — `late_submissions_flagged_accurately`, checked against the store's own timestamp, never against the flag itself
2. A plagiarism/similarity score is attached — `similarity_score_attached`; an absent score fails, because an absent score is not a zero score
3. No grade is published without faculty approval — `check_no_grade_published_without_faculty_approval`, plus a second guard inside `publish`

The rubric-maximum check uses `RUBRIC_TOLERANCE = 0.0` because §2.7 says *"never"*: a
grade half a mark over the maximum fails, and the diagnosis names the submission,
the awarded mark and the maximum.

**Loops are bounded.** The doc warns that an unbounded regenerate loop "can spin
forever". The revise loop is capped at `MAX_REVISIONS` and, when it hits the cap,
it **escalates to a human via `open_questions`** rather than hiding the fact.
**Lab 7** applies the same rule to the node graph: an explicit `attempts` counter
caps the `validate --fail--> workers` back-edge at `MAX_ATTEMPTS`, and on
exhaustion an `escalate` node carries the diagnosis and the failing draft to
`course_faculty`. The recursion limit is a backstop, not the bound.

**Tool discipline (§2.10 Lab 2) — the pitfall, encoded**

> *Never assert a fact in the prompt that a tool should verify.*

A prompt is untrusted input, so a fact in one is an **assertion to be tested**,
never a value to be reported. Three verdicts are possible, and the third is the
point:

| Verdict | When | Example |
|---|---|---|
| `CONFIRMED_BY_TOOL` | the tool agrees | "CS201 has 5 units" |
| `CONTRADICTED_BY_TOOL` | the tool disagrees — and the tool's value wins | "CS201 has 9 units" |
| `NOT_VERIFIABLE_WITH_CURRENT_TOOLS` | no tool computes it, so the agent declines | "the notes are clearly the best" |

The `LAB 2` demo ships a request with three false claims, three **true** claims,
and one uncomputable claim. The true ones are the control: a checker that refuted
everything would pass a test containing only false claims, so what is actually
being demonstrated is discrimination.

**Scope is part of a fact.** A read narrowed to `ASG-01` reports
`total_available_marks` of 20, not 65 — correct for the subset, wrong for the
course. `ScopeValidationResult` records its own scope and every claim records the
scope that settled it, so a course-wide claim is never checked against a
narrowed read and a figure cannot later be misread.

**The model proposes; the tool layer disposes.** The LLM is asked exactly one
question — *which tools, with what arguments* — and never returns a number.
A hallucinated tool name is refused, not skipped: a silently dropped call is
precisely how a prompt-supplied number gets in.

**The plan is locked, and "locked" is a state, not a promise.**

§2.12's risk table assigns *"Lock the plan"* to Labs 3 and 7. Two things are
needed for that to be a control rather than a document:

| | Mechanism | Why the obvious version fails |
|---|---|---|
| **The lock** | `LockedPlan` is frozen **and** every collection is a `tuple` **and** `lock()` deep-copies | Pydantic's `frozen=True` blocks attribute rebinding but **not** `list.append`. A "locked" plan on list fields is still editable, silently. |
| **The check** | `WorkspaceResult.check_against(plan) -> DriftReport` | A lock nobody compares against is a document, not a control. |

The plan also **hashes its own content**, so tampering is detectable even by a
caller who reached past the frozen model. Two fields are excluded from that hash
on purpose: `computed_at` and `locked_at`. Without that, two runs of an
identical case would hash differently and the drift report could never
distinguish a changed plan from a clock reading.

**Deferred is not passed.** On a broad plan, 17 of 27 obligations are
`verify_step` — criteria owned by the Lab 1 steps, which Lab 3 never runs. They
are held out of the verdict and the document says `0 ran, 17 pending`, with
`coverage_is_complete` False. Calling them passed *or* failed would be a lie in
one direction or the other.

**Nothing is ever "final".** `DocumentStatus` has exactly two members: `draft`
and `ready_for_review`. §2.12 requires the human to approve before publish, and
Lab 3 has no human gate — so a state called "final" would be a state this lab
cannot honestly produce, and a type that permits it is an invitation to invent
one later. `TemplateSpec` cannot even be constructed without an `approval`
section, so the gate cannot be forgotten by whoever writes the next template.

**An unmeetable promise is worse than none.** Out-of-scope items create no
obligation: a plan with milestones disabled carries no `track_milestones`
commitment. And an obligation kind the checker does not implement **fails**,
because a kind nothing can evaluate is an obligation nobody enforces.

**"Separate run state from the archive" needs two checks, not one.**

The layer check alone is defeated by `record.model_copy(update={"layer": ARCHIVE})` —
a routine way to derive a variant record, so a draft with a corrected title would
otherwise acquire a license to be archived. The second check is on `provenance`:
*which source did this store actually read*. It cannot be forged, because a caller
cannot make its own draft claim a file it never opened. A path that exists on disk
but belongs to another course is still refused.

**The pitfall is runnable, not a warning.** `MemoryStore.fingerprint()` names the
baseline (a 16-hex content digest), and `ContaminatedArchive` removes the one
method that enforces either invariant. The result is measured, not asserted:
`9ea328fcba23de18 -> 6fd02fd9ef4fe30d`, and the no-repeat screen then flags the
system's *own* draft at cosine 0.9996. The damage is trust, not latency — a
checker that flags itself teaches its reviewer to discount it.

**The similarity threshold is calibrated from measurement, and the margin is
reported.** Both backends' thresholds are the midpoint of their own measured gap,
re-derived on every run by a test. The gaps differ by 4.7x (0.0436 for the learned
model, 0.0093 for the hermetic one), and `threshold_margins()` prints the headroom
next to the verdict, because "4 of 4 correct" hides the only number that tells you
whether the threshold survives the next data edit.

**The triage filter is not optional.** `SimilarityQuery.kinds` defaults to
`["submission"]`, because the document says *history*. Unfiltered, the nearest
thing to a new linked-list answer is the course's own linked-list lecture notes —
measured at **0.9829** against the true prior submission's **0.9785**. A plagiarism
screen that answers "have you seen this before?" with the textbook looks like
evidence and is not.

---

## Reproducibility

Every run of Labs 1, 2 and 3 is deterministic. Same input → same plan, same
hash, same questions, same claim table, same `plan_sha256`. That is what lets a
reviewer re-check the acceptance criteria without your credentials, and it is
why the `scripted` backend is the default. `tests/conftest.py` forces
`LLM_BACKEND=scripted` and deletes `GOOGLE_API_KEY`, so a stray `.env` cannot
make the suite hit the network or bill you.

The Lab 2 corpus is fingerprinted in the notebook's final cell, so a reviewer can
confirm the exact bytes that produced a result. Lab 3 goes further: the skill
layer has no clock, no LLM client and no network, so its output is a pure
function of its typed input — demonstrated in the notebook by re-locking one
input and showing the hash does not move, and by showing that a 73-year-old
`computed_at` changes nothing.

First `fastembed` model download takes ~30 s and is cached under
`.fastembed_cache/` (Lab 4). Hugging Face does not support symlinks on Windows, so
`fastembed` prints a "degraded version" warning — it is harmless, and
`HF_HUB_DISABLE_SYMLINKS_WARNING=1` silences it.

Lab 4 keeps that determinism with the default `hashing` backend: 3-5 character
n-grams, 256 dimensions, blake2b, unsalted — no clock, no model, no network. A test
loads it in a **subprocess** and asserts the scores match the in-process ones, since
an unsalted deterministic hash is only reproducible across processes if nothing
quietly salted it.

Lab 5's determinism claim is narrower and stated as one: the in-process path is
byte-identical run to run, and the **over-the-wire** path is identical to the
in-process path on every leaf field except the one that genuinely differs between
two processes, `provenance.fetched_at`. That is checked by flattening both payloads
to dotted leaf paths and comparing 329 of them — not by one `==`, which would have
said "they differ" and named nothing.

Two numbers in Lab 5 are timings and will differ on your machine: the server spawn
and the four warm calls. The *claim* they support — that holding one session open
beats respawning per call, by roughly 33× here — is what is asserted; the absolute
milliseconds are printed as evidence, not as thresholds.

Lab 6 makes **no model calls** and needs no key, so it is reproducible offline like
Labs 1–5. Two things in its output are *supposed* to differ between runs, and both
are stated as properties rather than values:

* **`wall_clock_ms`** is a measured duration, charged with `math.ceil` so a
  sub-millisecond step still costs 1 ms. The total will be 5 on a fast machine and
  a larger number on a slow one. The property is that the dimension *moves* — a
  ceiling that never moves is the runaway-loop guard §2.12 asks for, present and
  inert.
* **The audit fingerprint** is a digest over the chain's record digests, and a
  record's digest covers everything except its timestamp. Two runs of the same
  plan therefore agree exactly when they measured the same step durations, and
  the demo steps take single-digit milliseconds, so runs 3 and 4 of a batch can
  and do collide. Quoting a fingerprint as an expected value would be quoting a
  value that cannot recur. The checks assert the *shape*: 5 records, one chain,
  `verified=True`, and the same fingerprint read back from the file.

The crash proof is deterministic in the property it asserts and not in its timing:
the child exits **9** every time, and the resume runs exactly `('report',)`.

Lab 7 is deterministic in the same way. Two runs of the same case produce the
same `plan_sha256` and the same published `document_id` — the plan hash holds out
the two clock readings (`computed_at`, `locked_at`) that Lab 3 excludes — which is
why the write-up quotes the hash's first characters as an *example of one run*
rather than as an expected value. The checks assert the properties that matter:
the bound fired at `MAX_ATTEMPTS`, the artifact was written and **names the
approver**, and the pre-gate nodes did not re-run on resume. None of them asserts
a literal digest. What genuinely varies between runs is the temporary directory
the lab is given; the checks open the artifact wherever it was written instead of
naming a path.

---

## Bugs found and fixed during the build

Recorded because each is a trap the next lab will meet too. Full detail in each
lab's write-up §f.

| # | Bug | Why it mattered |
|---|---|---|
| 1 | `SkillContractError` was raised **inside** the handler's `try`, so its own `except` swallowed it | A skill returning the wrong type produced a *recorded failure*, identical in the ledger to an ordinary runtime error. Nothing crashed; the audit log lied about the cause |
| 2 | On handler failure, `spec.output_model()` was assumed to be default-constructible | It is not. `LockedPlan` and `Document` both have required fields, so the failure surfaced as a `ValidationError` about a missing `course_code`, far from the cause. Now `SkillSpec.failure_value()` returns `None` when the empty value would be meaningless |
| 3 | `recompute_sha256` raised `AttributeError` on a plan tampered with via `model_construct` | The integrity check **crashed** on exactly the malformed objects it exists to detect. The adversary got a stack trace instead of a tamper report, and the trace looked like an unrelated bug |
| 4 | The hash payload was built **twice** — once in `PlanDraft.content_payload`, once in `LockedPlan.recompute_sha256` | A comment warned they must not diverge. They did: `notes` was added to one and the other kept passing — a check reporting "no tampering" while hashing something other than the whole plan. Collapsed to one `_content_payload()` |
| 5 | `verified_facts["computed_at"]` was inside the hash | Every identical run hashed differently, so the drift check could never tell a changed plan from a clock ticking. Fixed by `VOLATILE_FACT_KEYS` / `stable_facts()`; then `locked_at` hit the same bug from the other side |
| 6 | `marks_by_assignment.ASG-01` is a **dotted path**, but `verified_facts` held a nested dict | Every conforming case reported one spurious `value_mismatch`. Added `resolve_fact(facts, key)` with a `MISSING` sentinel, because `None` is a legitimate fact value and "no tool verified this" must be distinguishable from "the tool said None" |
| 7 | Obligation ids sorted as plain strings | `S10` rendered before `S2` in the document, and since order feeds the hash, a reordered plan looked like a changed one. Natural `_sort_key` |
| 8 | `assemble()` claimed every Lab 1 step had run | Would have rendered a table of green ticks over work Lab 3 never did. `completed_steps=()`, and `DEFERRED_KINDS` so those criteria render `pending` |
| 9 | A conforming document was marked `final` | §2.12: the human approves before publish, and Lab 3 has no gate. Replaced with `DocumentStatus.{DRAFT, READY_FOR_REVIEW}` — no third member exists |
| 10 | The `include_milestones` override was recorded, then dropped before reaching `PlanInputs` | The record was built correctly and went nowhere, so the audit trail said `assumed true` while the plan used `False`. Now carried into the plan's `notes`, rendered in the document, and covered by the hash |
| 11 | Three course constants survived in `skills/` docstrings | Caught by the no-hard-coding scan — which is why it reads source text rather than AST constants. A docstring is where a temporary constant lives |
| 12 | `describe()` rendered `<class '...'>` annotations, then a literal `Ellipsis` | Cosmetic, but the skill index is a deliverable a reviewer reads |
| 13 | `proof.templates_tried` was a truthy integer | Nothing could open a rendered document and assert on it. Replaced with `documents: dict[str, dict[str, Document]]` |
| 14 | `append()` overwrote `record.layer` with the store's own layer | Made `contains_foreign_layer()` a **tautology** — the cheap CI invariant could never report a foreign layer, so it always passed and proved nothing. A defensive assignment that quietly disabled the check it appeared to support |
| 15 | The `ContaminatedArchive` bypass was incomplete: the layer check lived in `append()`, not `_check_appendable()` | The demonstration class could not actually demonstrate anything, because overriding the one method it named did not defeat the check. Both invariants were moved into `_check_appendable()` for exactly this reason — **one place to look, one place to bypass** |
| 16 | `SimilarityQuery.kinds` defaulted to `[]`, meaning *no filter* | The "history" retriever returned the course's **own lecture notes** as a student's nearest prior work. The default is now `["submission"]` |
| 17 | `Embedder.embed()` iterated a bare `str` character-by-character | Produced one directionless vector per character instead of one document's vector. It ranked *something*, which is worse than failing — now `Sequence[str] | str` means one document for a bare string |
| 18 | `MemoryBoundaryError` was never imported in `retrieval.py` | `raise_flag`'s own guard raised `NameError` instead of the boundary error it documents. The guard existed to convert a corruption into a clean refusal, and it produced a stack trace |
| 19 | `raise_flag`'s `threshold` defaulted to the module constant | A flag could report a threshold nobody applied. It now copies `result.threshold_used` |
| 20 | `lab_threshold_report()` measured a hand-written probe list; the acceptance checks measured the real cases | The two printed different numbers for the same claim. `cases()` is now the single source and the report derives its rows from it |
| 21 | `SimilarityFlag.summary()` formatted the threshold `.2f` | `0.995` printed as `0.99`, contradicting the threshold table two lines above it. A formatting bug that reported a *different rule* than the one in force |
| 22 | `tools/__init__.py` re-exported `get_seed_store`, `LocalSeedStore`, `SeedStoreError` | Three names that made Lab 5's pitfall — *"no agent should touch a source directly"* — a one-line affair. A re-export is an affordance |
| 23 | `config.py::get_or_create_connector_token` wrote a **generated** token into `.env` | Every run left a credential on disk that nobody chose, and the next run silently reused it. Now ephemeral, in memory only |
| 24 | `memory/stores.py` resolved its allowed-provenance set from `LocalSeedStore.archive_sources()` | A second front door — and a worse one than an agent's, because that set is the allow-list `MemoryStore.append()` **enforces**. The layer could have widened the check it exists to perform. Found by the AST scan, not by review |
| 25 | `EndpointSpec.added_by_lab` defaulted to `"Lab 5"` and stood in for `documented` | All four endpoints reported themselves as additions, **including the three the document names** — an inverted traceability matrix. `documented` now comes from matching `doc_read` against `DOCUMENT_READ_PHRASES` |
| 26 | `list_courses` returned no provenance, and `LocalSeedAdapter.backing` returned `[]` for `kind="courses"` | Two causes stacked: the call omitted `adapter=`, and the adapter had no answer. Either alone gave an empty provenance — the one endpoint whose whole job is to describe the corpus |
| 27 | Test-side: `_python_files()` was not BOM-tolerant; the lying-MCP-server fixture fabricated its payload | An AST test dying with `SyntaxError: U+FEFF` reports an encoding artefact as a structural violation. A fixture built from scratch can pass for a server that would never return one; it now starts from a real payload with one field corrupted |
| 28 | Spawning the MCP server from a notebook cell raised `io.UnsupportedOperation: fileno` | `ipykernel` replaces `sys.stderr` with a stream that has no OS descriptor, and `mcp`'s `errlog` default is bound to it at import. The deliverable format is a notebook. The first fix **silently did nothing** — `stdio_client` is `@asynccontextmanager`-wrapped and `functools.wraps` does not copy `__defaults__`, so the patch hit the wrapper, not the function |
| 29 | `_flatten` emitted `notes.[3].body` | A difference inside a list was unnameable in the report that exists to name differences |
| 30 | The matrix's scope column was padded to a hardcoded `9` while its longest value is `45` (`assignments:read, notes:read, submissions:read`) | The faculty and TA rows ran together (`submissions:read2`), corrupting the table a reviewer uses most. Every column width is now measured from its data and asserted structurally (all lines share one offset-equality width), so the fix cannot be another constant |
| 31 | Checkpoint ids were `digest[:24]`, and LangGraph's `get_tuple` selects `ORDER BY checkpoint_id DESC` | A **string** compare over uniformly-distributed hex, so `load()` returned an *arbitrary* checkpoint from a five-step run — every time, and randomly. Now `f"{write_seq:012d}-{digest[:12]}"`, with the ordinal allocated from the SQLite table rather than from `RunState`, because a counter in the state does not survive a second process |
| 32 | `BudgetLedger.reserve()` checked, and `charge()` committed | Every dimension in every report read `0`, so **the budget could never refuse anything**. The whole feature was a set of comments. `reserve()` now checks and commits in one step; `charge()` remains for what is only knowable afterwards (`records_read`, `wall_clock_ms`) |
| 33 | A failing step was audited but its new chain position was never checkpointed | The next append reused a sequence number the log already held, and `AuditIntegrityError` fired **on an untampered log**. An integrity check that cries wolf is worse than none. Fixed with `_halt_at` |
| 34 | `resume()` never checked that the audit log belonged to the checkpoint | A wiring mistake surfaced as *"audit chain broken"*, naming the log as the culprit when the wiring was wrong. Fixed with `_assert_same_log` and a `reason` on `ResumeDivergence` so the message names the real problem |
| 35 | Four in one pass: the `Budget` limits validator ran in "after" mode; `ScriptedGate.remaining` double-subtracted; `InMemoryCheckpointStore` lacked `write_seq`; the generated `run_id` derived from `id(self)` | Pydantic coerced `True` to `1`, so a typo became a one-step budget and a run stopped for a reason nobody could reconstruct. `id(self)` is a **memory address**: CPython reuses them, so a later runtime could be handed a `run_id` a resume thread already used. Now `uuid4` |
| 36 | A second `run_lab06` into the same directory appended a fresh chain to a full one | Found by the **notebook**, which runs the lab twice on purpose — the test suite's fixture used two separate directories, so the suite passed. `AuditIntegrityError` named the audit log as the culprit, the one component that had done nothing wrong. Fixed at both ends: a per-invocation `run-<8 hex>` directory, and a `RuntimeBoundaryError` on a fresh run pointed at an occupied thread |
| 37 | A test asserted two identical runs must produce different fingerprints | A fingerprint is a digest over record content; `duration_ms` is in the digest but `at` is not. Two runs that measured identical single-digit millisecond durations produced identical chains — correctly. The assertion was the opposite of the documented contract, failed only under load, and hid a false claim in this document. Fixed the test to assert structural separation (separate files, same chain shape) and the contract (equal chains => equal fingerprints). |
| 38 | Two of doc §2.7's four checks were bound **directly** into the dispatcher while the other two unpacked a context | The direct ones raised `TypeError: 'CheckContext' object is not iterable`. A malformed dispatch would have crashed at the gate instead of returning a verdict — and a crash looks like an infrastructure fault, not a failed check |
| 39 | `specialists.py` called a non-existent `ToolResult.failure()` | The error path *crashed while reporting an error*. The API is `.unwrap()`, which raises `ToolError` carrying the connector's own message. A handler that dies on failure is worse than no handler |
| 40 | `assignment_distribution` dropped `due_date` | The milestone specialist then raised `KeyError`, so a field omitted three steps earlier surfaced as a crash in an unrelated agent |
| 41 | `run_specialists` handed every specialist the **same** state | `grading` never saw `similarity`'s scores, so the run escalated while both agents reported `ok=True` — two green lights over a broken pipeline. Now threads `{**state, **merged}` |
| 42 | `assemble` subscripted a pydantic `LabStep` (`s["step_id"]`) | Pydantic models are attribute-access, not dicts; the assembled table raised `TypeError` |
| 43 | `ingest` passed a `NotesQuery` where `compute_scope_validation` needs a `ScopeQuery` | The query models are `extra="forbid"`, so it failed **loudly** — which is the point of the model. A permissive model would have silently validated an empty scope |
| 44 | Five state keys were undeclared, so LangGraph **silently discarded** them | The plan locked an empty scope and reported three false "out of scope" findings. LangGraph drops undeclared keys without an error; the AST guard that now catches this was written because of it |
| 45 | `assemble` reported facts the plan never verified | 11 drift findings on a **correct** run, which trains a reviewer to ignore the drift table. Now reports only `GROUNDED_FACT_KEYS`; 0 findings |
| 46 | Similarity scores were **fabricated** — SHA-256 of the submission id | Every stored `similarity_score` is `None`, so any number was invented. Replaced by a real lexical measure (trigram shingles, Jaccard), which is what §2.7's *"a score is attached"* actually asks for |
| 47 | `describe_topology` used `draw_ascii()`, which needs the optional `grandalf` | `ImportError: Install grandalf to draw graphs` — the topology renderer is evidence, so it cannot depend on a drawing library. Reads the structural edge set instead |
| 48 | `resume_run` ignored its `root` argument | Wrote the published document into the repository instead of beside the checkpoint, so a resumed run polluted the tree it was run from |
| 49 | The published document **contradicted itself** | Lab 3's formatter hard-wires `Approved : no` / `Published: no` for a draft, so an approved run carried those lines above its table of published grades. Stamped in `publish`; Lab 3's tested module untouched |
| 50 | `FacultyDecision` accepted any string for `decision` | A typo became a refusal — a silent, consequential mis-reading of the human. A normalising-then-validating `field_validator` now rejects anything outside `approve \| edit \| override \| refuse` |
| 51 | `context.py` imported a non-existent `SkillRegistry` and an unused symbol | Would have raised `ImportError` the moment the module was imported outside the happy path |
| 52 | Three in one pass: the retry ordinal baked spaces into its fragment (`" Attempt 3 "`); the checkpoint emitted four `Deserializing unregistered type` warnings per resume; topology-only compiles warned *"compiled the graph without a checkpointer"* | The first rendered a double space after *"forever."* in the diagnosis a human reads. The second is LangGraph's own warning, whose remedy is the `allowed_msgpack_modules` allowlist now declared in `runner.py`. The third trained a reader to ignore the one warning that matters when a graph is about to run |

**52 defects found and fixed across Labs 1–7.**

Bugs 14, 15, 24, 28, 30 and 36 share one shape worth naming: **the defensive code
was what made the check untestable, or the safety mechanism misfired and pointed at
an innocent component.** Bug 14's `record.layer = self.layer` made the layer
invariant a tautology. Bug 24 let the layer that is checked supply its own
allow-list. Bug 28's first fix was a patch that reported success while rebinding a
function's `__defaults__` on the wrong object. Bug 30 fixed a rendering fault with
another constant. Bug 36's chain check correctly detected a broken chain and then
blamed the *log* for what was a `thread_id` collision — which costs more in a
reviewer's time than the defect it was built to catch, because it sends them to the
wrong file.

Bug 32 is a different shape and arguably the more instructive one: **the code was
correct and the feature was inert.** Checking a budget after the work and charging
it is the textbook order, and it produces a system where the budget is a report.
The same shape reappeared as `wall_clock_ms` charged from a measured 0.4 ms
duration: physically correct, and the dimension never moves, so §2.12's runaway-loop
ceiling is present, plausible and inert. Precision is not correctness when the
quantity is supposed to accumulate.