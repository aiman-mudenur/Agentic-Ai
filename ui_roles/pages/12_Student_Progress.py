import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, live_refresh, safe_list

st.title("📊 My Progress (live)")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_prog")
if st.button("↻ Refresh now", key="stu_prog_r"):
    st.rerun()

prog = safe_list(requests.get(f"{API}/student/progress", headers=h, timeout=15))
subs = safe_list(requests.get(f"{API}/submissions/", headers=h, timeout=10))
asgs = safe_list(requests.get(f"{API}/assignments/", headers=h, timeout=10))
titles = {a["id"]: a.get("title", f"#{a['id']}") for a in asgs}

overall = next((p for p in prog if not p.get("unit_number") and not p.get("assignment_id")), None)
if overall:
    c1, c2, c3 = st.columns(3)
    c1.metric("Completion", f"{overall.get('completion_pct', 0)}%")
    c2.metric("Status", overall.get("status", "-"))
    c3.metric("Marks", f"{overall.get('marks_obtained') or '—'}/{overall.get('max_marks') or '—'}")
    st.progress(min(max(float(overall.get("completion_pct", 0)) / 100.0, 0.0), 1.0))
else:
    st.info("No progress yet. Submit an assignment first — graphs appear here live after each grading.")

graded = [s for s in subs if s.get("status") == "graded" and s.get("awarded_marks") is not None]
if graded:
    st.subheader("Marks per assignment")
    st.bar_chart({titles.get(s["assignment_id"], f"#{s['assignment_id']}"): float(s["awarded_marks"]) for s in graded})
    for s in graded[::-1][:10]:
        st.success(f"✅ {titles.get(s['assignment_id'], s['assignment_id'])}: {s['awarded_marks']}/{s.get('max_marks') or '?'} — {s.get('feedback') or 'no feedback'}")

units = {}
for p in prog:
    if p.get("unit_number"):
        units[p["unit_number"]] = p
if units:
    st.subheader("Courses / units undertaken")
    st.dataframe([{"unit": u, "status": v.get("status"), "completion %": v.get("completion_pct"), "marks": v.get("marks_obtained")} for u, v in sorted(units.items())], width="stretch")

st.caption("Updates live: every submit/grade recomputes progress. Toggle Live refresh above for auto-updates.")
