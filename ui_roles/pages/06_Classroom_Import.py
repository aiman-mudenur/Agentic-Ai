import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, live_refresh, metric_card

st.title("Import from Google Classroom")
st.caption("Import coursework, materials, and announcements from a connected Google Classroom course.")

if st.session_state.get("role") != "faculty" or not st.session_state.get("token"):
    st.warning("Login as Faculty first (Faculty Dashboard page).")
    st.stop()
h = {"Authorization": f"Bearer {st.session_state['token']}"}
live_refresh("classroom")

try:
    stt = requests.get(f"{API}/classroom/status", headers=h, timeout=10)
    status = stt.json() if stt.ok else {"connected": False}
except Exception as e:
    st.error(str(e)[:300])
    st.stop()

if not status.get("connected"):
    st.info("Connect your Google account (read-only Classroom scopes) to import coursework, materials and announcements.")
    try:
        cu = requests.get(f"{API}/classroom/connect-url", headers=h, timeout=10)
        if cu.ok:
            st.link_button("Connect Google Classroom", cu.json()["auth_url"], key="gc_link")
            st.caption("Google approves -> token is stored server-side under data/tokens/ (never committed). Then press Refresh.")
        else:
            try:
                st.error(cu.json().get("detail", cu.text[:500]))
            except Exception:
                st.error(cu.text[:500])
    except Exception as e:
        st.error(str(e)[:300])
    if st.button("Refresh status", key="gc_status_r"):
        st.rerun()
    st.stop()

st.success("Google Classroom connected.")
missing = status.get("missing_scopes") or []
if missing:
    st.warning("Token is missing scopes: " + ", ".join(m.split("/")[-1] for m in missing)
               + ". Disconnect and reconnect to grant them.")
if st.button("Disconnect Classroom", key="gc_disconnect"):
    r = requests.post(f"{API}/classroom/disconnect", headers=h, timeout=10)
    if r.ok:
        st.success("Disconnected. Connect again to grant the current scopes.")
        st.rerun()
    else:
        st.error(f"{r.status_code}: {r.text[:300]}")

try:
    cr = requests.get(f"{API}/classroom/courses", headers=h, timeout=20)
    if cr.ok:
        courses = cr.json()
    else:
        st.error(f"{cr.status_code}: {cr.text[:300]}")
        courses = None
except Exception as e:
    st.error(str(e)[:300])
    courses = None
if courses is None:
    st.stop()
if not courses:
    empty_state("No Classroom courses found for this Google account.", title="Classroom courses")
    st.stop()

options = [(c.get("id"), c.get("name", c.get("id"))) for c in courses if c.get("id")]
sel = st.selectbox("Classroom course", options, format_func=lambda x: f"{x[1]} ({x[0]})")
if sel:
    cid = sel[0]
    bundle: dict = {}
    try:
        work = requests.get(f"{API}/classroom/courses/{cid}/work", headers=h, timeout=30)
        if work.ok:
            bundle = work.json()
        else:
            try:
                st.error(f"Could not read course: {work.json().get('detail', work.text[:400])}")
            except Exception:
                st.error(f"Could not read course ({work.status_code}).")
    except Exception as e:
        st.error(str(e)[:300])
    if bundle:
        for part, msg in (bundle.get("warnings") or {}).items():
            st.warning(f"{part}: {str(msg)[:300]}")
        cw = bundle.get("coursework", [])
        mats = bundle.get("materials", [])
        anns = bundle.get("announcements", [])
        c1, c2, c3 = st.columns(3)
        with c1:
            metric_card("Coursework items", len(cw))
        with c2:
            metric_card("Materials", len(mats))
        with c3:
            metric_card("Announcements", len(anns))
        with st.expander(f"Coursework ({len(cw)})", expanded=True):
            if not cw:
                st.caption("No coursework found" + (" — see warning above." if (bundle.get("warnings") or {}).get("coursework") else "."))
            for w in cw[:30]:
                st.write(f"{w.get('title')} — max {w.get('maxPoints') or '—'} — due {w.get('dueDate') or '—'}")
        with st.expander(f"Materials ({len(mats)})", expanded=True):
            if not mats:
                st.caption("No courseWorkMaterials found" + (" — see warning above." if (bundle.get("warnings") or {}).get("materials") else ". Check Announcements below — teachers often post there."))
            for m in mats[:30]:
                line = f"{m.get('title')}"
                if m.get("link"):
                    st.markdown(f"{line} — [open]({m['link']})")
                else:
                    st.write(line)
                if m.get("drive_files"):
                    st.caption(f"{len(m['drive_files'])} file(s): " + ", ".join(f.get("title", "?") for f in m["drive_files"][:5]))
        with st.expander(f"Announcements ({len(anns)})", expanded=True):
            if not anns:
                st.caption("No announcements found" + (" — see warning above." if (bundle.get("warnings") or {}).get("announcements") else "."))
            for a in anns[:30]:
                st.write(f"{(a.get('text') or '')[:280]}")
        st.divider()
        st.subheader("Import into portal")
        with st.form("gc_import"):
            suggested = (sel[1] or "").strip()
            code = st.text_input("Local course code (created if missing)", value=suggested)
            st.caption("Short code like CS_AI2027 — filter by this exact code in Notes/Assignments.")
            imp_cw = st.checkbox("Import coursework as assignments", value=True)
            imp_m = st.checkbox("Import materials & announcements as notes (Drive PDFs are downloaded with real file bytes)", value=True)
            pub = st.checkbox("Publish assignments immediately", value=True)
            if st.form_submit_button("Import"):
                if not code.strip():
                    st.error("Enter a local course code.")
                else:
                    try:
                        r = requests.post(f"{API}/classroom/import", headers=h, json={
                            "classroom_course_id": cid, "course_code": code.strip(),
                            "import_coursework": bool(imp_cw), "import_materials": bool(imp_m),
                            "publish": bool(pub)}, timeout=120)
                        if r.ok:
                            d = r.json()
                            st.success(f"Imported. {d['assignments_created']} assignments, {d['notes_created']} notes "
                                       f"({d.get('files_downloaded', 0)} files with bytes, {d.get('announcements_imported', 0)} announcements) "
                                       f"into {d['course_code']}")
                            if d.get("file_failures"):
                                st.warning("Some files failed: " + "; ".join(d["file_failures"][:5]))
                        else:
                            try:
                                st.error(r.json().get("detail", r.text[:500]))
                            except Exception:
                                st.error(r.text[:500])
                    except Exception as e:
                        st.error(str(e)[:300])
