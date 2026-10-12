import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list

st.title("📚 Notes & Materials")

if st.session_state.get("role") not in ("faculty", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_notes")

cc = st.text_input("Course", "CS201")
un = st.text_input("Unit (optional)")
params = {"course_code": cc}
if un.strip():
    params["unit_number"] = un.strip()
notes = safe_list(requests.get(f"{API}/notes/", headers=h, params=params, timeout=10))
if not notes:
    st.info("No notes yet. Upload the first one below.")
for n in notes:
    with st.expander(f"📄 {n.get('title')}  ·  {n.get('file_name') or 'no file'}", expanded=False):
        st.caption(f"Course {n.get('course_code')} · Unit {n.get('unit_number') or '-'} · {n.get('size_bytes', 0)} bytes")
        if n.get("file_name"):
            download_button("⬇ Open / download file", "notes", n["id"], n.get("file_name"), f"note_{n['id']}")

st.divider()
st.subheader("⬆ Upload Note")
with st.form("n"):
    t = st.text_input("Title")
    f = st.file_uploader("File")
    if st.form_submit_button("Upload"):
        if not f:
            st.error("Choose a file first.")
        elif not t.strip():
            st.error("Enter a title.")
        else:
            try:
                data = {"course_code": cc, "title": t.strip()}
                if un.strip():
                    data["unit_number"] = un.strip()
                fr = requests.post(f"{API}/notes/upload", headers={"Authorization": h["Authorization"]}, data=data, files={"file": (f.name, f.getvalue())}, timeout=30)
                st.success("Uploaded ✅") if fr.ok else st.error(f"{fr.status_code}: {fr.text[:500]}")
            except Exception as ex:
                st.error(str(ex)[:500])
