# Lab 8 Self-Audit

| Item | Evidence/Location | Status |
|------|-------------------|--------|
| All evidence files are UTF-8 without a BOM, pure ASCII | `docs/evidence/lab08_cli.txt` (10,323 chars, BOM=False, non-ascii bytes=0), `lab08_pytest.txt` (574 chars), `lab08_notebook_output.txt` (36,727 chars) | PASS |
| CLI passes self-checks | `python -m course_organizer.labs.lab08_parallel_swarm` → `OVERALL: PASS (27/27 checks)`, exit 0 | PASS |
| Lab 8 tests | `pytest tests/test_lab08_swarm.py` → `104 passed` | PASS |
| Full suite | `pytest -o addopts=` → `785 passed, 1 skipped` (68s) | PASS |
| No timing invariants quoted as contracts | `docs/lab08.md` §b/e: only relationships asserted (`parallel stage_ms < sequential stage_ms`, `2 <= max_overlap <= SWARM_MAX_CONCURRENCY`) | PASS |
| Structural concurrency bound | `max_overlap <= SWARM_MAX_CONCURRENCY (8)`, checked by tests and CLI (check 18) | PASS |
| Overlap >= 2 proves fan-out ran concurrently | check 17, test `test_the_parallel_fan_out_really_ran_concurrently` | PASS |
| Merge recomputes alignment | `merge_results` never reads `self_reported_alignment`; uses `alignment_to_plan` (regression test covers per-candidate marks) | PASS |
| Reducer is append for concurrent channels | `swarm_candidates` and `swarm_intervals` are `Annotated[..., append]`; annotation asserted | PASS |
| Send fan-out is a conditional edge | compiled graph shows `[cond] swarm_dispatch -> swarm_worker` with explicit path list | PASS |
| Lab 7 files untouched | `builder.py`, `nodes.py`, `runner.py`, `state.py`, `validation.py` and Lab 7 tests unchanged | PASS |
| Package star-import fixed | `graph/__all__` imports `CHECKPOINTED_TYPES`, `checkpoint_serializer`; locked by `test_the_package_star_import_resolves_every_declared_name` | PASS |
| Durability containment respected | `_EXPECTED_IO_CALLS["graph/swarm.py"]=1`, `["labs/lab08_parallel_swarm.py"]=3` in `tests/test_lab05_mcp_connector.py` | PASS |
| Evidence cites rather than fabricates | all numbers come from evidence files; notebook warms both topologies and discards warm-up | PASS |
| Notebook clean execution | 44 cells, 22 code, 22 executed, 0 errors; temp dir cleaned at end | PASS |
| Requirements traced | `docs/traceability_matrix_lab08.md` maps R1–R17 to code/tests/evidence | PASS |
