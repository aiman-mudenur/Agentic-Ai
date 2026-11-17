import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, metric_card

st.title("Assignments")
st.caption("Create assignments and review submissions.")

if st.session_state.get("role") != "faculty" or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_assign")

# Get faculty's own assignments
try:
    r = requests.get(f"{API}/assignments/", headers=h, timeout=10)
    if r.ok:
        items = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        items = None
except Exception as e:
    st.error(str(e)[:300])
    items = None

# Get submission counts for faculty's assignments
counts: dict = {}
try:
    sr = requests.get(f"{API}/faculty/submissions", headers=h, timeout=10)
    if sr.ok:
        for s in (sr.json() or []):
            aid = s.get("assignment_id")
            counts[aid] = counts.get(aid, 0) + 1
except Exception as e:
    st.error(str(e)[:300])

if items is not None and not items:
    empty_state("No assignments yet. Create one below.", title="Assignments")
elif items:
    metric_card("Assignments", len(items))
    for a in items:
        aid = a["id"]
        with st.expander(f"#{aid} {a.get('title')} — {a.get('max_marks')} marks — {counts.get(aid, 0)} submissions", expanded=False):
            st.write(f"**Course:** {a.get('course_code')} · **Unit:** {a.get('unit_number') or '-'} · **Due:** {a.get('due_date') or '—'}")
            if a.get("description"):
                st.write(a["description"])
            if a.get("rubric_text"):
                st.caption(f"Rubric: {a['rubric_text'][:300]}")
            if a.get("file_name"):
                download_button("Open / download brief", "assignments", aid, a.get("file_name"), f"asg_{aid}")
            else:
                st.caption("No brief file attached.")

st.divider()
st.subheader("Create Assignment")
with st.form("a"):
    cc = st.text_input("Course", "CS201")
    un = st.text_input("Unit")
    title = st.text_input("Title")
    desc = st.text_area("Description")
    mm = st.number_input("Max marks", 1.0, 100.0, 20.0)
    dd = st.text_input("Due date (YYYY-MM-DD, optional)")
    rb = st.text_area("Rubric (optional — used by auto-grade agent)")
    f = st.file_uploader("Brief file (optional)")
    if st.form_submit_button("Create"):
        if not title.strip():
            st.error("Enter a title.")
        else:
            try:
                fd = {"course_code": cc, "title": title.strip(), "max_marks": mm}
                if un.strip():
                    fd["unit_number"] = un.strip()
                if desc.strip():
                    fd["description"] = desc.strip()
                if dd.strip():
                    fd["due_date"] = dd.strip()
                if rb.strip():
                    fd["rubric_text"] = rb.strip()
                files = {"file": (f.name, f.getvalue())} if f else None
                fr = requests.post(f"{API}/assignments/", headers={"Authorization": h["Authorization"]}, data=fd, files=files, timeout=30)
                if fr.ok:
                    st.success("Created.")
                else:
                    st.error(f"{fr.status_code}: {fr.text[:500]}")
            except Exception as ex:
                st.error(str(ex)[:500])
