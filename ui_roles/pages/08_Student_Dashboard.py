import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, google_login_block, live_refresh, metric_card, status_pill

st.title("Student Dashboard")
st.caption("Your enrolled courses, assignments, submissions, and progress in one place.")

tab1, tab2 = st.tabs(["Login", "Sign up (real email)"])
with tab1:
    u = st.text_input("Email / Username", key="stu_u")
    p = st.text_input("Password", type="password", key="stu_p")
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
    if st.button("Refresh now", key="stu_dash_r"):
        st.rerun()
    try:
        resp = requests.get(f"{API}/student/dashboard", headers=h, timeout=10)
        if not resp.ok:
            st.error(f"{resp.status_code}: {resp.text[:500]}")
        else:
            d = resp.json()

            st.success(f"Welcome, {d.get('student_name', 'Student')}!")

            st.subheader("Enrolled Courses")
            courses = d.get("enrolled_courses", []) or []
            if courses:
                st.write(", ".join(courses))
            else:
                empty_state("You are not enrolled in any courses yet.", title="Enrolled Courses")

            st.divider()

            st.subheader("Summary")
            c1, c2, c3 = st.columns(3)
            with c1:
                metric_card("Assignments", d.get("assignments_count", 0))
            with c2:
                metric_card("My Submissions", d.get("submissions_count", 0))
            with c3:
                metric_card("Notes", d.get("notes_count", 0))

            course_progress = d.get("course_progress") or {}
            if course_progress:
                st.divider()
                st.subheader("Course Progress")
                for course_code, prog in course_progress.items():
                    pct = prog.get("completion_pct", 0)
                    with st.expander(f"{course_code} — {prog.get('status', '')} ({pct:.0f}%)", expanded=True):
                        m1, m2 = st.columns(2)
                        with m1:
                            metric_card("Completion", f"{pct:.0f}%")
                        with m2:
                            metric_card("Marks", f"{prog.get('marks_obtained') or '—'}/{prog.get('max_marks') or '—'}")
                        st.progress(min(max(float(pct) / 100.0, 0.0), 1.0))
            else:
                empty_state("No course progress available yet.", title="Course Progress")

            assignment_status = d.get("assignment_status") or {}
            if assignment_status:
                st.divider()
                st.subheader("Assignments")
                for aid, status in assignment_status.items():
                    late = " — Late" if status.get("is_late") else ""
                    grade = f" — Grade: {status.get('awarded_marks')}/{status.get('max_marks', '?')}" if status.get("awarded_marks") is not None else ""
                    cols = st.columns([1, 5])
                    with cols[0]:
                        status_pill("submitted" if status.get("submitted") else "not submitted")
                    with cols[1]:
                        st.write(f"Assignment #{aid}: {status.get('status', '')}{late}{grade}")
            else:
                empty_state("No assignments published yet.", title="Assignments")
    except Exception as e:
        st.error(str(e)[:500])
else:
    st.warning("Login as Student")

google_login_block("student", "stu")
