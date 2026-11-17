import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, status_pill

st.title("My Assignments")
st.caption("Published assignments for your enrolled courses, with submission and grade status.")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_assign")

try:
    r = requests.get(f"{API}/student/assignments", headers=h, timeout=10)
    if r.ok:
        items = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        items = None
except Exception as e:
    st.error(str(e)[:300])
    items = None

if items is not None and not items:
    empty_state("No assignments published yet.", title="My Assignments")
elif items:
    for a in items:
        aid = a["id"]
        submitted = bool(a.get("submitted"))
        late = " — Late" if a.get("is_late") else ""
        grade = f" — Grade: {a.get('awarded_marks')}/{a.get('max_marks', '?')}" if a.get("awarded_marks") is not None else ""
        status_label = "Submitted" if submitted else "New"
        with st.expander(f"{status_label} · #{aid} {a.get('title')} · {a.get('max_marks')} marks · Due {a.get('due_date') or '—'}{late}{grade}", expanded=not submitted):
            cols = st.columns([1, 4])
            with cols[0]:
                status_pill(a.get("status", "not submitted" if not submitted else "submitted"))
            with cols[1]:
                st.write(f"**Course:** {a.get('course_code')} · **Unit:** {a.get('unit_number') or '-'}")
            if a.get("description"):
                st.write(a["description"])
            if a.get("rubric_text"):
                st.caption(f"Rubric: {a['rubric_text'][:300]}")
            if a.get("file_name"):
                download_button("Open / download brief", "assignments", aid, a.get("file_name"), f"sasg_{aid}")
            else:
                st.caption("No brief file attached.")
            if submitted:
                st.success(f"Submitted (ID: {a.get('submission_id')})")
                if a.get("awarded_marks") is not None:
                    st.info(f"Grade: {a.get('awarded_marks')}/{a.get('max_marks', '?')}")
