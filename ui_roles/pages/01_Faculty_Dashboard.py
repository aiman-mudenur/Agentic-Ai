import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, google_login_block, live_refresh, metric_card

st.title("Faculty Dashboard")
st.caption("Manage courses, assignments, submissions, grading, and pipeline runs.")

tab1, tab2 = st.tabs(["Login", "Sign up (real email)"])
with tab1:
    u = st.text_input("Email / Username", key="fac_u")
    p = st.text_input("Password", type="password", key="fac_p")
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

if st.session_state.get("role") == "faculty" and st.session_state.get("token"):
    h = {"Authorization": f"Bearer {st.session_state['token']}"}
    live_refresh("fac_dash")
    if st.button("Refresh now", key="fac_dash_r"):
        st.rerun()
    try:
        d = requests.get(f"{API}/faculty/dashboard", headers=h, timeout=10).json()

        st.subheader("Summary")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            metric_card("Assignments", d.get("assignments_count", 0))
        with c2:
            metric_card("Submissions", d.get("submissions_count", 0))
        with c3:
            metric_card("Pending", d.get("pending_grading", 0))
        with c4:
            metric_card("Draft Graded", d.get("draft_graded", 0))
        with c5:
            metric_card("Approved", d.get("approved", 0))

        st.divider()
        if d.get("assignment_stats"):
            st.subheader("Assignment Breakdown")
            for a in d["assignment_stats"]:
                with st.expander(f"#{a['assignment_id']} {a['title']} ({a['course_code']})", expanded=False):
                    m1, m2, m3, m4, m5, m6 = st.columns(6)
                    m1.metric("Total", a["total_submissions"])
                    m2.metric("Pending", a["pending"])
                    m3.metric("Draft", a["draft_graded"])
                    m4.metric("Approved", a["approved"])
                    m5.metric("On-time", a["on_time"])
                    m6.metric("Late", a["late"])
        else:
            empty_state("No assignment breakdown available yet.", title="Assignment Breakdown")

        st.divider()
        st.subheader("Submissions")
        status_filter = st.selectbox("Filter by status", ["all", "submitted", "draft_graded", "graded"], key="fac_status_filter")
        params = {}
        if status_filter != "all":
            params["status"] = status_filter
        try:
            subs = requests.get(f"{API}/faculty/submissions", headers=h, params=params, timeout=10)
            if subs.ok:
                items = subs.json()
            else:
                try:
                    st.error(subs.json().get("detail", subs.text[:300]))
                except Exception:
                    st.error(f"{subs.status_code}: {subs.text[:300]}")
                items = []
        except Exception as ex:
            st.error(str(ex)[:300])
            items = []
        if items:
            st.dataframe(items, use_container_width=True)
            st.caption("Tip: open **Faculty → Submissions** for full review, GitHub details/diff, grading, and approval.")
        else:
            empty_state("No submissions yet. (Ensure the submission was created successfully and you are the faculty who owns that assignment.)", title="Submissions")

        st.divider()
        st.subheader("Deadline Reminders")
        st.caption("Send reminder emails for upcoming assignment deadlines. Intervals: 7, 3, and 1 day before the due date.")
        dry_run = st.checkbox("Dry run (check only, no emails sent)", key="rem_dry_run")
        if dry_run:
            st.info("Dry run is ON: this will only check which reminders would be sent and will NOT send any emails.")
        if st.button("Send deadline reminders", key="rem_send"):
            try:
                rr = requests.post(f"{API}/faculty/reminders/send", headers=h, params={"dry_run": dry_run}, timeout=30)
                if rr.ok:
                    summary = rr.json()
                    checked = summary.get("checked", 0)
                    sent = summary.get("sent", 0)
                    skipped = summary.get("skipped", 0)
                    failed = summary.get("failed", 0)
                    if dry_run:
                        st.success(f"Dry run complete: checked {checked} assignment(s). Would send {skipped} reminder(s) (already sent/duplicate/expired reminders are skipped). No emails were sent.")
                    elif sent == 0 and skipped > 0:
                        st.info(f"Checked {checked} assignment(s). Sent: {sent}. Nothing new to send — {skipped} reminder(s) were already sent or outside the reminder window (7/3/1 days before due date). If you expected emails, uncheck 'Dry run' and try again after the window, or check the assignment due date.")
                    else:
                        st.success(f"Checked {checked} assignment(s). Sent: {sent}, Skipped: {skipped}, Failed: {failed}.")
                    if summary.get("errors"):
                        for err in summary["errors"][:5]:
                            st.warning(err)
                else:
                    st.error(f"{rr.status_code}: {rr.text[:300]}")
            except Exception as e:
                st.error(str(e)[:300])

        st.divider()
        st.subheader("Run Status")
        try:
            repo_root = Path(__file__).resolve().parents[2]
            base = repo_root / "out" / "lab07"

            faculty_courses = d.get("courses", [])
            run_statuses = []

            for course_code in faculty_courses:
                if base.is_dir():
                    for cp in sorted(base.iterdir()):
                        if cp.is_dir() and (cp / "graph.sqlite").exists():
                            run_id = cp.name
                            try:
                                resp = requests.get(
                                    f"{API}/runs/{run_id}/status",
                                    headers=h,
                                    timeout=5,
                                )
                                if resp.ok:
                                    rs = resp.json()
                                    run_course = rs.get("course_authorized", False)
                                    if run_course:
                                        run_statuses.append({
                                            "run_id": run_id,
                                            "status": rs.get("status"),
                                            "domain": rs.get("domain"),
                                            "specialist_ok": rs.get("specialist_ok"),
                                            "domain_error": rs.get("domain_error"),
                                            "awaiting_gate": rs.get("awaiting_gate"),
                                            "nodes_ran": rs.get("nodes_ran", []),
                                        })
                            except Exception:
                                continue

                            if len(run_statuses) >= 3:
                                break

            if run_statuses:
                st.caption(f"Found {len(run_statuses)} run(s) with status info")
                for rs in run_statuses:
                    with st.expander(f"Run {rs['run_id']}", expanded=False):
                        cols = st.columns(3)
                        with cols[0]:
                            spec_ok = rs.get("specialist_ok")
                            st.caption(f"Specialist OK: {spec_ok}")
                            if spec_ok:
                                st.success("Specialist completed successfully")
                            elif spec_ok is False:
                                st.error("Specialist result unsuccessful")
                            else:
                                st.info("Specialist status unavailable")

                        with cols[1]:
                            dome = rs.get("domain")
                            st.caption(f"Domain: {dome if dome else 'unknown'}")

                        with cols[2]:
                            st_cap = rs.get("status")
                            if rs.get("awaiting_gate"):
                                st.info("Awaiting faculty approval")
                            elif st_cap == "published":
                                st.success("Published")
                            elif st_cap == "refused":
                                st.error("Refused/escalated")
                            elif st_cap == "escalated":
                                st.error("Failed validation - escalated, not published")
                            else:
                                st.info(f"Status: {st_cap}")

                        dome_err = rs.get("domain_error")
                        if dome_err:
                            st.caption(f"Domain error: {dome_err}")

                        nodes = rs.get("nodes_ran", [])
                        st.caption(f"Nodes: {', '.join(nodes) if nodes else 'none'}")
            else:
                empty_state("No run status information available. Runs can be started via the API '/api/runs/pipeline/start' and status checked at '/api/runs/{run_id}/status'.", title="Run Status")
        except Exception as e:
            st.info(f"Run status temporarily unavailable: {str(e)[:200]}")
    except Exception as e:
        st.error(f"Error loading dashboard: {str(e)[:300]}")
else:
    st.warning("Login as Faculty")

google_login_block("faculty", "fac")
