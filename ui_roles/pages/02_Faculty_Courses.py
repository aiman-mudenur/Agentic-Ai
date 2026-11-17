import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, metric_card

st.title("My Courses")
st.caption("Courses you teach and their units.")

if st.session_state.get("role") not in ("faculty", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_courses")

try:
    r = requests.get(f"{API}/courses/", headers=h, timeout=10)
    if r.ok:
        courses = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        courses = None
except Exception as e:
    st.error(str(e)[:300])
    courses = None

if courses is not None and not courses:
    empty_state("No courses yet.", title="My Courses")
elif courses:
    metric_card("Courses", len(courses))
    st.dataframe([{"code": c.get("course_code"), "title": c.get("title"), "description": (c.get("description") or "")[:120]} for c in courses], use_container_width=True)

st.divider()
st.subheader("Units for a course")
cc = st.text_input("Course code", "CS201")
try:
    r = requests.get(f"{API}/courses/{cc}/units", headers=h, timeout=10)
    if r.ok:
        units = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        units = None
except Exception as e:
    st.error(str(e)[:300])
    units = None

if units is not None and not units:
    empty_state(f"No units yet for {cc}.", title="Units")
elif units:
    for u in units:
        with st.expander(f"Unit {u.get('unit_number')}: {u.get('title')}", expanded=False):
            st.write(u.get("description") or "No description")
