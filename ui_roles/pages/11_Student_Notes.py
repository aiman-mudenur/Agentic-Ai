import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list

st.title("📚 Notes & Materials")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_notes")

cc = st.text_input("Course", "CS201")
un = st.text_input("Unit (optional)")
params = {"course_code": cc}
if un.strip():
    params["unit_number"] = un.strip()
notes = safe_list(requests.get(f"{API}/notes/", headers=h, params=params, timeout=10))
if not notes:
    st.info("No notes yet for this course.")
for n in notes:
    with st.expander(f"📄 {n.get('title')} · Unit {n.get('unit_number') or '-'} · {n.get('file_name') or 'no file'}", expanded=False):
        if n.get("file_name"):
            download_button("⬇ Open / download", "notes", n["id"], n.get("file_name"), f"snote_{n['id']}")
