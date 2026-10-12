import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
EVID = REPO / "docs" / "evidence"

st.title("Evidence Files")
if EVID.exists():
    files = sorted(EVID.glob("lab08*")) + sorted(EVID.glob("lab07*")) + sorted(EVID.glob("lab06*"))
    for f in files:
        with st.expander(f.name):
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
                st.code(txt[:4000], language="text")
                if len(txt) > 4000:
                    st.caption("Truncated preview")
            except Exception as e:
                st.error(str(e))
else:
    st.info("No evidence dir.")
