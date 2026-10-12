import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list, status_badge

st.title("📤 My Submissions")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_subs")

items = safe_list(requests.get(f"{API}/submissions/", headers=h, timeout=10))
if not items:
    st.info("No submissions yet. Submit your first one below.")
for s in items[::-1][:20]:
    with st.expander(f"{status_badge(s.get('status',''))} · Assignment #{s.get('assignment_id')} · {s.get('file_name')} · {s.get('awarded_marks') or '—'}/{s.get('max_marks') or '?'}", expanded=False):
        if s.get("feedback"):
            st.success(f"💬 Faculty feedback: {s['feedback']}")
        if s.get("is_late"):
            st.warning("Submitted late")
        download_button("⬇ Open / download my file", "submissions", s["id"], s.get("file_name"), f"mysub_{s['id']}")

st.divider()
st.subheader("➕ Submit Assignment")
asgs = safe_list(requests.get(f"{API}/assignments/", headers=h, timeout=10))
options = [a["id"] for a in asgs] if asgs else []
aid = st.selectbox("Assignment", options) if options else st.number_input("Assignment ID", 1, 100000, 1)
f = st.file_uploader("Your file")
if st.button("Submit"):
    if not f:
        st.error("Choose a file first.")
    else:
        try:
            fr = requests.post(f"{API}/submissions/submit", headers={"Authorization": h["Authorization"]}, data={"assignment_id": int(aid)}, files={"file": (f.name, f.getvalue())}, timeout=60)
            if fr.ok:
                st.success("Submitted ✅ — organiser/swarm ran in backend automatically. Progress updates after grading.")
                st.rerun()
            else:
                st.error(f"{fr.status_code}: {fr.text[:500]}")
        except Exception as ex:
            st.error(str(ex)[:500])
