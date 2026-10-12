import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import API, live_refresh

st.title("🧑‍🎓 Student Dashboard")

tab1, tab2 = st.tabs(["Login", "Sign up (real email)"])
with tab1:
    u = st.text_input("Email / Username", value="student@gmail.com", key="stu_u")
    p = st.text_input("Password", type="password", value="student123", key="stu_p")
    if st.button("Login", key="stu_login"):
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
    e = st.text_input("Email id", placeholder="xyz@gmail.com", key="stu_su_e")
    n = st.text_input("Full name", key="stu_su_n")
    pw = st.text_input("Password (min 6 chars)", type="password", key="stu_su_p")
    if st.button("Create student account", key="stu_signup"):
        try:
            r = requests.post(f"{API}/auth/signup", json={"email": e.strip(), "password": pw, "full_name": n, "role": "student"}, timeout=10)
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

if st.session_state.get("role") == "student" and st.session_state.get("token"):
    h = {"Authorization": f"Bearer {st.session_state['token']}"}
    live_refresh("stu_dash")
    if st.button("↻ Refresh now", key="stu_dash_r"):
        st.rerun()
    try:
        d = requests.get(f"{API}/student/dashboard", headers=h, timeout=10).json()
        c1, c2, c3 = st.columns(3)
        c1.metric("📝 Assignments", d.get("assignments", 0))
        c2.metric("📤 My submissions", d.get("submissions", 0))
        c3.metric("📚 Notes", d.get("notes", 0))
        prog = d.get("progress") or {}
        if prog:
            st.metric("📊 Overall completion", f"{prog.get('completion_pct', 0)}%")
            st.progress(min(max(float(prog.get("completion_pct", 0)) / 100.0, 0.0), 1.0))
            st.caption(f"Status: {prog.get('status')} · Marks: {prog.get('marks_obtained') or '—'}/{prog.get('max_marks') or '—'} (updates live after each grading)")
    except Exception as e:
        st.error(str(e)[:500])
else:
    st.warning("Login as Student")
