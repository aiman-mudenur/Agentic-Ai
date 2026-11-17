# Lab 9 — Grounding and Evidence: Checklist

Phase C · Applied reliability. Every candidate carries an evidence table;
ungrounded candidates fail validation instead of reaching faculty review.

## Requirement → code → test

| # | Requirement | Code | Test |
|---|---|---|---|
| 9.1 | Typed evidence model (candidate/output id, source id + type, course/unit scope, version/hash, locator, claim, excerpt, verdict + rejection reason) | `src/course_organizer/evidence.py`: `SourceIndex`, `ClaimEvidence`, `EvidenceItem` (+`ItemVerdict`), `CandidateEvidence` | `tests/test_lab09_evidence.py`: valid table incl. version + hash |
| 9.2 | Fixed locator vocabulary resolving to real records | `parse_locator`: `unit:<id>`, `note:<id>:version:<v>`, `rubric:<aid>:criterion:<i>`, `submission:<id>`; anything else is malformed | locator parse tests + malformed battery (incl. `similarity:0.9`) |
| 9.3 | Authorization, course/unit scope, identity, version verified; nothing invented | `build_evidence_table`: resolve → course check → unit-scope check → currency check → authorization → excerpt containment | missing / wrong-scope / narrowing / stale / inaccessible / invalid-reference tests |
| 9.4 | Similarity alone is not proof | No `similarity` source kind exists; score-flavoured excerpts match no record | `test_similarity_alone_never_grounds_a_claim` |
| 9.5 | Missing/invalid/inaccessible/out-of-scope evidence rejected; fail closed | Every failure mode yields `unsupported` + named reason; empty claims → missing-evidence item; garbage tables rejected in the check | all negative tests + `test_check_rejects_garbage_tables_without_raising` |
| 9.6 | Structured evidence table per candidate | `CandidateEvidence` (supported iff every item is); JSON-clean dumps ride `CourseState.evidence_tables` | aggregation test; checkpoint round-trip test |
| 9.7 | Evidence available to validation and faculty review | 5th check `evidence_grounded` (`validation.py` + `DOC_CHECKS`); tables in state; report detail + audit entry reach the gate via `GateRequest.validation` (schema untouched) | `test_evidence_grounded_is_the_fifth_doc_check`, `test_validate_*`, runner `checks_run == 5` |
| 9.8 | Integrated into the real graph path, not an isolated library | `validate` node builds tables from tool-verified state via `build_grade_evidence` (`nodes.py`); `CourseState.evidence_tables` declared (LangGraph drops undeclared keys) | Lab 7 runner suite (133 passed incl. real clean-run validation); Lab 7 CLI 18/18; Lab 8 CLI 27/27 |
| 9.9 | Student-private data never leaks into unrelated evidence | Stranger-student resolution returns `inaccessible` with empty excerpt and no owner text | `test_stranger_student_is_denied_with_no_leaked_excerpt` |
| 9.10 | Malformed evidence + reader-failure equivalents tested | Malformed locators are verdicts; empty index resolves nothing without raising | malformed + empty-index tests |
| 9.11 | Lab 4 retrieval and Lab 5 connector untouched | `evidence.py` does no IO; callers inject `SourceIndex` built from connector/tool reads | Lab 4 + Lab 5 suites green in full run |

## Design notes (honest boundaries)

- **Note bodies in graph runs:** state carries note *counts*, not texts, so the graph path emits submission + rubric + unit claims; a `note:`-locator claim in a graph run fails closed. Note locators are fully implemented and tested with synthetic bodies. No note text is fabricated to fill the gap.
- **Syllabus citations:** `unit:` locators are the syllabus-backed facts — the units come from the scope tool's syllabus check, and the approved vocabulary maps `syllabus` to that form.
- **Rubric indices are positional** into the assignment's approved criterion list (grade `criterion_marks` keys are criterion ids; locators use positions so any rubric shape works).
- **Empty unit scope = no unit constraint** (some runs lock no units); a non-empty scope narrows strictly.
- **5th check, not 4th rewritten:** doc 2.7's four checks are byte-identical in behavior; grounding appends. Three existing tests were updated to the 5-check contract (`len(DOC_CHECKS)`, passing-report evidence, runner count) — intent preserved, not weakened.
- **Faculty review sees evidence** through the validation report detail (which names ungrounded submissions + reasons) and the state tables. `GateRequest` schema was deliberately not widened (`extra="forbid"` + schema tests).
