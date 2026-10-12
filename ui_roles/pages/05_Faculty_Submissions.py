import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, download_button, live_refresh, safe_list, status_badge

st.title("📥 Submissions & Grading")

if st.session_state.get("role") not in ("faculty", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_subs")

aid = st.number_input("Filter by Assignment ID (0 = all)", 0, 100000, 0)
params = {"assignment_id": int(aid)} if aid else {}
subs = safe_list(requests.get(f"{API}/submissions/", headers=h, params=params, timeout=10))
pending = sum(1 for s in subs if s.get("status") == "submitted")

c1, c2 = st.columns(2)
c1.metric("📥 Total submissions (live)", len(subs))
c2.metric("⏳ Pending grading", pending)

if not subs:
    st.info("No submissions yet. Counts update live here as students submit.")
else:
    st.dataframe(
        [{"id": s["id"], "assignment": s["assignment_id"], "file": s.get("file_name"), "status": s.get("status"), "marks": s.get("awarded_marks"), "late": s.get("is_late")} for s in subs[::-1][:50]],
        width="stretch",
    )
    for s in subs[::-1][:20]:
        sid = s["id"]
        with st.expander(f"{status_badge(s.get('status',''))}  #{sid} · {s.get('file_name')} · {s.get('awarded_marks') or '—'}/{s.get('max_marks') or '?'}", expanded=False):
            if s.get("feedback"):
                st.write(f"💬 {s['feedback']}")
            if s.get("similarity_score") is not None:
                flag = "🚩 flagged" if s.get("similarity_flagged") else "ok"
                st.caption(f"Similarity: {s['similarity_score']} ({flag})")
            download_button("⬇ Open / download submission", "submissions", sid, s.get("file_name"), f"sub_{sid}")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button(f"🤖 Auto-grade with agent #{sid}", key=f"ag_{sid}"):
                    try:
                        gr = requests.post(f"{API}/submissions/{sid}/auto-grade", headers=h, timeout=60)
                        if gr.ok:
                            st.success(f"Agent graded: {gr.json().get('awarded_marks')} marks ✅ (faculty can override below)")
                            st.rerun()
                        else:
                            st.error(f"{gr.status_code}: {gr.text[:500]}")
                    except Exception as ex:
                        st.error(str(ex)[:300])
            with col_b:
                with st.form(f"g_{sid}"):
                    marks = st.number_input("Awarded marks", 0.0, 200.0, float(s.get("awarded_marks") or 0.0))
                    fb = st.text_area("Feedback", value=s.get("feedback") or "")
                    if st.form_submit_button("Save manual grade"):
                        try:
                            gr = requests.post(f"{API}/submissions/{sid}/grade", headers=h, params={"awarded_marks": float(marks), "feedback": fb.strip() or None}, timeout=15)
                            st.success("Graded ✅") if gr.ok else st.error(f"{gr.status_code}: {gr.text[:500]}")
                        except Exception as ex:
                            st.error(str(ex)[:300])
