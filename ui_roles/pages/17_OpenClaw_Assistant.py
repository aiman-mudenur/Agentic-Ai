import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, empty_state, metric_card, status_pill

st.title("AI Course Assistant (OpenClaw)")
st.caption("Optional OpenClaw orchestration layer. Restricted tools only; grades/publishing require human approval.")

if st.session_state.get("role") not in ("faculty", "hod", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty, HoD, or Admin first.")
    st.stop()

h = {"Authorization": f"Bearer {st.session_state['token']}"}

# --------------------------------------------------------------------- health
with st.container(border=True):
    st.subheader("Connection status")
    if st.button("Check OpenClaw health", key="oc_health_btn"):
        try:
            hr = requests.get(f"{API}/openclaw/health", headers=h, timeout=20)
            if hr.ok:
                st.session_state["oc_health"] = hr.json()
            else:
                st.error(f"{hr.status_code}: {hr.text[:300]}")
                st.session_state["oc_health"] = None
        except Exception as e:
            st.error(str(e)[:300])
            st.session_state["oc_health"] = None

    health = st.session_state.get("oc_health")
    if health:
        c1, c2, c3 = st.columns(3)
        with c1:
            metric_card("Configured", "Yes" if health.get("configured") else "No")
        with c2:
            metric_card("Connected", "Yes" if health.get("connected") else "No")
        with c3:
            metric_card("Status", health.get("status") or "unknown")
        st.caption(f"Gateway: {health.get('gateway_url')} · protocol {health.get('protocol', 'n/a')}")
        cfg = health.get("config") or {}
        if cfg:
            st.caption("Allowed tools: " + ", ".join(cfg.get("allowed_tools", [])))
        if health.get("error"):
            st.error(f"Gateway error: {health.get('error')}")
        if health.get("status") in ("not_configured", "error", "not_connected"):
            st.info(
                "OpenClaw is not active yet. Set OPENCLAW_ENABLED=true, OPENCLAW_GATEWAY_URL and "
                "OPENCLAW_GATEWAY_TOKEN in .env, then restart the API. See docs/openclaw_integration.md."
            )
        elif health.get("connected"):
            st.success("OpenClaw Gateway is connected and the assistant is active.")
    else:
        st.caption("Click **Check OpenClaw health** to see whether the assistant is active.")

# --------------------------------------------------------------------- chat
st.divider()
st.subheader("Ask the assistant")
st.caption("Example: “List my assignments for CS404”, “Which submissions are late?”, “Summarize notes for CS201”.")

with st.form("oc_chat_form"):
    msg = st.text_area("Your question", key="oc_msg", placeholder="Ask about assignments, submissions, GitHub activity, or notes…")
    if st.form_submit_button("Send"):
        if not msg.strip():
            st.error("Type a question first.")
        else:
            try:
                cr = requests.post(f"{API}/openclaw/chat", headers=h, json={"message": msg.strip()}, timeout=320)
                if cr.ok:
                    data = cr.json()
                    st.session_state["oc_last_response"] = data
                    st.success("Assistant response received.")
                else:
                    try:
                        detail = cr.json().get("detail", cr.text[:500])
                    except Exception:
                        detail = f"{cr.status_code}: {cr.text[:500]}"
                    if "model" in detail.lower():
                        detail += (
                            " Fix: open the OpenClaw Control UI (or the OpenClaw desktop app) → "
                            "Model → Default model and choose a supported model, e.g. "
                            "openai/gpt-6-luna."
                        )
                    st.error(detail)
            except Exception as e:
                st.error(str(e)[:300])

resp = st.session_state.get("oc_last_response")
if resp:
    st.markdown("**Response**")
    if resp.get("error"):
        st.error(resp.get("error"))
    # The API returns only friendly fields: {ok, reply, state}.
    text = resp.get("reply") or resp.get("text") or resp.get("message") or resp.get("summary")
    if text:
        st.markdown(str(text))
    else:
        st.info("The assistant did not return a readable answer. Please try again.")

# --------------------------------------------------------------------- tools
st.divider()
st.subheader("Restricted tools")
try:
    tr = requests.get(f"{API}/openclaw/tools", headers=h, timeout=10)
    if tr.ok:
        tools = tr.json()
        st.caption("Allowlisted: " + ", ".join(tools.get("allowed_tools", [])))
        st.warning("Grade/publishing tools only create a pending approval. A human must approve it below.")
    else:
        st.error(f"{tr.status_code}: {tr.text[:300]}")
except Exception as e:
    st.error(str(e)[:300])

# --------------------------------------------------------------------- approvals
st.divider()
st.subheader("Pending approvals")
try:
    ar = requests.get(f"{API}/openclaw/approvals", headers=h, timeout=10)
    if ar.ok:
        approvals = ar.json()
    else:
        st.error(f"{ar.status_code}: {ar.text[:300]}")
        approvals = []
except Exception as e:
    st.error(str(e)[:300])
    approvals = []

if not approvals:
    empty_state("No pending approvals.", title="Approvals")
else:
    for a in approvals:
        with st.expander(f"#{a['id']} {a['action']} · {a['status']}", expanded=False):
            status_pill(a["status"])
            st.caption(f"Reason: {a.get('reason') or '—'}")
            st.caption(f"Entity: {a.get('entity_type')} #{a.get('entity_id') or '—'}")
            st.caption(f"Requested by user #{a.get('requested_by')} · {str(a.get('created_at'))[:19]}")
            if a.get("payload"):
                st.code(a["payload"], language="json")
            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"Approve #{a['id']}", key=f"oc_app_{a['id']}"):
                    try:
                        rr = requests.post(f"{API}/openclaw/approvals/{a['id']}/approve", headers=h, timeout=20)
                        if rr.ok:
                            st.success(f"Approved #{a['id']}.")
                            st.rerun()
                        else:
                            try:
                                st.error(rr.json().get("detail", rr.text[:400]))
                            except Exception:
                                st.error(f"{rr.status_code}: {rr.text[:400]}")
                    except Exception as e:
                        st.error(str(e)[:300])
            with col2:
                if st.button(f"Reject #{a['id']}", key=f"oc_rej_{a['id']}"):
                    try:
                        rr = requests.post(f"{API}/openclaw/approvals/{a['id']}/reject", headers=h, timeout=20)
                        if rr.ok:
                            st.success(f"Rejected #{a['id']}.")
                            st.rerun()
                        else:
                            try:
                                st.error(rr.json().get("detail", rr.text[:400]))
                            except Exception:
                                st.error(f"{rr.status_code}: {rr.text[:400]}")
                    except Exception as e:
                        st.error(str(e)[:300])
