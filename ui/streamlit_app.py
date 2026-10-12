# streamlit_app.py
from pathlib import Path
import sys
import streamlit as st

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

st.set_page_config(page_title="Course Organizer", layout="wide")
st.title("Course Organizer — End-to-End")
st.markdown("Run Lab 7 pipeline, Lab 8 swarm, approve gates, view published outputs and audit trails.")
st.caption(f"Repo: {REPO}")

st.subheader("Quick Links")
col1, col2, col3 = st.columns(3)
with col1:
    st.page_link("pages/01_Home.py", label="Home")
with col2:
    st.page_link("pages/03_Run_Pipeline.py", label="Run Pipeline (Lab 7)")
with col3:
    st.page_link("pages/04_Run_Swarm.py", label="Run Swarm (Lab 8)")
st.page_link("pages/05_Gate.py", label="Gate / Resume")
st.page_link("pages/06_Published.py", label="Published Documents")
st.page_link("pages/07_Audit.py", label="Audit & Timing")
st.page_link("pages/08_Evidence.py", label="Evidence Files")
