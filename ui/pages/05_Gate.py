import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph import DECISION_APPROVE, DECISION_EDIT, DECISION_OVERRIDE, DECISION_REFUSE
from course_organizer.graph import resume_run
from course_organizer.graph.swarm import resume_swarm

st.title("Gate / Resume")
col1, col2 = st.columns(2)
with col1:
    kind = st.radio("System", ["lab07", "lab08"], index=0)
with col2:
    run_id = st.text_input("Run ID")

dec = st.radio("Decision", ["approve", "edit", "override", "refuse"], index=0)
MAP = dict(approve=DECISION_APPROVE, edit=DECISION_EDIT, override=DECISION_OVERRIDE, refuse=DECISION_REFUSE)

if st.button("Resume", type="primary"):
    root = REPO / "out" / kind
    if kind == "lab07":
        run = resume_run(run_id=run_id, decision=MAP[dec], root=root)
    else:
        run = resume_swarm(run_id=run_id, decision=MAP[dec], root=root)
    st.success("Resumed")
    st.write(run.summary())
    if run.published:
        st.write("Published:", run.published)
