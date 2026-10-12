# Lab 8 Report (Submission-Ready)

**Title:** Lab 8 — Parallel Swarm: fan out, merge by plan alignment

**Phase:** B — Orchestration  
**Status:** Complete (104/104 tests pass; CLI 27/27 checks, exit 0)

## Objective
Implement §2.10 Lab 8: fan-out/fan-in over work items, merge by plan alignment, accepted when the parallel run matches or beats sequential quality at lower wall-clock time. The implementation is additive (no changes to Lab 7’s graph/nodes/runner/tests).

## Design
- Work items: both submission (8) and unit (5), submissions first (priority 2 vs 1). 13 items × 3 deterministic strategies = 39 candidates per pass.
- Fan-out: conditional `langgraph.types.Send` from `swarm_dispatch` to `swarm_worker` (one Send per (item,strategy)); fan-in at `swarm_merge`. `swarm_candidates` uses `Annotated[..., append]` reducer.
- Merge: recomputes alignment via `alignment_to_plan` (never trusts `self_reported_alignment`); selects maximum alignment per item, tie-break by declared strategy order. Logs every candidate (winners and losers). Priority-weighted quality (denom 21).
- Concurrency: bounded fan-out `SWARM_MAX_CONCURRENCY = 8` (measured). Structural checks: `2 <= max_overlap <= 8`. Only GIL-releasing work is per-candidate durable write (`open → write → flush → os.fsync`), which drives the parallel win.
- Topologies: parallel and sequential differ only in swarm entry node; Lab 7 back-edge and gate preserved.

## Evidence
- CLI: `docs/evidence/lab08_cli.txt` — `OVERALL: PASS (27/27 checks)`
- Pytest: `docs/evidence/lab08_pytest.txt` — `104 passed`
- Notebook: `docs/evidence/lab08_notebook_output.txt` — 44 cells/22 code/22 executed/0 errors; parallel < sequential; inside pool width; identical winners; warm-up shown and discarded
- Traceability: `docs/traceability_matrix_lab08.md` (R1–R17)
- Self-audit: `docs/self_audit_lab08.md`

## Measured results
- Candidates: 39 produced, 13 selected, 26 rejected (all logged)
- Priority weights: submissions 2, units 1 (denom 21); quality 1.0 by kind
- Stage-speedup (CLI): ~1.4x at identical quality
- Concurrency: `parallel peak_overlap=8`, `sequential peak_overlap=1` (≤ SWARM_MAX_CONCURRENCY)
- Published: 8 approved grades, 4,713 ASCII chars (`DOC-*-organised-course-*.md`)
- Exhaustion (injected defect): escalates at 3 attempts, names failing checks (`draft_grade_within_rubric_maximum`, `similarity_score_attached`), all 117 candidate records on disk

## Bugs found/fixed (Lab 8)
1. `graph/__all__` declared names without importing them → star-import `AttributeError` (fixed; locked by test)
2. `merge_results` reused stale `content` variable → all merge-log records showed last candidate’s marks (fixed with per-candidate `cand_content`; regression test added)
3. Unbounded `Send` fan-out unstable across contexts (kernel lost vs plain process won) → pinned `max_concurrency=8`, structural bound asserted
4. Cold-start asymmetry in comparison → notebook warms both topologies and discards warm-up
5. Test-authoring corrections (alignment formula, overclaimed violations, fan_out reads `swarm_items`, `TypedDict`/`get_type_hints` with `include_extras=True`, tie-break behaviour)

## Reproduce
```bash
cd "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
python -m course_organizer.labs.lab08_parallel_swarm
python -m pytest tests/test_lab08_swarm.py -o addopts=
python -m pytest -o addopts=  # 785 passed, 1 skipped
```

## Conclusion
Lab 8 meets all stated requirements (R1–R17). The fan-out/fan-in is a real `Send` fan-out with a measured, structural concurrency bound; selection is strictly by plan alignment with deterministic tie-break; the parallel run matches sequential quality at lower wall-clock time; and evidence is captured (ASCII, no BOM). The implementation remains additive and preserves Lab 7’s behaviour.
