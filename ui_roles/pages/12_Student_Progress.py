import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, metric_card, safe_list

st.title("My Progress")
st.caption("Live progress, approved grades, and performance over time.")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_prog")
if st.button("Refresh now", key="stu_prog_r"):
    st.rerun()

try:
    r = requests.get(f"{API}/student/progress", headers=h, timeout=15)
    if r.ok:
        prog = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        prog = None
except Exception as e:
    st.error(str(e)[:300])
    prog = None

subs = safe_list(requests.get(f"{API}/submissions/", headers=h, timeout=10))
asgs = safe_list(requests.get(f"{API}/student/assignments", headers=h, timeout=10))
titles = {a["id"]: a.get("title", f"#{a['id']}") for a in asgs}

graded = [s for s in subs if s.get("status") == "graded" and s.get("approved_at") and s.get("awarded_marks") is not None]

if prog is not None and not prog:
    empty_state("No progress yet. Submit an assignment first — graphs appear here live after each grading.", title="Course Progress")
elif prog:
    st.subheader("Course Progress")
    for p in prog:
        if p.get("unit_number") is None and p.get("assignment_id") is None:
            c1, c2, c3 = st.columns(3)
            with c1:
                metric_card("Course", p.get("course_code"))
            with c2:
                metric_card("Completion", f"{p.get('completion_pct', 0)}%")
            with c3:
                metric_card("Marks", f"{p.get('marks_obtained') or '—'}/{p.get('max_marks') or '—'}")
            pct = p.get("completion_pct", 0)
            st.progress(min(max(float(pct) / 100.0, 0.0), 1.0))

    units = {}
    for p in prog:
        if p.get("unit_number") or p.get("assignment_id"):
            key = f"{p.get('course_code')}-{p.get('unit_number') or p.get('assignment_id')}"
            units[key] = p
    if units:
        st.subheader("Units and Assignments")
        st.dataframe([{
            "course": k.split("-")[0],
            "unit/assignment": k.split("-")[1],
            "status": v.get("status"),
            "completion %": v.get("completion_pct"),
            "marks": v.get("marks_obtained"),
            "max_marks": v.get("max_marks"),
        } for k, v in sorted(units.items())], use_container_width=True)
if graded:
    st.subheader("Marks per Assignment (Approved Only)")
    chart_data = {titles.get(s["assignment_id"], f"#{s['assignment_id']}"): float(s["awarded_marks"]) for s in graded}
    st.bar_chart(chart_data)
    for s in graded[::-1][:10]:
        st.success(f"{titles.get(s['assignment_id'], s['assignment_id'])}: {s['awarded_marks']}/{s.get('max_marks') or '?'} — {s.get('feedback') or 'no feedback'}")
else:
    empty_state("No approved grades yet — the chart appears after the first approval.", title="Marks per Assignment")

st.caption("Updates live: every submit/grade recomputes progress. Only approved grades shown.")

st.divider()
st.subheader("Performance over time (line)")
trend = [
    s for s in subs
    if s.get("submitted_at") and s.get("status") == "graded" and s.get("approved_at")
    and s.get("awarded_marks") is not None
]
trend = sorted(trend, key=lambda s: s["submitted_at"])
if trend:
    st.line_chart({s["submitted_at"][:10]: float(s["awarded_marks"]) for s in trend})
    st.caption("Approved marks in submission-date order — rises/falls show performance trend.")
else:
    empty_state("No approved grades yet — the line appears after the first approval.", title="Performance over time")
