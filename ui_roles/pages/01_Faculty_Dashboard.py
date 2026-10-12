import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, live_refresh

st.title("👩‍🏫 Faculty Dashboard")

tab1, tab2 = st.tabs(["Login", "Sign up (real email)"])
with tab1:
    u = st.text_input("Email / Username", value="faculty@gmail.com", key="fac_u")
    p = st.text_input("Password", type="password", value="faculty123", key="fac_p")
    if st.button("Login", key="fac_login"):
        try:
            r = requests.post(f"{API}/auth/login", data={"username": u.strip(), "password": p}, timeout=10)
            if r.ok:
                t = r.json()
                st.session_state["token"] = t["access_token"]
                st.session_state["role"] = t["role"]
                st.session_state["email"] = t.get("email", u)
                st.success(f"Logged in as {t.get('email')}")
                st.rerun()
            else:
                st.error(f"{r.status_code}: {r.text[:500]}")
        except Exception as e:
            st.error(str(e)[:500])
with tab2:
    st.caption("Register with your real email id (e.g. xyz@gmail.com)")
    e = st.text_input("Email id", placeholder="xyz@gmail.com", key="fac_su_e")
    n = st.text_input("Full name", key="fac_su_n")
    pw = st.text_input("Password (min 6 chars)", type="password", key="fac_su_p")
    if st.button("Create faculty account", key="fac_signup"):
        try:
            r = requests.post(f"{API}/auth/signup", json={"email": e.strip(), "password": pw, "full_name": n, "role": "faculty"}, timeout=10)
            if r.ok:
                t = r.json()
                st.session_state["token"] = t["access_token"]
                st.session_state["role"] = t["role"]
                st.session_state["email"] = t.get("email", e)
                st.success(f"Account created: {t.get('email')}")
                st.rerun()
            else:
                st.error(f"{r.status_code}: {r.text[:500]}")
        except Exception as ex:
            st.error(str(ex)[:500])

if st.session_state.get("role") in ("faculty", "admin") and st.session_state.get("token"):
    h = {"Authorization": f"Bearer {st.session_state['token']}"}
    live_refresh("fac_dash")
    if st.button("↻ Refresh now", key="fac_dash_r"):
        st.rerun()
    try:
        d = requests.get(f"{API}/faculty/dashboard", headers=h, timeout=10).json()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📝 Assignments", d.get("assignments_count", 0))
        c2.metric("📥 Submissions (live)", d.get("submissions_count", 0))
        c3.metric("⏳ Pending grading", d.get("pending_grading", 0))
        c4.metric("📚 Notes", d.get("notes_count", 0))
        subs = requests.get(f"{API}/submissions/", headers=h, timeout=10)
        items = subs.json() if subs.ok else []
        if items:
            graded = [s for s in items if s.get("status") == "graded" and s.get("awarded_marks") is not None]
            if graded:
                avg = sum(float(s["awarded_marks"]) for s in graded) / len(graded)
                st.metric("⭐ Class average (graded)", f"{avg:.1f}")
                st.bar_chart({f"#{s['id']}": float(s["awarded_marks"]) for s in graded[-20:]})
            st.caption("Latest submissions")
            st.dataframe(
                [
                    {"id": s["id"], "assignment": s["assignment_id"], "file": s.get("file_name"), "status": s.get("status"), "marks": s.get("awarded_marks")}
                    for s in items[-10:][::-1]
                ]
            )
        else:
            st.info("No submissions yet. Counts update live here as students submit.")
    except Exception as e:
        st.error(str(e)[:500])
else:
    st.warning("Login as Faculty")
