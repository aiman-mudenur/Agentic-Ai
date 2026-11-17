import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, metric_card, safe_list, status_pill

st.title("Projects and Milestones")
st.caption("Manage course projects, groups, checkpoints, and milestone evidence.")

if st.session_state.get("role") not in ("faculty", "hod", "admin", "student") or not st.session_state.get("token"):
    st.warning("Login first (Login page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
role = st.session_state.get("role")
live_refresh("projects")

# --- List projects ---
try:
    r = requests.get(f"{API}/projects", headers=h, timeout=10)
    if r.ok:
        projects = r.json()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")
        projects = None
except Exception as e:
    st.error(str(e)[:300])
    projects = None

if projects is None:
    pass
elif not projects:
    empty_state("No projects yet.", title="Projects")
else:
    for proj in projects:
        with st.expander(f"{proj.get('course_code')} — {proj.get('title')}", expanded=False):
            st.caption(f"Created by user #{proj.get('created_by')} | {str(proj.get('created_at', ''))[:19]}")
            if proj.get("description"):
                st.write(proj["description"])

            # Schedule health
            try:
                sh = requests.get(f"{API}/projects/{proj['id']}/schedule-health", headers=h, timeout=10)
                health = sh.json() if sh.ok else []
            except Exception:
                health = []
            if health:
                st.subheader("Group progress")
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

            # Groups
            try:
                gs = requests.get(f"{API}/projects/{proj['id']}/groups", headers=h, timeout=10)
                groups = gs.json() if gs.ok else []
            except Exception:
                groups = []
            if groups:
                st.subheader("Groups")
                for g in groups:
                    with st.expander(f"Group {g.get('group_id')} — {len([m for m in (g.get('members') or '').split(',') if m.strip()])} members", expanded=False):
                        members = [m.strip() for m in (g.get("members") or "").split(",") if m.strip()]
                        if members:
                            st.write("Members: " + ", ".join(members))
                        else:
                            st.caption("No members assigned.")
                        # Update members (faculty/hod/admin)
                        if role in ("faculty", "hod", "admin"):
                            new_members = st.text_input("Update members (comma-separated student IDs)", value=g.get("members", ""), key=f"upd_members_{g['id']}")
                            if st.button("Update members", key=f"upd_btn_{g['id']}"):
                                try:
                                    r = requests.patch(f"{API}/projects/{proj['id']}/groups/{g['group_id']}", headers=h, params={"members": new_members.strip()}, timeout=10)
                                    if r.ok:
                                        st.success("Members updated.")
                                        st.rerun()
                                    else:
                                        st.error(f"{r.status_code}: {r.text[:300]}")
                                except Exception as e:
                                    st.error(str(e)[:300])

            # Milestones
            try:
                ms = requests.get(f"{API}/projects/{proj['id']}/milestones", headers=h, timeout=10)
                milestones = ms.json() if ms.ok else []
            except Exception:
                milestones = []
            if milestones:
                st.subheader("Milestones")
                st.dataframe(
                    [{"checkpoint": m.get("checkpoint_id"), "name": m.get("name"), "group": m.get("group_id"), "due": str(m.get("due_date", ""))[:10], "status": m.get("status"), "evidence": "Yes" if m.get("evidence") else "No"} for m in milestones],
                    use_container_width=True,
                )
                # Update milestone status/evidence
                for m in milestones:
                    with st.expander(f"Update {m.get('checkpoint_id')} — {m.get('name')}", expanded=False):
                        status_opts = ["not_started", "in_progress", "submitted", "accepted", "overdue", "blocked"]
                        cur_idx = status_opts.index(m.get("status", "not_started")) if m.get("status") in status_opts else 0
                        new_status = st.selectbox("Status", status_opts, index=cur_idx, key=f"ms_st_{m['id']}")
                        new_evidence = st.text_input("Evidence (link or description)", value=m.get("evidence", "") or "", key=f"ms_ev_{m['id']}")
                        if st.button("Update milestone", key=f"ms_upd_{m['id']}"):
                            try:
                                r = requests.patch(f"{API}/milestones/{m['id']}", headers=h, params={"status": new_status, "evidence": new_evidence.strip() or None}, timeout=10)
                                if r.ok:
                                    st.success("Milestone updated.")
                                    st.rerun()
                                else:
                                    try:
                                        st.error(r.json().get("detail", r.text[:300]))
                                    except Exception:
                                        st.error(f"{r.status_code}: {r.text[:300]}")
                            except Exception as e:
                                st.error(str(e)[:300])
            else:
                st.caption("No milestones defined for this project.")

            # Edit project (faculty/hod/admin, own project)
            if role in ("faculty", "hod", "admin"):
                with st.expander("Edit project", expanded=False):
                    new_title = st.text_input("Title", value=proj.get("title", ""), key=f"edit_title_{proj['id']}")
                    new_desc = st.text_area("Description", value=proj.get("description") or "", key=f"edit_desc_{proj['id']}")
                    if st.button("Save changes", key=f"edit_proj_{proj['id']}"):
                        try:
                            r = requests.patch(f"{API}/projects/{proj['id']}", headers=h, params={"title": new_title.strip() or None, "description": new_desc.strip() or None}, timeout=10)
                            if r.ok:
                                st.success("Project updated.")
                                st.rerun()
                            else:
                                st.error(f"{r.status_code}: {r.text[:300]}")
                        except Exception as e:
                            st.error(str(e)[:300])

# --- Create project (faculty/hod/admin) ---
if role in ("faculty", "hod", "admin"):
    st.divider()
    st.subheader("Create a project")
    with st.form("new_project"):
        p_course = st.text_input("Course code")
        p_title = st.text_input("Project title")
        p_desc = st.text_area("Description (optional)")
        if st.form_submit_button("Create project"):
            if not p_course.strip() or not p_title.strip():
                st.error("Course code and title are required.")
            else:
                try:
                    r = requests.post(f"{API}/projects", headers=h, params={"course_code": p_course.strip(), "title": p_title.strip(), "description": p_desc.strip() or None}, timeout=15)
                    if r.ok:
                        st.success(f"Project created (ID: {r.json().get('id')}).")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])

    # --- Create group ---
    st.divider()
    st.subheader("Create a group")
    with st.form("new_group"):
        g_project = st.number_input("Project ID", min_value=1, step=1, value=1)
        g_id = st.text_input("Group ID (e.g. G1)")
        g_members = st.text_input("Members (comma-separated student IDs)")
        if st.form_submit_button("Create group"):
            if not g_id.strip():
                st.error("Group ID is required.")
            else:
                try:
                    r = requests.post(f"{API}/projects/{int(g_project)}/groups", headers=h, params={"group_id": g_id.strip(), "members": g_members.strip()}, timeout=15)
                    if r.ok:
                        st.success(f"Group {g_id.strip()} created.")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])

    # --- Create milestone ---
    st.divider()
    st.subheader("Create a milestone")
    with st.form("new_milestone"):
        m_project = st.number_input("Project ID", min_value=1, step=1, value=1, key="m_proj")
        m_group = st.text_input("Group ID", key="m_group")
        m_checkpoint = st.text_input("Checkpoint ID (e.g. M1)", key="m_cp")
        m_name = st.text_input("Name", key="m_name")
        m_due = st.text_input("Due date (YYYY-MM-DD)", key="m_due")
        m_deliv = st.text_area("Expected deliverable", key="m_deliv")
        if st.form_submit_button("Create milestone"):
            if not m_group.strip() or not m_checkpoint.strip() or not m_name.strip() or not m_due.strip():
                st.error("Group ID, checkpoint ID, name, and due date are required.")
            else:
                try:
                    r = requests.post(f"{API}/projects/{int(m_project)}/milestones", headers=h, params={
                        "group_id": m_group.strip(),
                        "checkpoint_id": m_checkpoint.strip(),
                        "name": m_name.strip(),
                        "due_date": m_due.strip(),
                        "expected_deliverable": m_deliv.strip(),
                    }, timeout=15)
                    if r.ok:
                        st.success(f"Milestone {m_checkpoint.strip()} created.")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])
