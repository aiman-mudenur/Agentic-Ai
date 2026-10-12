"""Shared Streamlit helpers (kept outside pages/ so it never appears in nav)."""

from __future__ import annotations

import time

import requests
import streamlit as st

API = "http://127.0.0.1:8000"


def token_headers() -> dict:
    return {"Authorization": f"Bearer {st.session_state['token']}"}


def require_login(*roles: str) -> bool:
    if not st.session_state.get("token") or st.session_state.get("role") not in roles:
        st.warning("Please login first from the Dashboard page.")
        st.stop()
    return True


def live_refresh(key: str = "live") -> None:
    if st.toggle("Live refresh (10s)", value=False, key=f"{key}_live"):
        time.sleep(10)
        st.rerun()


def safe_list(resp: requests.Response):
    if resp.ok:
        try:
            return resp.json()
        except Exception:
            st.error(resp.text[:500])
            return []
    st.error(f"{resp.status_code}: {resp.text[:500]}")
    return []


def download_button(label: str, kind: str, obj_id: int, filename: str | None, key: str) -> None:
    """Fetch file bytes via API and offer an in-page download/open button."""
    if st.button(label, key=key):
        try:
            r = requests.get(f"{API}/{kind}/{int(obj_id)}/download", headers=token_headers(), timeout=30)
            if r.ok:
                st.download_button(
                    "Save / open file",
                    data=r.content,
                    file_name=filename or f"{kind}-{obj_id}",
                    key=f"{key}_dl",
                )
                ctype = (r.headers.get("content-type") or "").lower()
                if ctype.startswith("text/") or (filename or "").lower().endswith((".txt", ".md", ".csv")):
                    try:
                        st.code(r.content.decode("utf-8", errors="ignore")[:4000])
                    except Exception:
                        pass
            else:
                st.error(f"{r.status_code}: {r.text[:300]}")
        except Exception as e:
            st.error(str(e)[:300])


def status_badge(status: str) -> str:
    icons = {"graded": "✅", "submitted": "📥", "not_started": "⚪", "in_progress": "🔄"}
    return f"{icons.get(status, '•')} {status}"
