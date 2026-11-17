import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, metric_card

st.title("Agent Activity")
st.caption("Proof that the lab agents run behind the UI — read from the submission trace, audit log, and run artifacts. No extra tracking was added.")

if st.session_state.get("role") not in ("faculty", "hod", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty, HoD or Admin (Login page). Students see their own Progress page instead.")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("devact")
if st.button("Refresh now", key="devact_r"):
    st.rerun()

with st.expander("UI action -> lab agents -> evidence (how to read this page)", expanded=False):
    st.markdown("""
- **Student submits file** -> Submission Collection (timestamp/late) -> Similarity (score/flag) -> Grading Assistance (draft) -> Swarm best-of-N (Lab 8) -> audit write (Lab 6). Evidence: `swarm_run_id`, `draft_marks`, `similarity`; audit: `submission_created`, `swarm_completed`, `auto_draft_grade_entered`
- **Faculty grades / approves** -> Faculty Review gate -> publish + archive (Lab 7), audit (Lab 6). Evidence: `approved_by/at`; audit: `grade_approved`
- **Faculty uploads note** -> Notes Repository versioning. Evidence: Notes page versions; audit: `note_uploaded/updated`
- **Classroom import** -> Lab 5 connector reads (courses/coursework/materials). Evidence: Audit: `assignment_imported`, `note_imported`
- **Assignment created** -> Distribution (brief + rubric stored, schedule). Evidence: Assignments page; audit: `course_created`
- **Pipeline runs (API `/runs/*`)** -> Full Lab 7 / Lab 8 graphs. Evidence: Run artifacts table (out/lab07, out/lab08)
""")

st.subheader("Per-submission agent trace (latest first)")
activity_loaded = False
try:
    with st.spinner("Loading agent trace..."):
        r = requests.get(f"{API}/dev/activity", headers=h, timeout=15)
    if r.ok:
        data = r.json()
        activity_loaded = True
    else:
        st.error(f"{r.status_code}: {(r.text or '')[:300]}")
        data = {}
except Exception as e:
    st.error(str(e)[:300])
    data = {}

trace = (data or {}).get("trace", []) if activity_loaded else []
if activity_loaded and trace:
    st.dataframe(trace, use_container_width=True)
elif activity_loaded:
    empty_state("No submissions yet — submit one as a student and Refresh.", title="Per-submission agent trace")

st.subheader("Agent audit entries (Lab 6 trail)")
logs = (data or {}).get("agent_audit", []) if activity_loaded else []
if activity_loaded and logs:
    st.dataframe(logs, use_container_width=True)
elif activity_loaded:
    empty_state("No agent audit entries yet.", title="Agent audit entries")

st.subheader("Graph run artifacts (Labs 7/8)")
runs_loaded = False
try:
    with st.spinner("Loading run artifacts..."):
        rr = requests.get(f"{API}/dev/runs", headers=h, timeout=10)
    if rr.ok:
        runs = rr.json()
        runs_loaded = True
    else:
        st.error(f"{rr.status_code}: {(rr.text or '')[:300]}")
        runs = []
except Exception as e:
    st.error(str(e)[:300])
    runs = []

if runs_loaded and runs:
    st.dataframe(runs, use_container_width=True)
    st.caption("Each run dir holds the graph checkpoint + published/merged artifacts on disk under out/.")
elif runs_loaded:
    empty_state("No pipeline runs on disk yet (background submit-swarms and /runs/* executions appear here).", title="Graph run artifacts")

# Run summary metrics
if runs_loaded and runs:
    st.subheader("Run summary")
    total_runs = len(runs)
    awaiting = sum(1 for r in runs if r.get("awaiting_gate"))
    published = sum(1 for r in runs if r.get("status") == "published")
    failed = sum(1 for r in runs if r.get("status") in ("refused", "escalated"))
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Total runs", total_runs)
    with c2:
        metric_card("Awaiting gate", awaiting)
    with c3:
        metric_card("Published", published)
    with c4:
        metric_card("Failed", failed)

st.divider()
st.subheader("Agent repository (computed live from the running code)")
try:
    with st.spinner("Loading agent inventory..."):
        ir = requests.get(f"{API}/dev/inventory", headers=h, timeout=20)
    if ir.ok:
        inv = ir.json()
        inv_loaded = True
    else:
        st.error(f"{ir.status_code}: {(ir.text or '')[:300]}")
        inv = {}
        inv_loaded = False
except Exception as e:
    st.error(str(e)[:300])
    inv = {}
    inv_loaded = False

counts = (inv or {}).get("counts", {}) if inv_loaded else {}
if inv_loaded and counts:
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        metric_card("Agents", counts.get("agents_total", 0))
    with m2:
        metric_card("Tools", counts.get("tools", 0))
    with m3:
        metric_card("Skills", counts.get("skills", 0))
    with m4:
        metric_card("Swarm strategies", len(((inv.get("framework") or {}).get("swarm_merge_strategies") or [])))
    with st.expander("Specialist agents (Phase A, wired into Lab 7/8 graphs)", expanded=False):
        st.dataframe((inv or {}).get("specialist_agents", []), use_container_width=True)
    with st.expander("Lab agents (one per foundations lab)", expanded=False):
        st.dataframe((inv or {}).get("lab_agents", []), use_container_width=True)
    with st.expander("Tools (Lab 2 + Lab 4 registries, built live)", expanded=False):
        st.dataframe((inv or {}).get("tools", []), use_container_width=True)
    with st.expander("Skills (Lab 3 specs)", expanded=False):
        st.dataframe((inv or {}).get("skills", []), use_container_width=True)
    fw = (inv or {}).get("framework", {})
    st.subheader("How LangChain / LangGraph is implemented here")
    vers = fw.get("versions", {}) or {}
    c1, c2 = st.columns(2)
    with c1:
        st.write("**Installed:** " + ", ".join(
            f"{k}=={v}" if v else f"{k} (missing)" for k, v in vers.items()) + ".")
    with c2:
        st.write("**LLM backends:** " + "; ".join(fw.get("llm_backends", [])) + ".")
    st.dataframe(fw.get("uses", []), use_container_width=True)
    st.caption("Counts come from the running code (registries, module attributes, installed versions) — not hard-coded.")
elif inv_loaded:
    empty_state("Inventory unavailable.", title="Agent repository")

st.divider()
st.subheader("Coordinator oversight")
st.caption("Read-only summary of pipeline run health and audit trail. No consequential actions are performed here.")
if runs_loaded and runs:
    c1, c2 = st.columns(2)
    with c1:
        st.write("**Runs with specialist failure:** " + str(sum(1 for r in runs if r.get("specialist_ok") is False)))
    with c2:
        st.write("**Runs awaiting approval:** " + str(sum(1 for r in runs if r.get("awaiting_gate"))))
    st.caption("Plan locking and budgets are enforced by the governed runtime (Lab 6). Grades are published only through the faculty approval gate (Lab 7).")
else:
    empty_state("No run data available for oversight.", title="Coordinator oversight")

