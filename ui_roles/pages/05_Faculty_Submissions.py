import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, metric_card, safe_list, status_badge, status_pill

st.title("Submissions and Grading")
st.caption("Review, grade, and approve student submissions.")

if st.session_state.get("role") != "faculty" or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("fac_subs")

# Filters
c1, c2 = st.columns(2)
with c1:
    aid = st.number_input("Filter by Assignment ID (0 = all)", 0, 100000, 0)
with c2:
    status_filter = st.selectbox("Status", ["all", "submitted", "draft_graded", "graded"], key="fac_status_filter")

params = {}
if aid:
    params["assignment_id"] = int(aid)
if status_filter != "all":
    params["status"] = status_filter

loaded = False
try:
    r = requests.get(f"{API}/faculty/submissions", headers=h, params=params, timeout=10)
    if r.ok:
        subs = r.json()
        loaded = True
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        subs = []
except Exception as e:
    st.error(str(e)[:300])
    subs = []

# Metrics
c1, c2, c3, c4 = st.columns(4)
with c1:
    metric_card("Total", len(subs))
with c2:
    metric_card("Pending", sum(1 for s in subs if s.get("status") == "submitted"))
with c3:
    metric_card("Draft Graded", sum(1 for s in subs if s.get("status") == "draft_graded"))
with c4:
    metric_card("Approved", sum(1 for s in subs if s.get("status") == "graded"))

if loaded and not subs:
    empty_state("No submissions yet.", title="Submissions")
elif subs:
    st.dataframe(
        [{"id": s["id"], "assignment": s.get("assignment_title"), "student": s.get("student_id"), "file": s.get("file_name") or s.get("github_repo_url"), "status": s.get("status"), "marks": s.get("awarded_marks"), "late": s.get("is_late"), "type": s.get("submission_type", "file")} for s in subs],
        use_container_width=True,
    )
    for s in subs[:20]:
        sid = s["id"]
        status = s.get("status")
        with st.expander(f"{status_badge(status)}  #{sid} · {s.get('assignment_title')} · {s.get('file_name')} · {s.get('awarded_marks') or '—'}/{s.get('max_marks') or '?'}", expanded=False):
            status_pill(status or "submitted")
            stype = s.get("submission_type") or "file"
            if stype == "github":
                st.markdown("**Type:** GitHub submission")
                repo = s.get("github_repo_url")
                commit = s.get("commit_hash") or ""
                branch = s.get("branch") or ""
                if repo:
                    st.markdown(f"**Repository:** [{repo}]({repo})")
                st.caption(f"Commit: {commit or '—'} · Branch: {branch or '—'}")
                if st.button(f"Load commit diff #{sid}", key=f"diff_{sid}"):
                    try:
                        dr = requests.get(f"{API}/submissions/{sid}/diff", headers=h, timeout=30)
                        if dr.ok:
                            diff = dr.json()
                            st.caption(f"Changes: +{diff.get('total_additions', 0)} / -{diff.get('total_deletions', 0)} ({diff.get('total_changes', 0)} total)")
                            for f in (diff.get("files") or []):
                                with st.expander(f"{f.get('status','')} {f.get('filename','')}", expanded=False):
                                    if f.get("patch"):
                                        st.code(f.get("patch"), language="diff")
                                    else:
                                        st.caption("No textual patch available.")
                        else:
                            try:
                                st.error(dr.json().get("detail", dr.text[:400]))
                            except Exception:
                                st.error(f"{dr.status_code}: {dr.text[:400]}")
                    except Exception as ex:
                        st.error(str(ex)[:300])
            else:
                st.markdown("**Type:** File submission")
                download_button("Open / download", "submissions", sid, s.get("file_name"), f"fac_sub_{sid}")
            if s.get("feedback"):
                st.info(f"Feedback: {s['feedback']}")
            if s.get("similarity_score") is not None:
                flag = "flagged" if s.get("similarity_flagged") else "ok"
                st.caption(f"Similarity: {s['similarity_score']} ({flag})")

            if status == "submitted":
                col_a, col_b = st.columns(2)
                with col_a:
                    if stype == "github":
                        st.caption("Auto-grading is not available for GitHub submissions. Review the repo/commit/diff and grade manually.")
                    else:
                        if st.button(f"Auto-grade #{sid}", key=f"ag_{sid}"):
                            try:
                                gr = requests.post(f"{API}/submissions/{sid}/auto-grade", headers=h, timeout=60)
                                if gr.ok:
                                    st.success(f"Draft grade: {gr.json().get('awarded_marks')} marks")
                                    st.rerun()
                                else:
                                    try:
                                        st.error(gr.json().get("detail", gr.text[:500]))
                                    except Exception:
                                        st.error(f"{gr.status_code}: {gr.text[:500]}")
                            except Exception as ex:
                                st.error(str(ex)[:300])
                with col_b:
                    with st.form(f"grade_{sid}"):
                        marks = st.number_input("Awarded marks", 0.0, 200.0, float(s.get("awarded_marks") or 0.0))
                        fb = st.text_area("Feedback", value=s.get("feedback") or "")
                        if st.form_submit_button("Save as draft grade"):
                            try:
                                gr = requests.post(f"{API}/submissions/{sid}/grade", headers=h, params={"awarded_marks": float(marks), "feedback": fb.strip() or None}, timeout=15)
                                if gr.ok:
                                    st.success("Draft grade saved.")
                                    st.rerun()
                                else:
                                    try:
                                        st.error(gr.json().get("detail", gr.text[:500]))
                                    except Exception:
                                        st.error(f"{gr.status_code}: {gr.text[:500]}")
                            except Exception as ex:
                                st.error(str(ex)[:300])

            elif status == "draft_graded":
                st.info("Draft grade entered. Approve to publish to student.")
                if st.button(f"Approve and Publish #{sid}", key=f"approve_{sid}"):
                    try:
                        gr = requests.post(f"{API}/submissions/{sid}/approve", headers=h, timeout=15)
                        if gr.ok:
                            st.success("Approved and published to student.")
                            st.rerun()
                        else:
                            st.error(f"{gr.status_code}: {gr.text[:500]}")
                    except Exception as ex:
                        st.error(str(ex)[:300])
                with st.form(f"edit_{sid}"):
                    marks = st.number_input("Edit marks", 0.0, 200.0, float(s.get("awarded_marks") or 0.0))
                    fb = st.text_area("Edit feedback", value=s.get("feedback") or "")
                    if st.form_submit_button("Update draft grade"):
                        try:
                            gr = requests.post(f"{API}/submissions/{sid}/grade", headers=h, params={"awarded_marks": float(marks), "feedback": fb.strip() or None}, timeout=15)
                            if gr.ok:
                                st.success("Draft grade updated.")
                                st.rerun()
                            else:
                                st.error(f"{gr.status_code}: {gr.text[:500]}")
                        except Exception as ex:
                            st.error(str(ex)[:300])

            elif status == "graded":
                st.success("Published to student")
                if s.get("approved_at"):
                    st.caption(f"Approved at: {s['approved_at'][:19]}")
                with st.form(f"override_{sid}"):
                    marks = st.number_input("Override marks", 0.0, 200.0, float(s.get("awarded_marks") or 0.0))
                    fb = st.text_area("Override feedback", value=s.get("feedback") or "")
                    if st.form_submit_button("Override (creates new draft)"):
                        try:
                            gr = requests.post(f"{API}/submissions/{sid}/grade", headers=h, params={"awarded_marks": float(marks), "feedback": fb.strip() or None}, timeout=15)
                            if gr.ok:
                                st.success("New draft grade created.")
                                st.rerun()
                            else:
                                st.error(f"{gr.status_code}: {gr.text[:500]}")
                        except Exception as ex:
                            st.error(str(ex)[:300])

st.divider()
st.subheader("Release Management")
st.caption("Publish versioned releases of approved course workspaces and manage rollback.")

with st.expander("Publish a new release", expanded=False):
    with st.form("publish_release"):
        pub_course = st.text_input("Course code", key="pub_course")
        pub_artifact = st.text_input("Artifact reference (run id or path, optional)", key="pub_artifact")
        pub_evidence = st.text_input("Evidence reference (optional)", key="pub_evidence")
        if st.form_submit_button("Publish release"):
            if not pub_course.strip():
                st.error("Course code is required.")
            else:
                try:
                    r = requests.post(f"{API}/releases/publish", headers=h, json={
                        "course_code": pub_course.strip(),
                        "version": 1,
                        "artifact_ref": pub_artifact.strip() or None,
                        "evidence_ref": pub_evidence.strip() or None,
                    }, timeout=15)
                    if r.ok:
                        d = r.json()
                        st.success(f"Published release v{d.get('version')} for {d.get('course_code')} (status: {d.get('status')}).")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])

st.subheader("Current release")
cur_course = st.text_input("Course code", key="cur_course", value="")
if cur_course.strip():
    try:
        cr = requests.get(f"{API}/releases/current/{cur_course.strip()}", headers=h, timeout=10)
        if cr.ok:
            cur = cr.json()
            if cur.get("detail"):
                st.info(cur["detail"])
            else:
                st.write(f"**Version:** {cur.get('version')} | **Status:** {cur.get('status')} | **Created:** {str(cur.get('created_at', ''))[:19]}")
                st.caption(f"Artifact: {cur.get('artifact_ref') or '—'} | Evidence: {cur.get('evidence_ref') or '—'}")
        else:
            st.error(f"{cr.status_code}: {cr.text[:300]}")
    except Exception as e:
        st.error(str(e)[:300])

st.subheader("Release history")
hist_course = st.text_input("Filter by course code (optional)", key="hist_course")
try:
    hr = requests.get(f"{API}/releases/", headers=h, params={"course_code": hist_course.strip()} if hist_course.strip() else {}, timeout=10)
    if hr.ok:
        releases = hr.json()
    else:
        st.error(f"{hr.status_code}: {hr.text[:300]}")
        releases = None
except Exception as e:
    st.error(str(e)[:300])
    releases = None

if releases is not None and not releases:
    empty_state("No releases found.", title="Release history")
elif releases:
    st.dataframe(
        [{"id": r.get("id"), "course": r.get("course_code"), "version": r.get("version"), "status": r.get("status"), "created_at": str(r.get("created_at", ""))[:19], "artifact": r.get("artifact_ref")} for r in releases],
        use_container_width=True,
    )
    for r in releases:
        if r.get("status") == "current":
            if st.button(f"Rollback release #{r.get('id')} (v{r.get('version')})", key=f"rb_{r.get('id')}"):
                try:
                    rr = requests.post(f"{API}/releases/rollback/{r.get('id')}", headers=h, timeout=10)
                    if rr.ok:
                        st.success(f"Rollback completed for release #{r.get('id')}.")
                        st.rerun()
                    else:
                        try:
                            st.error(rr.json().get("detail", rr.text[:300]))
                        except Exception:
                            st.error(f"{rr.status_code}: {rr.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])
