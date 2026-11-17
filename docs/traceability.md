# Traceability Matrix — Identity, Persistence & Role Dashboards

This matrix maps the requirements from the problem statement to the implementation.

| Req ID | Requirement | Implementation | Test |
|--------|-------------|----------------|------|
| **R1** | Users table with id, name, role (student/faculty/hod), salted password hash | `models/auth.py` User model with `role` enum, `hashed_password` (pbkdf2_sha256) | `test_isolation.py` TEST 1-4 |
| **R2** | Seed synthetic demo accounts: 1 HoD, 2 faculty, 5+ students across 2 courses | `scripts/seed_demo.py` — idempotent, explicit command only | Manual: `python -m scripts.seed_demo` |
| **R3** | Seeding never runs automatically on login or app start | `db/init_db.py` only creates tables; seeding is separate | Verified: no seed call in `api/main.py` startup |
| **R4** | All records in SQLite: courses, enrollments, assignments (with faculty creator), submissions (student ID + write-once timestamp), draft/approved grades, note versions, milestones, audit log | `models/` — `Course`, `Enrollment`, `Assignment`, `Submission`, `Note` (versioned), `AuditLog` | `test_isolation.py` — data persists across logins |
| **R5** | Per-user scoping enforced in connector/data layer (Lab 5), not only UI | All API routers filter by `user.id` + `role` + `Enrollment` | `test_isolation.py` TEST 1-4 — 403 on cross-access |
| **R6** | Student token requesting another student's records → 403 | `api/routers/submissions.py` `list_submissions` filters by `student_id`; `download_submission` checks ownership | `test_isolation.py` TEST 1 — 403 on download |
| **R7** | Student dashboard: enrolled courses, assignments/deadlines, own submissions with on-time/late, milestone status, notes, APPROVED grades only | `api/routers/student.py` `/dashboard`, `/assignments`, `/progress`, `/notes` | `test_flow.py` — student sees only approved grades |
| **R8** | Draft grades and similarity flags never shown to students | `api/routers/submissions.py` `_submission_to_dict` filters `awarded_marks`, `similarity_score`, `similarity_flagged` for `user_role == "student"` | `test_isolation.py` TEST 3 — `awarded_marks=None`, `similarity_score=None` |
| **R9** | Faculty dashboard: own courses, ALL assignments created, submission dashboard per assignment (on-time/late, similarity flags), draft grades queue with approve/edit/override, notes portal with version history, audit view of grades published | `api/routers/faculty.py` `/dashboard`, `/submissions` | `test_flow.py` — faculty sees draft_graded, can approve |
| **R10** | HoD dashboard: read-only overview across courses (submission/approval counts, late rates, audit summary) — NO approve/edit actions | `api/routers/hod.py` `/dashboard`, `/audit-log`, `/submissions/pending-approval`, `/submissions/{id}/approve` | `test_isolation.py` TEST 4 — HoD gets 403 on grade, 200 on approve |
| **R11** | Write-once submission timestamps | `models/submission.py` `submitted_at` set once; `submit_assignment` checks for existing submission (409 if duplicate) | `test_isolation.py` — duplicate submit returns 409 |
| **R12** | Note versions never deleted | `models/course.py` `Note` has `version`, `parent_note_id`, `is_current`; upload creates new version, marks old `is_current=False` | `api/routers/notes.py` `/upload` creates version chain |
| **R13** | Nothing publishes without faculty approval | `Submission.status` = `draft_graded` → `graded` (after approve); student sees only `graded` + `approved_at` | `test_flow.py` — student sees `None` until approve |
| **R14** | Audit entry for every grade published and who approved | `models/auth.py` `AuditLog`; entries on `grade_entered`, `grade_approved`, `auto_draft_grade_entered`, `submission_created` | `test_isolation.py` — audit entries created |
| **R15** | Student A cannot see Student B's data (UI path and direct connector call) | API-level scoping + UI filters | `test_isolation.py` TEST 1 |
| **R16** | Faculty cannot see another faculty's assignments | `api/routers/assignments.py` filters by `created_by` | `test_isolation.py` TEST 2 |
| **R17** | Assignments created before logout visible after login again | SQLite persistence; `Assignment` has `created_by`, `course_code` | `test_flow.py` — assignments persist |
| **R18** | Student never sees draft grades | `_submission_to_dict` returns `awarded_marks=None` for `status != "graded" or not approved_at` | `test_isolation.py` TEST 3 |
| **R19** | HoD cannot modify anything | HoD gets 403 on `/grade` endpoint; only `/approve` allowed | `test_isolation.py` TEST 4 |
| **R17** | Three separate dashboards selected by role after login | `ui_roles/pages/01_Faculty_Dashboard.py`, `08_Student_Dashboard.py`, `14_HoD_Dashboard.py` | Manual verification |

---

## Test Coverage Summary

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_isolation.py` | 4 test groups | R5, R6, R7, R8, R9, R10, R11, R13, R14, R15, R16, R18, R19 |
| `test_flow.py` | 1 integration test | R4, R7, R8, R12, R13 |
| `test_lab08_swarm.py` | 104 tests | Lab 8 swarm |
| All lab tests | 785 passed | Labs 1-8 core functionality |

Run all isolation tests:
```bash
python scripts/test_isolation.py
```

Run full test suite:
```bash
python -m pytest -o addopts= -q
```

---

## Round 2 — Realtime Flow, Open Enrollment, Google Integrations (R20–R27)

| Req ID | Requirement | Implementation | Test |
|--------|-------------|----------------|------|
| **R20** | Faculty creates courses/assignments/notes with no restrictions | `POST /courses/` (auto-creates rows); create-assignment/upload-note no longer 403 on other courses; ownership kept via `created_by` for submission scoping | `test_enrollment_realtime.py::test_open_course_creation_and_self_enrollment` |
| **R21** | Students self-enroll in any number of catalog courses | `GET /courses/catalog`, `POST/DELETE /courses/{code}/enroll`, `GET /courses/my/enrollments`; signup `course_code` lookup fixed (by code, not PK); UI page `15_Course_Catalog.py` | same test (enroll, idempotent re-enroll, 404 on unknown) |
| **R22** | Submit is instantly visible to faculty for grading | Submit requires enrollment (403 otherwise, shown in UI); `/faculty/submissions` scoped by `created_by`; background `run_submit_pipeline` | `test_submit_visible_to_faculty_with_auto_draft` |
| **R23** | Lab agents orchestrated on submit: Lab 8 swarm + Lab 2 auto-grade draft + Lab 6 audit + progress | `services/swarm_trigger.py::run_submit_pipeline` (background thread, env-gated `AUTO_SWARM_ON_SUBMIT` / `AUTO_GRADE_ON_SUBMIT`); draft `graded_by=None`, faculty approves | same test (draft appears, approve publishes, student sees grade, no similarity leak) |
| **R24** | Realtime Gmail login (real Google accounts) | `services/google_oauth.py` + `api/routers/google.py`: consent URL → callback → one-time code → JWT exchange; find-or-create by verified email; UI `google_login_block` on all 3 dashboards; fails closed (503 + setup steps) when `credentials.json` absent | `test_google_endpoints_fail_closed_without_config` |
| **R25** | Faculty imports notes/assignments from Google Classroom | `services/classroom.py` on Lab 5 `RestGoogleWorkspaceTransport` (injected per-user service); `api/routers/classroom.py`: status/connect-url/courses/work/import (coursework→assignments with `maxPoints`/due dates, materials→notes, provenance in audit log); tokens in `data/tokens/` (gitignored); UI page `06_Classroom_Import.py` | Manual (needs live Google consent); transport mock-covered by Lab 5 suite |
| **R26** | Line graphs in student progress | `12_Student_Progress.py`: approved marks vs submission date `st.line_chart` appended; existing layout untouched | test asserts `submitted_at` present on student payload |
| **R27** | Fresh DB after identity-schema change | `scripts/reset_db.py` (delete → recreate → `seed_demo`); `.env.example` documents `GOOGLE_REDIRECT_URI`, `AUTO_*` flags | Manual: `python -m scripts.reset_db` |
| **R28** | Approved grade must reach the student graph (bug: `approved_at` never exposed) | `_submission_to_dict` student branch now includes `approved_at` and shows marks for any `graded` row (status only set by approve path) | verify script: student sees `graded 40.6 approved_at=...`, UI filter passes |
| **R29** | Role-wise Login page (Student/Faculty/HoD/Admin) routing to role dashboards | `ui_roles/pages/Login.py` (4 tabs, `st.switch_page` per role); per-page guards remain so cross-role URLs show warnings, not data | Manual: login per tab, check landing + sidebar |
| **R30** | Auth control top-right on every page | `ui_helpers.topbar()` (identity + Logout, else Login → Login page); called first in all 14 pages + Home | Manual: check corner on each page |
| **R31** | Gmail verification code emailed, not only on-screen | `services/mailer.py` (stdlib SMTP); callback emails code when `SMTP_*` set, else on-screen fallback; `SMTP_*` keys in `.env.example` | Manual: needs Gmail App Password (see README §7) |
| **R32** | No dummy/test credentials anywhere | Prefills cleared (01/08/14), demo block removed (Home), `13_Admin_Users.py` deleted, `seed_demo.py` seeds catalog only | `grep` for old demo emails in UI returns nothing |
| **R33** | Public catalog + HoD stats resilient + admin role works | `GET /courses/catalog` needs no login; HoD page shows 403/404 with fix hints; `hod` endpoints + signup/Google accept `admin` | `test_catalog_public_and_hod_stats_with_admin_role` |
| **R34** | Post-login hub: success msg + role feature list + chosen redirect | `ui_helpers.ROLE_FEATURES` + `feature_hub()`; `Login.py` shows hub after any login; top-right `topbar()` (identity/Logout/Login) on all 14 pages + Home | Manual: login per tab, pick features |
| **R35** | Role-independent Login/Catalog, feature-locked pages | Catalog browses logged-out (read-only; actions need login); every data page keeps role guards (warning, never data) | Manual: open Catalog logged out; wrong-role page shows warning |
| **R36** | Developer proof that lab agents run behind the UI | `run_submit_pipeline` stamps `submission.run_id` + `swarm_completed` audit; `GET /dev/activity` (per-submission trace + agent audit) and `/dev/runs` (graph artifacts); UI page `07_Agent_Activity.py` with action→labs→evidence map (in hub for faculty/hod/admin) | `test_submit_pipeline_records_swarm_evidence_and_dev_activity` |
| **R37** | Agent repository in the app: agent/tool/skill counts + LangChain/LangGraph map | `services/agent_inventory.py` (counts computed live: 6 specialists + 4 lab agents, 4 tools via registries, 2 skill specs, `importlib.metadata` versions); `GET /dev/inventory`; repository section on Agent Activity page | inventory assertions inside `test_submit_pipeline_records_swarm_evidence_and_dev_activity` |
| **R38** | Classroom detail reads must use discovery parameter names (`courses.get` takes `id`) | `RestGoogleWorkspaceTransport.course()` calls `.get(id=…)`; per-part warnings preserved so one refusal can't hide the rest | `test_classroom_transport_uses_discovery_param_names` |
| **R39** | Materials visible even when coursework/topics are refused; student-side coursework + topics + announcements scopes | `get_work` fetches announcements too; portal scopes add `coursework.me.readonly`, `topics.readonly`, `announcements.readonly` (Lab 5 list untouched); `/status` reports missing scopes; `POST /disconnect` for clean reconnect; announcements import as notes | scope assertions in `test_classroom_transport_uses_discovery_param_names` |
| **R40** | Response keys must match discovery (`courseWorkMaterial`, `topic` singular) | Fixed `_paged` keys in `course_materials`/`topics`; stub test now uses discovery-shaped payloads so a wrong key fails | extended `test_classroom_transport_uses_discovery_param_names` |
| **R41** | Imported notes carry real file bytes, not 0-byte placeholders | `download_drive_file` (Drive `get_media`, native Docs/Sheets/Slides exported to PDF) + `save_bytes`; one note per attached file (`title — filename`, real size); per-file failures reported, import continues; preview shows attachment counts; course code prefilled from Classroom name | `test_classroom_drive_file_parsing_and_download` |
| **R42** | Notes findable: no free-text course codes; link-less imports still openable | Course **dropdowns** (catalog / enrolled) on both Notes pages; `Note.source_url` stored on every import + "Open source" button; `_normalize_course_code` turns pasted `CS_AI2027 (891792891951)` into `CS_AI2027` | `test_import_normalizes_code_and_stores_source_url` |
| **R43** | Upgrades must not 500 on old database files | `init_db` now auto-migrates: `ADD COLUMN` for any model field missing from an existing table + backfill of note version defaults; data preserved, no reset needed | `test_init_migrates_legacy_db_missing_columns` |
| **R44** | Pre-link imports regain an openable Classroom URL without re-importing | Startup backfill copies the provenance URL from each note's audit entry onto `source_url`; both Notes pages already render the source button | `test_backfill_recovers_note_links_from_audit_log` |

## Round 5 — Lab 11 Tested Build Factory (R47)

| Req ID | Requirement | Implementation | Test |
|--------|-------------|----------------|------|
| **R47** | Reproducible build entry on the existing graph with typed verdicts + hard gate | `src/course_organizer/build.py` (`BuildRequest/Result`, `AcceptanceReport`, input verification, plan-identity diff, fail-closed `evaluate_acceptance`, no resume path) + `scripts/run_build.py` CLI (exit 0 ready / 1 otherwise) | `tests/test_lab11_build.py` (23 tests); Lab 7 CLI 18/18 exit 0 |
| **R48** | Supplied locked plan governs execution (handoff, adoption, scope enforcement) | `run_pipeline(authorized_plan=…)` seed; `CourseState.authorized_plan`; plan-node adopt (re-verify + narrow scope, pre-workers); distribution/collection guards; scope-equality acceptance | Governed-scope, rejection (unit/assignment/facts/tamper/course/authority), evidence-scoping, bypass tests in `test_lab11_build.py` |

## Round 3 — Lab 9 Grounding and Evidence (R45)

| Req ID | Requirement | Implementation | Test |
|--------|-------------|----------------|------|
| **R45** | Typed evidence model + 5th validation check + graph integration | `src/course_organizer/evidence.py` (vocabulary, `SourceIndex`, verdicts, grade builder); `DOC_CHECKS` + `CheckContext` + `CHECKS` extended; `CourseState.evidence_tables` declared; `validate` node builds tables from tool-verified state | `tests/test_lab09_evidence.py` (24 tests); Lab 7/8 suites + CLIs green |

## Round 4 — Lab 10 Self-Healing Validation (R46)

| Req ID | Requirement | Implementation | Test |
|--------|-------------|----------------|------|
| **R46** | Typed findings + targeted repair + revalidation + budgeted escalation | `src/course_organizer/graph/repair.py` (`Finding`, `diagnose`, `sync_grade_metadata`, `splice_grades`, `steps_for`); workers retry branch (deterministic sync + subset regen + splice); `next_route(unrepairable=…)`; `repair_findings` channel; `reason="unrepairable"` | `tests/test_lab10_self_heal.py` (16 tests); Lab 7 CLI 18/18 exit 0 |