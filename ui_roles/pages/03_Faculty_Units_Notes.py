import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, metric_card

st.title("Notes and Materials")
st.caption("Upload, version, and browse course notes and materials.")

if st.session_state.get("role") != "faculty" or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_notes")

try:
    cat = requests.get(f"{API}/courses/catalog", headers=h, timeout=10)
    codes = [c["course_code"] for c in cat.json()] if cat.ok else []
except Exception:
    codes = []
if not codes:
    st.warning("No courses in catalog yet. Create one from Course Catalog, then upload notes.")
    st.stop()

st.subheader("Course and unit")
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
    empty_state("No notes yet for this course. Upload the first one below, or import from Classroom.", title="Notes")
elif notes:
    metric_card("Notes", len(notes))
    for n in notes:
        current_label = "Current" if n.get("is_current") else "Old"
        title = n.get("title") or f"Note #{n.get('id')}"
        version = n.get("version", 1)
        file_name = n.get("file_name") or "no file"
        with st.expander(f"{title} v{version} — {file_name} ({current_label})", expanded=False):
            st.caption(f"Course {n.get('course_code')} · Unit {n.get('unit_number') or '-'} · v{version} · {n.get('size_bytes', 0)} bytes")
            if n.get("file_name"):
                download_button("Open / download", "notes", n["id"], n.get("file_name"), f"note_{n['id']}")
            elif n.get("source_url"):
                st.link_button("Open source (Classroom)", n["source_url"], key=f"nsrc_{n['id']}")
            else:
                st.caption("No file attached.")
            if n.get("is_current") and n.get("version", 1) > 1:
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

st.divider()
st.subheader("Upload / Update Note")
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
                if fr.ok:
                    st.success("Uploaded.")
                    if fr.json().get("version", 1) > 1:
                        st.info("Updated existing note — new version created")
                else:
                    st.error(f"{fr.status_code}: {fr.text[:300]}")
            except Exception as ex:
                st.error(str(ex)[:500])
