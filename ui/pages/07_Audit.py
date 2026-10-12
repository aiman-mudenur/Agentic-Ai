import sys
from pathlib import Path
import json
import streamlit as st

REPO = Path(__file__).resolve().parents[2]

st.title("Audit & Timing")
kind = st.radio("System", ["lab07", "lab08"], index=0)
root = REPO / "out" / kind
if root.exists():
    states = list(root.rglob("state.json"))
    if states:
        sel = st.selectbox("Select run", list(map(str, sorted(states, key=lambda p: p.stat().st_mtime, reverse=True))))
        try:
            data = json.loads(Path(sel).read_text(encoding="utf-8"))
            st.subheader("Summary")
            st.write(data.get("summary", {}))
            st.subheader("Nodes")
            st.write(data.get("nodes_ran", []))
            st.subheader("Audit trail")
            for e in data.get("audit_trail", []):
                st.write(e)
            if data.get("swarm_timing"):
                st.subheader("Swarm timing")
                st.write(data["swarm_timing"])
            if data.get("swarm_merge_log"):
                st.caption(f"Merge log entries: {len(data['swarm_merge_log'])}")
        except Exception as e:
            st.error(str(e))
    else:
        st.info("No state.json found.")
else:
    st.info("No outputs.")
