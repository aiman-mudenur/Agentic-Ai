import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.graph.swarm import run_swarm

st.title("Run Swarm (Lab 8)")
root = REPO / "out" / "lab08"
run_id = st.text_input("Run ID", value="ui-swarm-1")
parallel = st.checkbox("Parallel (Send fan-out)", value=True)

if st.button("Run swarm", type="primary"):
    root.mkdir(parents=True, exist_ok=True)
    run = run_swarm(
        course_code="CS201",
        run_id=run_id,
        root=root,
        output_dir=root / run_id,
        parallel=parallel,
    )
    st.success("Started")
    st.write(run.summary())
    st.write("nodes:", " → ".join(run.nodes_ran()))
    t = run.state.get("swarm_timing")
    if t:
        st.write(dict(mode=t["mode"], tasks=t["tasks"], stage_ms=round(t["stage_ms"],3), span_ms=round(t["span_ms"],3), overlap=t["max_overlap"], quality=run.state.get("swarm_quality")))
    if run.awaiting_gate:
        st.warning("Awaiting faculty gate. Go to Gate page.")
