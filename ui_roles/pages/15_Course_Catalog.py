import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, safe_list

st.title("Course Catalog")
st.caption("Open catalog — visible to everyone. Login to register, create, or manage.")

token = st.session_state.get("token")
h = {"Authorization": f"Bearer {token}"} if token else {}
role = st.session_state.get("role")
live_refresh("catalog")

try:
    r = requests.get(f"{API}/courses/catalog", headers=h, timeout=10)
    catalog = r.json() if r.ok else []
    if not r.ok:
        st.error(f"Catalog unavailable ({r.status_code}). Is the API running with the latest code?")
except Exception as e:
    st.error(str(e)[:300])
    catalog = []

if role == "student" and token:
    st.subheader("Register for courses (any number, per your semester syllabus)")
    mine = safe_list(requests.get(f"{API}/courses/my/enrollments", headers=h, timeout=10))
    enrolled = {e["course_code"] for e in mine}
    if enrolled:
        st.success(f"Enrolled in: {', '.join(sorted(enrolled))}")
    if not catalog:
        empty_state("No courses listed yet. Ask faculty to create some.", title="Course Catalog")
    for c in catalog:
        code = c["course_code"]
        is_enrolled = bool(c.get("enrolled")) or code in enrolled
        label = f"{code} — {c.get('title')}" + (" (enrolled)" if is_enrolled else "")
        with st.expander(label, expanded=False):
            if c.get("description"):
                st.write(c["description"])
            if is_enrolled:
                if st.button(f"Unenroll from {code}", key=f"unen_{code}"):
                    r = requests.delete(f"{API}/courses/{code}/enroll", headers=h, timeout=10)
                    if r.ok:
                        st.success(f"Unenrolled from {code}")
                        st.rerun()
                    else:
                        st.error(f"{r.status_code}: {r.text[:300]}")
            else:
                if st.button(f"Register for {code}", key=f"en_{code}"):
                    r = requests.post(f"{API}/courses/{code}/enroll", headers=h, timeout=10)
                    if r.ok:
                        st.success(f"Registered for {code} — assignments for it appear now")
                        st.rerun()
                    else:
                        st.error(f"{r.status_code}: {r.text[:300]}")

elif role == "faculty" and token:
    st.subheader("Create a course")
    with st.form("new_course"):
        code = st.text_input("Course code (e.g. CS301)")
        title = st.text_input("Title")
        desc = st.text_area("Description (optional)")
        units_raw = st.text_area("Units, one per line as NUMBER|Title (optional)", placeholder="1|Arrays\n2|Trees")
        if st.form_submit_button("Create course"):
            if not code.strip() or not title.strip():
                st.error("Course code and title are required.")
            else:
                units = []
                for line in (units_raw or "").splitlines():
                    if "|" in line:
                        n, t = line.split("|", 1)
                        if n.strip() and t.strip():
                            units.append({"unit_number": n.strip(), "title": t.strip()})
                r = requests.post(f"{API}/courses/", headers=h,
                                  json={"course_code": code.strip(), "title": title.strip(),
                                        "description": desc.strip() or None, "units": units}, timeout=15)
                if r.ok:
                    st.success(f"Course {code.strip()} created.")
                    st.rerun()
                else:
                    st.error(f"{r.status_code}: {r.text[:300]}")
    st.divider()
    st.subheader("All courses in catalog")
    if catalog:
        st.dataframe([{"code": c["course_code"], "title": c.get("title")} for c in catalog], use_container_width=True)
    else:
        empty_state("No courses yet.", title="Course Catalog")

else:
    st.subheader("All courses (read-only)")
    if catalog:
        st.dataframe([{"code": c["course_code"], "title": c.get("title"),
                       "description": (c.get("description") or "")[:120]} for c in catalog],
                      use_container_width=True)
    else:
        empty_state("No courses yet.", title="Course Catalog")
    if not token:
        st.warning("Login from the Login page to register (students) or create (faculty).")
