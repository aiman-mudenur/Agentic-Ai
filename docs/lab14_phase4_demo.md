# Lab 14 Phase 4 — End-to-End Capstone Demo

This demo exercises the whole course-organizer stack as one workflow:

**Request → Coordinator → parallel workers → domain specialist → validation/repair → faculty approval gate → publication → dashboard status**

Every step is real: the graph suspends on a genuine LangGraph interrupt, and
publication happens only after an explicit faculty decision is fed back through
the gate. Nothing is auto-approved, nothing simulates the gate, and the default
demo needs no Google account, no OAuth, no network, and no live LLM.

## Prerequisites

Python 3.14 with the project virtualenv:

```powershell
python -m venv $HOME\.virtualenvs\course-organizer
$HOME\.virtualenvs\course-organizer\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
python -m scripts.seed_demo
```

The demo runs on the `scripted` LLM backend (deterministic, offline) and the
local seed corpus (`CS201 Data Structures`) by default. No API key or
`credentials.json` is required; those are used only by the Google Classroom
connectors, which the demo does not touch.

## 1. Fastest path: the CLI demo (no server, no browser)

From the repository root, with the virtualenv active:

```powershell
# Start a run and HOLD at the faculty gate (the default):
python -m scripts.run_lab14_demo --domain lab

# Examine the printed gate request, then decide:
python -m scripts.run_lab14_demo --resume demo-lab-<id> --decision approve
python -m scripts.run_lab14_demo --resume demo-lab-<id> --decision refuse
```

`--domain` accepts `theory`, `lab`, or `project`. Each invocation prints:

- the **run id** and the **checkpoint path** (`out/demo/lab14/<run_id>/graph.sqlite`),
- the nodes executed so far,
- the pending **faculty review block** (course, domain, specialist outcome,
  validation result, draft grade count, similarity flags, audit trail),
- after a decision, every **artifact** with its size, and the published
  workspace path.

Exit codes: `0` success (published, or cleanly refused); `1` the run did not
publish (escalated/specialist failed); `2` governance blocked publication
(approval was attempted on a run whose specialist failed); `3` usage error
(invalid domain/decision, or a run id that already has a checkpoint).

Each run gets its own checkpoint directory (default root
`out/demo/lab14`, override with `--root`), so demos never inherit another
run's state and never touch the API's `out/lab07` runs. Re-using an existing
run id fails loudly rather than silently resuming the old thread.

## 2. Full stack: API + faculty dashboard

### Launch the API

```powershell
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

### Provision a faculty account (one-time)

Public signup deliberately downgrades privileged roles to student, so a
faculty account is provisioned explicitly (the same way an operator seeds an
institution's first accounts):

```powershell
python -m scripts.provision_faculty --email fac@example.edu --password 'choose-a-pass'

# HoD / admin / student account (password required for a new account):
python -m scripts.provision_faculty --email you@gmail.com --password 'choose-a-pass' --role hod

# Promote an account that already exists (e.g. one a Google sign-in created
# as a student). No password needed - it is never changed:
python -m scripts.provision_faculty --email you@gmail.com --role faculty
```

Both public signup and Google sign-in clamp a privileged role request down to
`student`, because the role in an OAuth `state` parameter is editable in the
address bar and so can never be trusted to grant privileges. A Google sign-in
for an email that already exists in the database gets that stored role, so
provisioning first is what makes `faculty`/`hod`/`admin` sign-ins work.

### An account that is both faculty and HoD

Repeat `--role` to grant more than one hat. The first is the account's primary
role; the rest are extra roles it may switch to after signing in:

```powershell
python -m scripts.provision_faculty --email you@gmail.com --password 'choose-a-pass' --role faculty --role hod
```

Sign in normally and the post-login page offers an **"Act as"** selector. Only
one role is active at a time: acting as HoD closes the faculty endpoints, and
acting as faculty closes the HoD ones.

The selector can only move between roles already stored on the account. The
active role is re-checked against the database on every request
(`api/deps.py:get_current_user`), so an operator demoting an account takes
effect immediately, and no request can widen its own access. The account's
primary `role` column is never rewritten by acting as another role.

### Run a pipeline through the API

All pipeline endpoints require a faculty JWT (`POST /auth/login` with the
provisioned email/password).

```powershell
# Start a run (suspends at the real gate):
curl -X POST http://127.0.0.1:8000/runs/pipeline/start `
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" `
  -d '{"course_code":"CS201","run_id":"demo-api-001","domain":"lab"}'

# Read the pending status (authorized, read-only):
curl http://127.0.0.1:8000/runs/demo-api-001/status -H "Authorization: Bearer $TOKEN"

# Approve (publishes) or refuse — the only way a run ever publishes:
curl -X POST http://127.0.0.1:8000/runs/pipeline/resume `
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" `
  -d '{"run_id":"demo-api-001","decision":"approve"}'
```

Artifacts land under `out/lab07/<run_id>/`; the status endpoint returns only
scoped run metadata (no student data, no credentials).

### Launch the dashboard

```powershell
cd ui_roles
python -m streamlit run Home.py --server.port 8501
```

Log in as the faculty user. The **Run Status** section on the Faculty
Dashboard lists pipeline runs and distinguishes the five states:

| State | What the dashboard shows |
|---|---|
| Running / draft | `Status: draft` (workers done, no decision yet) |
| Awaiting approval | `Awaiting faculty approval` (graph suspended at the gate) |
| Published | `Published` |
| Refused | `Refused/escalated` |
| Failed validation | `Failed validation - escalated, not published` |
| Failed specialist | `Specialist result unsuccessful` (red) |

The section only shows runs for courses the faculty member teaches (courses
where they created an assignment), matching the app's existing authorization
convention. Create one assignment via the dashboard or `POST /assignments/`
to associate yourself with CS201.

## 3. Expected results

A healthy run on the seeded corpus:

- `ingest → plan → workers → validate → assemble` execute, then the graph
  **suspends** at `gate` (`awaiting_gate: true`).
- The gate request shows the course (`CS201 Data Structures`), the requested
  domain, `specialist_ok: true`, `validation passed=true`, 8 draft grades,
  and 2 similarity flags awaiting review.
- Approval publishes a workspace document
  (`DOC-CS201-organised-course-<hash>.md`, ~4.8 KB) containing the locked
  blueprint, the plan hash, and per-unit sections, plus the `graph.sqlite`
  checkpoint, both under the run's directory.
- Refusal records the decision and publishes nothing.
- Approving a run whose specialist failed returns HTTP 409 and writes no file.

## 4. Where things live

| Item | Location |
|---|---|
| CLI demo checkpoints/artifacts | `out/demo/lab14/<run_id>/` |
| API-run checkpoints/artifacts | `out/lab07/<run_id>/` |
| Published workspace | `<run_dir>/DOC-<course>-organised-course-<hash>.md` |
| Checkpoint (resume state) | `<run_dir>/graph.sqlite` |
| Run status (machine-readable) | `GET /runs/{run_id}/status` (API) |
| Run status (visual) | Faculty Dashboard → Run Status |
| Metrics | `GET /metrics/overview`, dashboard Metrics section |
| Audit trail | Inside the run's gate request (`audit trail:` lines) |

## 5. Limitations

- Runs execute synchronously inside the start request; a run is observably
  "running" only while that call is in flight. The first persistable state is
  the gate suspension.
- The Streamlit page itself is not covered by automated tests; the Phase 4
  suite tests the status endpoint contract it consumes (`awaiting_gate`,
  `status`, `specialist_ok`, `domain_error`, `nodes_ran`, `course_authorized`)
  and asserts the page still maps those fields to distinct state labels.
- Approving or refusing a run is exposed both as a resume API call and via the
  CLI demo; there is no approve button in the dashboard (grades approval is a
  separate, existing feature).
