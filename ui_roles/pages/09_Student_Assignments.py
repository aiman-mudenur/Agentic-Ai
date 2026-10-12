import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list, status_badge

st.title("📝 My Assignments")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_assign")

items = safe_list(requests.get(f"{API}/assignments/", headers=h, timeout=10))
mine = safe_list(requests.get(f"{API}/submissions/", headers=h, timeout=10))
submitted_for = {s.get("assignment_id") for s in mine}

if not items:
    st.info("No assignments published yet.")
for a in items:
    aid = a["id"]
    done = aid in submitted_for
    badge = "✅ Submitted" if done else "🆕 New"
    with st.expander(f"{badge} · #{aid} {a.get('title')} · {a.get('max_marks')} marks · Due {a.get('due_date') or '—'}", expanded=not done):
        st.write(f"**Course:** {a.get('course_code')} · **Unit:** {a.get('unit_number') or '-'}")
        if a.get("description"):
            st.write(a["description"])
        if a.get("file_name"):
            download_button("⬇ Open / download brief", "assignments", aid, a.get("file_name"), f"sasg_{aid}")
        else:
            st.caption("No brief file attached.")
