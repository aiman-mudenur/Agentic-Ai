import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list

st.title("📝 Assignments")

if st.session_state.get("role") not in ("faculty", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_assign")

items = safe_list(requests.get(f"{API}/assignments/", headers=h, timeout=10))
subs = safe_list(requests.get(f"{API}/submissions/", headers=h, timeout=10))
counts: dict = {}
for s in subs:
    counts[s.get("assignment_id")] = counts.get(s.get("assignment_id"), 0) + 1

if not items:
    st.info("No assignments yet. Create one below.")
for a in items:
    aid = a["id"]
    with st.expander(f"📌 #{aid} {a.get('title')} · {a.get('max_marks')} marks · 📥 {counts.get(aid, 0)} submissions (live)", expanded=False):
        st.write(f"**Course:** {a.get('course_code')} · **Unit:** {a.get('unit_number') or '-'} · **Due:** {a.get('due_date') or '—'}")
        if a.get("description"):
            st.write(a["description"])
        if a.get("rubric_text"):
            st.caption(f"Rubric: {a['rubric_text'][:300]}")
        if a.get("file_name"):
            download_button("⬇ Open / download brief", "assignments", aid, a.get("file_name"), f"asg_{aid}")

st.divider()
st.subheader("➕ Create Assignment")
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
                st.success("Created ✅") if fr.ok else st.error(f"{fr.status_code}: {fr.text[:500]}")
            except Exception as ex:
                st.error(str(ex)[:500])
