import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, download_button, empty_state, live_refresh, metric_card, safe_list, status_badge, status_pill

st.title("My Submissions")
st.caption("Your submitted work, grades, feedback, and submission history.")

if st.session_state.get("role") != "student" or not st.session_state.get("token"):
    st.warning("Login as Student first (Student Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("stu_subs")

try:
    r = requests.get(f"{API}/submissions/", headers=h, timeout=10)
    if r.ok:
        items = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        items = None
except Exception as e:
    st.error(str(e)[:300])
    items = None

if items is None:
    pass
elif not items:
    empty_state("No submissions yet. Submit your first one below.", title="My Submissions")
else:
    total = len(items)
    graded = sum(1 for s in items if s.get("status") == "graded")
    pending = total - graded
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("Total Submissions", total)
    with c2:
        metric_card("Graded", graded)
    with c3:
        metric_card("Pending", pending)

    for s in items[::-1][:20]:
        status = s.get("status")
        badge = status_badge(status)
        submission_type = s.get("submission_type", "file")
        with st.expander(f"{badge} · Assignment #{s.get('assignment_id')} · {s.get('file_name') or s.get('github_repo_url', 'no file')}", expanded=False):
            status_pill(status or "submitted")
            if s.get("feedback"):
                st.info(f"Feedback: {s['feedback']}")
            if s.get("is_late"):
                st.warning("Submitted late")
            # Show submission type specific info
            if s.get("submission_type") == "github":
                st.caption(f"Repository: {s.get('github_repo_url')}")
                st.caption(f"Commit: {s.get('commit_hash') or '—'} · Branch: {s.get('branch') or '—'}")
                col_diff, col_commits = st.columns(2)
                with col_diff:
                    if st.button("View commit diff", key=f"diff_{s['id']}"):
                        try:
                            rd = requests.get(f"{API}/submissions/{s['id']}/diff", headers=h, timeout=15)
                            if rd.ok:
                                diff_data = rd.json()
                                st.caption(f"Changes: {diff_data.get('total_changes', 0)} (+{diff_data.get('total_additions', 0)}/-{diff_data.get('total_deletions', 0)})")
                                with st.expander(f"View diff ({len(diff_data.get('files', []))} files)", expanded=True):
                                    for f in diff_data.get("files", []):
                                        st.caption(f"**{f.get('filename')}** ({f.get('status')}) +{f.get('additions',0)}/-{f.get('deletions',0)}")
                                        if f.get("patch"):
                                            st.code(f["patch"], language="diff")
                            else:
                                st.error(f"{rd.status_code}: {rd.text[:300]}")
                        except Exception as e:
                            st.error(str(e)[:300])
                with col_commits:
                    if st.button("View commits", key=f"commits_{s['id']}"):
                        try:
                            rc = requests.get(f"{API}/submissions/{s['id']}/commits", headers=h, timeout=15)
                            if rc.ok:
                                commits_data = rc.json()
                                commits = commits_data.get("commits", [])
                                if commits:
                                    st.caption(f"Recent commits ({len(commits)}):")
                                    for c in commits[:5]:
                                        st.caption(f"`{c.get('short_sha', c.get('sha','')[:7])}` {c.get('message', '')[:60]}")
                                else:
                                    st.caption("No commits found")
                            else:
                                st.error(f"{rc.status_code}: {rc.text[:300]}")
                        except Exception as e:
                            st.error(str(e)[:300])
            else:
                download_button("Open / download my file", "submissions", s["id"], s.get("file_name"), f"mysub_{s['id']}")

st.divider()
st.subheader("Submit Assignment")
asgs = safe_list(requests.get(f"{API}/student/assignments", headers=h, timeout=10))
options = [(a["id"], a["title"]) for a in asgs if not a.get("submitted")]
if options:
    aid, title = st.selectbox("Assignment", options, format_func=lambda x: f"#{x[0]} {x[1]}")
else:
    aid = st.number_input("Assignment ID", 1, 100000, 1)
    title = ""

# Submission type selector
sub_type = st.radio("Submission type", ["File upload", "GitHub repository"], key="sub_type", horizontal=True)

if sub_type == "File upload":
    f = st.file_uploader("Your file", key="sub_file")
    if st.button("Submit"):
        if not f:
            st.error("Choose a file first.")
        else:
            try:
                fr = requests.post(f"{API}/submissions/submit", headers={"Authorization": h["Authorization"]}, data={"assignment_id": int(aid)}, files={"file": (f.name, f.getvalue())}, timeout=60)
                if fr.ok:
                    st.success("Submitted — write-once recorded. Progress updates after grading.")
                    st.rerun()
                else:
                    st.error(f"{fr.status_code}: {fr.text[:500]}")
            except Exception as ex:
                st.error(str(ex)[:500])
else:
    repo_url = st.text_input("GitHub repository URL", key="sub_repo_url", placeholder="https://github.com/owner/repo")
    commit_hash = st.text_input("Commit SHA", key="sub_commit_hash", placeholder="a1b2c3d4e5f6...")
    branch = st.text_input("Branch (optional)", key="sub_branch", placeholder="main")
    github_token = st.text_input(
        "GitHub token (optional)",
        key="sub_github_token",
        type="password",
        placeholder="ghp_... (helps with rate limits or private repos)",
        help="Optional personal access token. If omitted, the server may use a configured token or anonymous GitHub access.",
    )
    if st.button("Submit"):
        if not repo_url.strip() or not commit_hash.strip():
            st.error("Repository URL and commit SHA are required.")
        else:
            try:
                payload = {
                    "assignment_id": int(aid),
                    "repo_url": repo_url.strip(),
                    "commit_hash": commit_hash.strip(),
                    "branch": branch.strip() or None,
                }
                if github_token.strip():
                    payload["github_token"] = github_token.strip()
                fr = requests.post(f"{API}/submissions/github", headers={"Authorization": h["Authorization"]}, data=payload, timeout=60)
                if fr.ok:
                    st.success("Submitted — write-once recorded. Progress updates after grading.")
                    st.rerun()
                else:
                    st.error(f"{fr.status_code}: {fr.text[:500]}")
            except Exception as ex:
                st.error(str(ex)[:500])