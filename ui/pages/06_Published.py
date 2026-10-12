import sys
from pathlib import Path
import streamlit as st

REPO = Path(__file__).resolve().parents[2]

st.title("Published Documents")
kind = st.radio("System", ["lab07", "lab08"], index=0)
root = REPO / "out" / kind
if root.exists():
    files = list(root.rglob("DOC-*-organised-course-*.md"))
    if not files:
        st.info("No published documents found.")
    else:
        choices = {str(f): f for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)}
        sel = st.selectbox("Select document", list(choices.keys()))
        p = choices[sel]
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
            st.code(txt[:5000], language="markdown")
            if len(txt) > 5000:
                st.caption("Truncated preview")
            st.download_button("Download", txt, file_name=p.name)
        except Exception as e:
            st.error(str(e))
else:
    st.info("No outputs yet.")
