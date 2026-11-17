import streamlit as st
import requests

st.set_page_config(page_title="Course Organizer", layout="wide", page_icon="CO")

from ui_helpers import topbar

topbar()

st.title("Course Organizer")
st.caption("Role-based portal — email login, persistent data, live dashboards.")

st.subheader("Role overview")
col1, col2, col3 = st.columns(3)
with col1:
    with st.container(border=True):
        st.markdown("**Faculty**")
        st.markdown("Create assignments, grade submissions, upload notes, track class progress.")
        st.markdown("Start here: **Faculty Dashboard**")
with col2:
    with st.container(border=True):
        st.markdown("**Student**")
        st.markdown("View enrolled courses, submit assignments, download notes, see approved grades.")
        st.markdown("Start here: **Student Dashboard**")
with col3:
    with st.container(border=True):
        st.markdown("**Head of Department**")
        st.markdown("Read-only overview: submission rates, late submissions, grade approvals, audit log.")
        st.markdown("Start here: **HoD Dashboard**")

st.divider()
st.subheader("Workflow")
st.markdown("**Flow:** Faculty creates assignment → Student submits (write-once) → Faculty grades (draft) → HoD/Faculty approves → Student sees published grade.")

st.info("Go to the **Login** page and choose your role: **Student**, **Faculty**, **HoD**, or **Admin**. Sign in with your password or with Google — after login you are taken to your role's dashboard.")

st.divider()
st.subheader("System status")
try:
    r = requests.get("http://127.0.0.1:8000/health", timeout=5)
    if r.ok:
        st.success("API is reachable at http://127.0.0.1:8000")
    else:
        st.error("API reachable but unhealthy.")
except Exception as e:
    st.error(f"API not reachable. Start Terminal 1 first. ({str(e)[:200]})")
