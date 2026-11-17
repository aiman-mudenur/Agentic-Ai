import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, google_login_block, live_refresh, metric_card

st.title("Head of Department Dashboard")
st.caption("Departmental oversight: courses, submissions, approvals, and audit activity.")

tab1, tab2 = st.tabs(["Login", "Sign up (real email)"])
with tab1:
    u = st.text_input("Email / Username", key="hod_u")
    p = st.text_input("Password", type="password", key="hod_p")
    if st.button("Login", key="hod_login"):
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
    e = st.text_input("Email id", placeholder="xyz@gmail.com", key="hod_su_e")
    n = st.text_input("Full name", key="hod_su_n")
    pw = st.text_input("Password (min 6 chars)", type="password", key="hod_su_p")
    if st.button("Create HoD account", key="hod_signup"):
        try:
            r = requests.post(f"{API}/auth/signup", json={"email": e.strip(), "password": pw, "full_name": n, "role": "hod"}, timeout=10)
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

if st.session_state.get("role") in ("hod", "admin") and st.session_state.get("token"):
    h = {"Authorization": f"Bearer {st.session_state['token']}"}
    live_refresh("hod_dash")
    if st.button("Refresh now", key="hod_dash_r"):
        st.rerun()

    try:
        d_resp = requests.get(f"{API}/hod/dashboard", headers=h, timeout=10)
        if not d_resp.ok:
            try:
                detail = d_resp.json().get("detail", d_resp.text[:300])
            except Exception:
                detail = d_resp.text[:300]
            if d_resp.status_code == 403:
                st.error("403 Forbidden — this account's role cannot view HoD statistics. Log in as HoD/Admin.")
            elif d_resp.status_code == 404:
                st.error("404 — the API is running old code without /hod routes. Restart Terminal 1 (uvicorn) and Refresh.")
            else:
                st.error(f"{d_resp.status_code}: {detail}")
            st.stop()
        d = d_resp.json()
        st.success(f"Welcome, {d.get('hod_name', 'HoD')}")

        st.subheader("Overview")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            metric_card("Courses", d.get("total_courses", 0))
        with c2:
            metric_card("Students", d.get("total_students", 0))
        with c3:
            metric_card("Faculty", d.get("total_faculty", 0))
        with c4:
            metric_card("Assignments", d.get("total_assignments", 0))

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            metric_card("Total Submissions", d.get("total_submissions", 0))
        breakdown = d.get("submission_breakdown", {}) or {}
        with c2:
            metric_card("Submitted", breakdown.get("submitted", 0))
        with c3:
            metric_card("Draft Graded", breakdown.get("draft_graded_awaiting_approval", 0))
        with c4:
            metric_card("Approved", breakdown.get("approved_published", 0))

        c1, c2 = st.columns(2)
        with c1:
            metric_card("Late Submissions", d.get("late_submissions", 0))
        with c2:
            metric_card("Similarity Flagged", d.get("similarity_flagged", 0))

        pending = d.get("pending_approval", 0)
        if pending and pending > 0:
            st.warning(f"{pending} draft grades awaiting approval")
            if st.button("View Pending Approvals", key="hod_view_pending"):
                st.session_state["hod_show_pending"] = True
                st.rerun()

        st.divider()
        st.subheader("Course Breakdown")
        courses = d.get("courses") or []
        if courses:
            for c in courses:
                with st.expander(f"{c['course_code']} — {c['title']}", expanded=False):
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("Assignments", c.get("assignments", 0))
                    m2.metric("Submissions", c.get("total_submissions", 0))
                    m3.metric("Late Rate", f"{c.get('late_rate', 0)}%")
                    m4.metric("Flagged", c.get("similarity_flagged", 0))
                    m5.metric("Approved", c.get("approved", 0))
        else:
            empty_state("No courses available yet.", title="Course Breakdown")

        if st.session_state.get("hod_show_pending"):
            st.divider()
            st.subheader("Pending Approvals")
            pending_subs = requests.get(f"{API}/hod/submissions/pending-approval", headers=h, timeout=10).json()
            if pending_subs:
                for s in pending_subs:
                    with st.expander(f"#{s['id']} {s['assignment_title']} — {s['student_name']} ({s['course_code']})", expanded=True):
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Student", s["student_name"])
                        c2.metric("Submitted", s["submitted_at"][:10] if s.get("submitted_at") else "—")
                        c3.metric("Grade", f"{s['awarded_marks']}/{s['max_marks']}")
                        c4.metric("Late", "Yes" if s.get("is_late") else "No")
                        if s.get("similarity_flagged"):
                            st.warning("Similarity flagged")
                        if st.button(f"Approve #{s['id']}", key=f"hod_approve_{s['id']}"):
                            r = requests.post(f"{API}/hod/submissions/{s['id']}/approve", headers=h, timeout=10)
                            if r.ok:
                                st.success("Approved!")
                                st.session_state["hod_show_pending"] = False
                                st.rerun()
                            else:
                                st.error(f"{r.status_code}: {r.text[:300]}")
            else:
                empty_state("No pending approvals.", title="Pending Approvals")
            if st.button("Close", key="hod_close_pending"):
                st.session_state["hod_show_pending"] = False
                st.rerun()

        st.divider()
        st.subheader("Project Milestones")
        try:
            proj_resp = requests.get(f"{API}/projects", headers=h, timeout=10)
            if proj_resp.ok:
                projects = proj_resp.json()
            else:
                st.error(f"{proj_resp.status_code}: {proj_resp.text[:300]}")
                projects = None
        except Exception as e:
            st.error(str(e)[:300])
            projects = None

        if projects is None:
            pass
        elif not projects:
            empty_state("No projects defined yet. Faculty can create projects via the API.", title="Project Milestones")
        else:
            for proj in projects:
                with st.expander(f"{proj.get('course_code')} — {proj.get('title')}", expanded=False):
                    st.caption(f"Created by user #{proj.get('created_by')} | {str(proj.get('created_at', ''))[:19]}")
                    try:
                        sh = requests.get(f"{API}/projects/{proj['id']}/schedule-health", headers=h, timeout=10)
                        if sh.ok:
                            health = sh.json()
                        else:
                            health = []
                    except Exception:
                        health = []
                    if health:
                        for g in health:
                            c1, c2, c3, c4 = st.columns(4)
                            with c1:
                                metric_card("Group", g.get("group_id"))
                            with c2:
                                metric_card("Completed", f"{g.get('completed', 0)}/{g.get('total_checkpoints', 0)}")
                            with c3:
                                metric_card("Health", f"{g.get('schedule_health', 0):.0%}")
                            with c4:
                                st.caption(f"Status: {g.get('status')} | Overdue: {g.get('overdue', 0)} | Evidence missing: {g.get('evidence_missing', 0)}")
                            if g.get("members"):
                                st.caption(f"Members: {g['members']}")
                    else:
                        st.caption("No groups or milestones defined for this project.")
                    # List milestones
                    try:
                        ms = requests.get(f"{API}/projects/{proj['id']}/milestones", headers=h, timeout=10)
                        if ms.ok:
                            milestones = ms.json()
                        else:
                            milestones = []
                    except Exception:
                        milestones = []
                    if milestones:
                        st.dataframe(
                            [{"checkpoint": m.get("checkpoint_id"), "name": m.get("name"), "group": m.get("group_id"), "due": str(m.get("due_date", ""))[:10], "status": m.get("status"), "evidence": "Yes" if m.get("evidence") else "No"} for m in milestones],
                            use_container_width=True,
                        )

        st.divider()
        st.subheader("Audit Log (last 100)")
        with st.expander("Audit Log (last 100)", expanded=False):
            logs = requests.get(f"{API}/hod/audit-log", headers=h, timeout=10).json()
            if logs:
                for log in logs:
                    st.caption(f"`{log['timestamp'][:19]}` **{log['action']}** by {log['actor_id']} on {log['entity_type']} #{log['entity_id']}: {log['details'][:200] if log['details'] else ''}")
            else:
                empty_state("No audit entries yet.", title="Audit Log")

    except Exception as e:
        st.error(str(e)[:500])
else:
    st.warning("Login as HoD (or Admin) from the Login page")

google_login_block("hod", "hod")
