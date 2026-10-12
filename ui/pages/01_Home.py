import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

st.title("Home")
st.markdown("""
- Lab 7: full pipeline (ingest → plan → workers → validate → assemble → gate → publish)
- Lab 8: parallel swarm (dispatch → merge → validate → gate → publish)
- Gate approval supports approve/edit/override/refuse
- Outputs in `out/lab07/` and `out/lab08/`
""")
st.info("Use the sidebar to navigate.")
