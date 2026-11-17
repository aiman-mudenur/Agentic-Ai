# Lab 10 — Self-Healing Validation: Checklist

Phase C · Applied reliability. Validation failures become typed findings;
failing items regenerate in a targeted way; the unfixable escalates with a
diagnosis. The loop stays bounded, the gate stays human.

## Requirement → code → test

| # | Requirement | Code | Test |
|---|---|---|---|
| 10.1 | Typed findings: failing check, affected submission/item ids, rule, diagnosis, repairability, repair scope | `graph/repair.py`: `Finding` (`check`, `submission_ids`, `rule`, `diagnosis`, `repairable`, `repair_steps`, `repair_target`); `diagnose()` | Per-check finding tests; unknown-check and ambiguous-item cases escalate without mutation |
| 10.2 | Official checks incl. rubric-max, late flags, similarity metadata, no-publish-without-approval | Unchanged `graph/validation.py` checks; findings read their verdicts | Existing Lab 7 check tests untouched and green |
| 10.3 | Fault injection proving detection | `fail_until_attempt` seam + targeted tampering (flipped late flag, dropped score, deleted/garbage table, unapproved row, cross-student decoy) | Injection→exact-finding tests; tampered-late end-to-end heal |
| 10.4 | Targeted repair only; preserve valid work, plan, evidence | Workers retry branch: deterministic `sync_grade_metadata` (flags/metadata only — marks/feedback/status/timestamps never touched) + `run_specialists(only=[...])` subset + `splice_grades` (failing ids replaced, passing carried verbatim, missing added) | Splice/purity unit tests; two-pass preservation test (grades equal, plan hash intact); evidence rebuilds every validate pass and is re-checked, never assumed |
| 10.5 | Revalidate after every repair; finite budget; escalate when unsafe | Topology unchanged; `next_route(..., unrepairable=False)` (default preserves all existing routing); validate node passes `True` only when failing non-empty, attempts ≥ 1, zero auto-handled findings → immediate `escalate` (`reason="unrepairable"`); `MAX_ATTEMPTS` exhaustion path untouched | Routing contract tests; immediate-escalate test with budget left; exhaustion test |
| 10.6 | Audit validation/diagnosis/attempts/revalidation/exhaustion/escalation in existing channels | Findings + `unrepairable` in validate entry extras; `repaired_submissions`/`regen_steps` in workers entry extras; declared `repair_findings` state channel; no new node, still one entry per node per pass | Entry-extras assertions; node-count test green |
| 10.7 | Repair success, exhaustion, non-repairable, preservation | `tests/test_lab10_self_heal.py` (16 tests): success via nodes, exhaustion via budget, unapproved blocked + escalated, preservation quartet | All green |
| 10.8 | Labs 1–9 regression, esp. graph/grounding/gate | Lab 7 graph/runner, Lab 8, Lab 9 suites; Lab 7 CLI 18/18 exit 0; full suite | Below |

## Design notes (honest boundaries)

- **No second pipeline.** Repair reuses `run_specialists(only=[...])`, the existing bounded edge, and the existing escalation builder. First passes run all six specialists exactly as before (roster contract intact).
- **Unapproved publication is never repaired.** No worker grants approval; the finding carries empty steps and the run escalates. Verified by test with budget remaining.
- **Late repair moves flags, never timestamps.** The flag is re-derived from store seconds (the doc's own remedy); `submitted_at` rows are asserted identical before/after.
- **Deterministic rebuilds are revalidated, not trusted.** Repaired candidates get fresh evidence tables in the same validate pass and must pass all five checks to advance; a table/candidate mismatch fails closed.
- **Note-body limitation stands** (Lab 9): no repair path needs note bodies; stale-version cases classify as faculty escalation. Nothing fabricated.
- **Three existing tests tracked the new contract** (`len(DOC_CHECKS)` 4→5, passing-report evidence, runner count 5) — intent preserved, not weakened. The wall-clock timing test (`test_the_parallel_pass_is_lower_wall_clock_than_the_sequential_pass`) failed once under full-suite load and passed isolated (1.06s); pre-existing load flake, unrelated to this change, reported as-is.
