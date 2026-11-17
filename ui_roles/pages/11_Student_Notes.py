import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, safe_list

st.title("My Notes")
st.caption("Notes and materials for your enrolled courses.")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_notes")

mine = safe_list(requests.get(f"{API}/courses/my/enrollments", headers=h, timeout=10))
codes = [e["course_code"] for e in mine]
if not codes:
    st.warning("You are not enrolled in any course yet. Register from Course Catalog first.")
    st.stop()
cc = st.selectbox("Course", codes, index=0)
un = st.text_input("Unit (optional)")
params = {"course_code": cc}
if un.strip():
    params["unit_number"] = un.strip()

try:
    r = requests.get(f"{API}/notes/", headers=h, params=params, timeout=10)
    if r.ok:
        notes = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        notes = None
except Exception as e:
    st.error(str(e)[:300])
    notes = None

if notes is not None and not notes:
    empty_state("No notes yet for this course.", title="My Notes")
elif notes:
    for n in notes:
        title = n.get("title") or f"Note #{n.get('id')}"
        unit = n.get("unit_number") or "-"
        file_name = n.get("file_name") or "no file"
        uploaded = (n.get("uploaded_at") or "")[:19]
        meta = f"{n.get('course_code', '')} · Unit {unit} · {file_name}"
        if uploaded:
            meta += f" · {uploaded}"
        with st.expander(f"{title} — {meta}", expanded=False):
            if n.get("course_code"):
                st.caption(f"Course: {n['course_code']}")
            if n.get("uploaded_at"):
                st.caption(f"Uploaded: {n['uploaded_at'][:19]}")
            if n.get("size_bytes"):
                st.caption(f"Size: {n['size_bytes']} bytes")
            if n.get("file_name"):
                download_button("Open / download", "notes", n["id"], n.get("file_name"), f"snote_{n['id']}")
            elif n.get("source_url"):
                st.link_button("Open source (Classroom)", n["source_url"], key=f"snsrc_{n['id']}")
            else:
                st.caption("No file attached to this note.")
            if n.get("version", 1) > 1:
                if st.button("View versions", key=f"ver_{n['id']}"):
                    try:
                        vr = requests.get(f"{API}/notes/{n['id']}/versions", headers=h, timeout=10)
                        vers = vr.json() if vr.ok else []
                    except Exception:
                        vers = []
                    if vers:
                        for v in vers:
                            st.caption(f"v{v['version']} · {v['file_name']} · {str(v.get('uploaded_at', ''))[:19]} · {'Current' if v.get('is_current') else 'Old'}")
                    else:
                        st.caption("No versions available.")
