import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph import run_pipeline

st.title("Run Pipeline (Lab 7)")
root = REPO / "out" / "lab07"
run_id = st.text_input("Run ID", value="ui-pipeline-1")

if st.button("Run pipeline", type="primary"):
    root.mkdir(parents=True, exist_ok=True)
    run = run_pipeline(
        course_code="CS201",
        run_id=run_id,
        root=root,
        output_dir=root / run_id,
    )
    st.success("Started")
    st.write(run.summary())
    st.write("nodes:", " → ".join(run.nodes_ran()))
    if run.awaiting_gate:
        st.warning("Awaiting faculty gate. Go to Gate page.")
    st.session_state[f"run_{run_id}"] = str(run.published) if run.published else ""
