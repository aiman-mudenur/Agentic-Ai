# Lab 8 Traceability Matrix

| R# | Doc requirement (verbatim) | Code | Tests (behaviour) | Evidence | Verified |
|---|---------------------------|------|-------------------|----------|----------|
| R1 | "Lab 8 - Parallel Swarm - many assignment submissions, then merge" (L699) | `graph/swarm.py::build_work_items`, `fan_out` | work items cover both kinds; submissions before units | `docs/evidence/lab08_cli.txt` (scenario 2) | PASS |
| R2 | "Parallelise the per-unit work and select the best." (L700) | `graph/swarm.py::fan_out` (Send), `merge_results` | parallel fan-out exists; best selected per item | `docs/evidence/lab08_cli.txt` checks 2,3,11 | PASS |
| R3 | "Several worker agents process assignment submissions in parallel; a merge/selector picks the best per the plan..." (L701) | `graph/swarm.py::make_swarm_worker`, `build_work_items` (submissions priority 2) | submission items dispatched first, priority-weighted quality | checks 6,7,15; quality table | PASS |
| R4 | "Locked plan; per-unit work items." (L704 inputs) | `swarm_dispatch` reads `plan_locked`; `plan_scope` built | all 13 items processed from locked plan | check 8,20 | PASS |
| R5 | "Best-of-N results merged into one compliant output." (L704 outputs) | `merge_results`, Lab 7 `assemble`/`publish` | 3 strategies/item, 8 grades published (4713 chars) | checks 9,22 | PASS |
| R6 | "Parallel workers + a Merge selector under the Coordinator." (L704 agents) | `swarm_worker` ×39, `swarm_merge`, under Lab 7 pipeline | topology shows dispatch→worker→merge | checks 1,2,3 | PASS |
| R7 | "A fan-out/fan-in that processes all units and merges by alignment to the plan. Accept: Parallel run matches or beats sequential quality at lower wall-clock time." (L704) | `fan_out`, `merge_results`, timing (`swarm_timing`) | all units processed; identical winners; parallel < sequential; 2<=overlap<=8 | checks 8,11,13,14,16,17,18,19 | PASS |
| R8 | "Parallel worker fan-out + merge." (L706) | conditional Send edge + `swarm_worker→swarm_merge` | edge set read from compiled graph | checks 1,2,3 | PASS |
| R9 | "Merge strictly by plan alignment; picking the 'nicest' output quietly breaks coverage." (L707) | `merge_results` recomputes alignment; tie-break on declared order | 'nicest' (highest marks) rejected when alignment ties; log shows it | check 12; alignment table | PASS |
| R10 | `parallel_map(process_assignment, plan.units)` / `merge(results, key=alignment_to(plan))` (L710) | `fan_out` emits Sends; `merge_results` uses `alignment_to_plan` as key | one Send per (item,strategy); recomputes score | checks 2,11 | PASS |
| R11 | "Candidates calibrated and screened; best-aligned selected, rest logged." (L573 §2.7) | `align_*`, `merge_results`, `write_candidate_artifact` | 39 candidates, 13 selected, 26 logged; each durable record | checks 9,10,26,27 | PASS |
| R12 | "on failure only this assignment submission is regenerated." (L574 §2.7) | per-item work items; swarm runs on every retry | exhaustion shows swarm stage ran each attempt | check 24,27 | PASS |
| R13 | Four validation checks still gate (L558 §2.7) | Lab 7 `validate` after `swarm_merge` | run stops at gate with §2.7 checks passing | check 20 | PASS |
| R14 | "Grounded generation... evidence record per unit; ungrounded items rejected." (L823 §2.11) | `UNIT_OBLIGATIONS.no_overclaim`, per-candidate evidence | `no_overclaim` screen; 39 evidence records | check 26; evidence files | PASS |
| R15 | "Bound `MAX_ATTEMPTS`... escalate to course faculty with diagnosis" (L825 §2.11) | Lab 7 bound (unchanged) | exhaustion escalates at 3 attempts, names failing checks + diagnosis | checks 23,25 | PASS |
| R16 | "Never publish grade/feedback without faculty approval." (L830 §2.12) | Lab 7 real interrupt gate, `Command(resume=...)` | nothing published before gate; approved run publishes | checks 20,21,22 | PASS |
| R17 | "Keep clear version history for notes and materials." (L833 §2.12) | attempt in filenames, per-run dirs `out/lab08/<run_id>/` | different attempts do not overwrite; records carry attempt/provenance | evidence filenames; records | PASS |
