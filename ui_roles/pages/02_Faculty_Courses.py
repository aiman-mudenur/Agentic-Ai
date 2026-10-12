import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, live_refresh, safe_list

st.title("📖 Courses / Units")

if st.session_state.get("role") not in ("faculty", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_courses")
try:
    courses = safe_list(requests.get(f"{API}/courses/", headers=h, timeout=10))
    if courses:
        st.dataframe([{"code": c.get("course_code"), "title": c.get("title")} for c in courses])
    else:
        st.info("No courses yet.")
except Exception as e:
    st.error(str(e)[:500])
cc = st.text_input("Course code", "CS201")
try:
    units = safe_list(requests.get(f"{API}/courses/{cc}/units", headers=h, timeout=10))
    if units:
        for u in units:
            with st.expander(f"Unit {u.get('unit_number')}: {u.get('title')}", expanded=False):
                st.write(u.get("description") or "No description")
    else:
        st.info("No units yet for this course.")
except Exception as e:
    st.error(str(e)[:500])
