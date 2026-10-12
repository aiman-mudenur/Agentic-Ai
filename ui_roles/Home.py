import streamlit as st
import requests

st.set_page_config(page_title="Course Organizer", layout="wide", page_icon="🎓")
st.title("🎓 Course Organizer")
st.caption("Faculty / Student portal — real email login, files, auto-grading, live progress.")

col1, col2 = st.columns(2)
with col1:
    st.subheader("👩‍🏫 Faculty")
    st.markdown("Upload **notes**, create **assignments**, see **live submission counts**, **auto-grade with the agent**, track class progress.")
    st.markdown("Start here: **Faculty Dashboard** (login or sign up with your real email).")
with col2:
    st.subheader("🧑‍🎓 Student")
    st.markdown("View **assignments**, **download notes**, **submit files**, watch your **progress graphs update live** after each grading.")
    st.markdown("Start here: **Student Dashboard** (login or sign up with your real email).")

st.divider()
st.markdown("**Flow:** Faculty creates assignment → Student submits (swarm organises in backend automatically) → Faculty grades manually or via **Auto-grade agent** → Both sides see **live updates + graphs**.")
st.success("Demo logins: faculty@gmail.com / faculty123 · student@gmail.com / student123 · admin@gmail.com / admin123")

try:
    r = requests.get("http://127.0.0.1:8000/health", timeout=5)
    if r.ok:
        st.success("API is reachable at http://127.0.0.1:8000")
    else:
        st.error("API reachable but unhealthy.")
except Exception as e:
    st.error(f"API not reachable. Start Terminal 1 first. ({str(e)[:200]})")
