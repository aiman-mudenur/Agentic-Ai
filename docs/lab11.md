# Lab 11 — Tested Build Factory: Checklist

Phase C · Applied reliability. A build takes an approved locked plan, runs
the existing graph, and returns a typed verdict. Green means "a faculty
member may review" — never approval or publication.

## Correction: the authorized plan governs execution (not just the verdict)

The audit found `run_build` validated the input plan but never passed it in:
the graph re-locked scope from the store, and only a post-run diff stood
between a wrong plan and review. Corrected:

- `run_pipeline(..., authorized_plan=...)` forwards the verified dump into the
  graph seed (additive optional arg; all 30+ existing callers unchanged).
- `CourseState.authorized_plan` declared (undeclared keys are discarded).
- The plan node re-verifies (parse, hash, locked, course, trusted
  `ctx.human_authority`, store-supported scope from ingest facts — missing
  facts fail closed) and **adopts** the plan: `plan_locked`/`sha`/`obligations`
  verbatim, `units_requested` + `scope_assignment_ids` narrowed to it, hash in
  the audit trail. No re-lock of a different plan. Absent key = old path.
- Distribution + collection filter to the authorized aids **only when the key
  is present** (explicit guard); grading/similarity/milestones/evidence follow
  automatically since they read filtered state. No-authorized-plan runs are
  byte-identical.
- `build.py` passes the verified dump, maps `"authorized plan"` failures to
  `blocked` (not infrastructure), and additionally requires executed scope ==
  input scope. Post-run `diff_plans` stays as defense in depth.

## Requirement → code → test

| # | Requirement | Code | Test |
|---|---|---|---|
| 11.1 | Reproducible entry taking a validated locked plan + authorized course context | `src/course_organizer/build.py`: `BuildRequest` (plan dict, course, run id, bound, dirs, test-only fault flag); `scripts/run_build.py` CLI (exit 0 ready, 1 otherwise) | Passing-build test; CLI demo transcript `docs/evidence/lab11_cli.txt` |
| 11.2 | Runs the existing graph, no duplicate pipeline | `run_build` calls the existing `run_pipeline` unchanged (no runner edits); units/course come from the request, scope re-derives from the store per Lab 7 honesty rules | Full Lab 7/8 suites green without modification |
| 11.3 | Typed result: artifact, acceptance report, failed checks, evidence | `BuildResult` (status/reason/run/plan-sha/artifact/report/failed/evidence/audit/gate/can_advance/published-always-None), `AcceptanceReport` (conjunction of §11.4) | Field assertions in every build test; `build_report.json` written + parsed |
| 11.4 | Full acceptance incl. grounding + repair/revalidation | `evaluate_acceptance`: 5/5 checks present + passing, evidence per graded candidate, gate awaiting, nothing published, plan identity verified — each independently blocking | Incomplete/missing-evidence/published unit tests; repairable-injected test heals (attempts==2); exhausted test escalates |
| 11.5 | Hard gate: failed/incomplete reports never advance | Statuses `blocked`/`escalated`/`refused` carry `can_advance=False`; refusals happen before any run directory exists; build module has no resume/publish path (AST test) | Refusal tests assert no dirs; bypass tests (AST + monkeypatched resume) |
| 11.6 | Green permits review only | No resume/approve/publish import or call; result says who may act next | AST + behavioral tests; `published is None` in every test |
| 11.7 | Passing/failing/malformed/unlocked/missing-evidence/injected/exhausted/bypass scenarios | `tests/test_lab11_build.py` (13 tests, synthetic seed fixtures, offline) | All green |
| 11.8 | Preserve connector/runtime/graph/audit/approval/Labs 1–10 | Zero changes outside `build.py`, `run_build.py`, `run_pipeline` seed passthrough (additive default-off), docs | Full suite green; no existing test modified |
| 11.9 | Synthetic fixtures only | CS201 seed corpus + tmp dirs; conftest forces scripted backend | Suite config unchanged |
| 11.10 | Plan identity enforced, not excused | Upfront: parse/hash/locked/course/units checks. Post-run: hash + order-sensitive field diff (`units`, `assignment_ids`, obligations count, non-volatile facts) via `diff_plans`; mismatch blocks even a passing run | Mismatch test (reordered units → blocked with named diff); tamper/contradiction tests |

## Design notes (honest boundaries)

- **The runner builds its own plan node — by architecture.** Scope always re-derives from the store (Lab 7 honesty rule). Identity is therefore proven two ways: the input plan constrains the request (same course, non-empty locked scope) *before* execution, and the run's locked content is compared field-by-field *after*. A mismatch blocks with the differing fields named; the hash is the authority, the diff is the diagnosis.
- **Refusals create nothing.** Verified by asserting the run directory absent — not by log reading.
- **`fail_until_attempt` on `BuildRequest` is test-only instrumentation**, documented on the field. Real requests leave it 0.
- **Lab 8 timing test:** failed once in a focused file run under load (115.6ms vs 82.7ms), passed isolated (1.06s/1.14s), and the full suites it ran inside passed with zero failures. Unchanged and unquarantined per instruction; history recorded, not hidden.
