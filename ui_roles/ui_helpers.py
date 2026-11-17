"""Shared Streamlit helpers (kept outside pages/ so it never appears in nav)."""

from __future__ import annotations

import html
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
    return status


def inject_theme_css() -> None:
    """Inject the shared dark-professional CSS once per Streamlit session.

    Idempotent: repeated calls within the same session are no-ops. CSS uses
    stable Streamlit data-testid selectors and co- prefixed classes. It does
    not interfere with widgets or navigation.
    """
    if st.session_state.get("_co_theme_css_injected"):
        return
    st.markdown(
        """
        <style>
        :root {
            --co-navy: #0A0F1C;
            --co-navy-light: #1E293B;
            --co-blue: #729BFF;
            --co-blue-dark: #5B8DEF;
            --co-blue-light: #1E3A5F;
            --co-teal: #45C4B0;
            --co-slate: #9AAAC0;
            --co-muted: #6B7B93;
            --co-bg: #0B1120;
            --co-surface: #111827;
            --co-surface-alt: #172235;
            --co-border: #29364A;
            --co-border-strong: #3B4D6B;
            --co-radius-sm: 6px;
            --co-radius-md: 10px;
            --co-radius-lg: 14px;
            --co-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.2);
            --co-shadow-md: 0 4px 12px rgba(0, 0, 0, 0.3);
            --co-space-xs: 4px;
            --co-space-sm: 8px;
            --co-space-md: 16px;
            --co-space-lg: 24px;
            --co-space-xl: 32px;
        }

        /* ---- Page background ---- */
        .stApp,
        [data-testid="stAppViewContainer"] {
            background-color: var(--co-bg);
        }

        /* ---- Sidebar: deep navy shell ---- */
        section[data-testid="stSidebar"] {
            background-color: var(--co-navy);
            border-right: 1px solid var(--co-border);
        }
        section[data-testid="stSidebar"] * {
            color: #CBD5E1;
        }
        section[data-testid="stSidebar"] a,
        section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] {
            color: #94A3B8;
            border-radius: var(--co-radius-sm);
            padding: 6px 10px;
            transition: background-color 0.15s ease, color 0.15s ease;
        }
        section[data-testid="stSidebar"] a:hover,
        section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]:hover {
            background-color: rgba(255, 255, 255, 0.06);
            color: #E2E8F0;
        }
        section[data-testid="stSidebar"] [aria-current="page"] {
            background-color: rgba(114, 155, 255, 0.15);
            color: #FFFFFF;
            font-weight: 600;
        }
        section[data-testid="stSidebar"] .stCaption,
        section[data-testid="stSidebar"] small {
            color: #64748B;
        }
        section[data-testid="stSidebar"] hr {
            border-color: rgba(255, 255, 255, 0.08);
        }

        /* ---- Typography ---- */
        h1, h2, h3, h4 {
            color: var(--co-text);
            font-weight: 700;
            letter-spacing: -0.01em;
        }
        h1 { font-size: 1.75rem; margin-bottom: var(--co-space-sm); }
        h2 { font-size: 1.35rem; margin-top: var(--co-space-lg); margin-bottom: var(--co-space-sm); }
        h3 { font-size: 1.1rem; margin-top: var(--co-space-md); margin-bottom: var(--co-space-xs); }
        p, .stMarkdown, .stCaption {
            color: var(--co-slate);
            line-height: 1.55;
        }
        .stCaption {
            color: var(--co-muted);
            font-size: 0.875rem;
        }

        /* ---- Bordered containers / cards ---- */
        [data-testid="stVerticalBlockBorderWrapper"] {
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-md);
            background-color: var(--co-surface);
            box-shadow: var(--co-shadow-sm);
            padding: var(--co-space-md);
        }
        .co-card, .co-metric, .co-empty {
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-md);
            background-color: var(--co-surface);
            padding: var(--co-space-md);
            box-shadow: var(--co-shadow-sm);
        }
        .co-pill {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: 600;
            line-height: 1.4;
        }

        /* ---- Buttons ---- */
        .stButton > button,
        button[kind="primary"],
        button[kind="secondary"] {
            border-radius: var(--co-radius-sm);
            font-weight: 600;
            font-size: 0.875rem;
            padding: 6px 16px;
            transition: all 0.15s ease;
            border: 1px solid var(--co-border);
            background-color: var(--co-surface-alt);
            color: var(--co-text);
        }
        .stButton > button:hover {
            border-color: var(--co-blue);
            color: var(--co-blue);
            background-color: rgba(114, 155, 255, 0.1);
        }
        .stButton > button[kind="primary"],
        button[kind="primary"] {
            background-color: var(--co-blue);
            color: #0B1120;
            border-color: var(--co-blue);
            box-shadow: 0 1px 3px rgba(114, 155, 255, 0.25);
        }
        .stButton > button[kind="primary"]:hover {
            background-color: var(--co-blue-dark);
            border-color: var(--co-blue-dark);
            box-shadow: 0 2px 6px rgba(114, 155, 255, 0.3);
        }
        .stButton > button:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        .stButton > button:focus-visible {
            outline: 2px solid var(--co-blue);
            outline-offset: 2px;
        }

        /* ---- Form inputs ---- */
        .stTextInput input,
        .stTextArea textarea,
        .stNumberInput input,
        .stSelectbox select,
        .stDateInput input {
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-sm);
            background-color: var(--co-surface-alt);
            color: var(--co-text);
            font-size: 0.875rem;
            transition: border-color 0.15s ease, box-shadow 0.15s ease;
        }
        .stTextInput input:focus,
        .stTextArea textarea:focus,
        .stNumberInput input:focus,
        .stSelectbox select:focus,
        .stDateInput input:focus {
            border-color: var(--co-blue);
            box-shadow: 0 0 0 3px rgba(114, 155, 255, 0.15);
            outline: none;
        }

        /* ---- Expanders ---- */
        [data-testid="stExpander"] {
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-md);
            background-color: var(--co-surface);
            overflow: hidden;
        }
        [data-testid="stExpander"] summary {
            font-weight: 600;
            color: var(--co-text);
            padding: var(--co-space-sm) var(--co-space-md);
        }
        [data-testid="stExpander"] summary:hover {
            background-color: var(--co-surface-alt);
        }

        /* ---- Dataframes ---- */
        [data-testid="stDataFrame"] {
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-md);
            overflow: hidden;
        }

        /* ---- Tabs ---- */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: var(--co-space-xs);
            border-bottom: 1px solid var(--co-border);
        }
        [data-testid="stTabs"] [data-baseweb="tab"] {
            border-radius: var(--co-radius-sm) var(--co-radius-sm) 0 0;
            padding: 8px 16px;
            font-weight: 500;
            color: var(--co-muted);
            transition: color 0.15s ease, background-color 0.15s ease;
        }
        [data-testid="stTabs"] [data-baseweb="tab"]:hover {
            color: var(--co-text);
            background-color: var(--co-surface-alt);
        }
        [data-testid="stTabs"] [aria-selected="true"] {
            color: var(--co-blue);
            font-weight: 600;
            border-bottom: 2px solid var(--co-blue);
        }

        /* ---- Alerts ---- */
        [data-testid="stAlert"] {
            border-radius: var(--co-radius-md);
            border: 1px solid var(--co-border);
            padding: var(--co-space-sm) var(--co-space-md);
        }

        /* ---- Metrics ---- */
        [data-testid="stMetric"] {
            background-color: var(--co-surface);
            border: 1px solid var(--co-border);
            border-radius: var(--co-radius-md);
            padding: var(--co-space-sm) var(--co-space-md);
        }
        [data-testid="stMetricLabel"] {
            color: var(--co-muted);
            font-size: 0.8rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        [data-testid="stMetricValue"] {
            color: var(--co-text);
            font-weight: 700;
        }

        /* ---- Dividers ---- */
        hr {
            border-color: var(--co-border);
            margin: var(--co-space-md) 0;
        }

        /* ---- Topbar spacing ---- */
        .stApp header {
            background-color: transparent;
        }

        /* ---- Faculty Courses: reduce top padding ---- */
        [data-testid="stMainBlockContainer"] {
            padding-top: 1.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["_co_theme_css_injected"] = True


def metric_card(label: str, value, delta=None) -> None:
    """Compact metric display using a bordered native container + st.metric."""
    try:
        container = st.container(border=True)
    except (TypeError, AttributeError):
        container = st.container()
    with container:
        st.metric(label=label, value=value, delta=delta)


def empty_state(message: str, title: str | None = None) -> None:
    """Clear empty-state message using native Streamlit components."""
    with st.container(border=True):
        if title:
            st.subheader(title)
        st.caption(message)


def status_pill(status: str) -> None:
    """Colored status indicator. Dynamic text is HTML-escaped.

    Renders via st.markdown with unsafe_allow_html=True. The existing
    status_badge() is left untouched for backward compatibility.
    """
    s = (status or "").strip().lower()
    if s in {"graded", "approved", "published", "complete", "completed", "passed", "ok", "success"}:
        bg, fg = "#1A2E23", "#4ADE80"
    elif s in {"submitted", "draft_graded", "in_progress", "pending", "running", "reviewing"}:
        bg, fg = "#1A2540", "#729BFF"
    elif s in {"rejected", "refused", "failed", "error", "late", "overdue"}:
        bg, fg = "#2E1A1A", "#F87171"
    else:
        bg, fg = "#1E293B", "#9AAAC0"
    st.markdown(
        f'<span class="co-pill" style="background-color:{bg};color:{fg};">{html.escape(str(status))}</span>',
        unsafe_allow_html=True,
    )


def card(title: str, body: str) -> None:
    """Simple native-component card wrapper (no arbitrary HTML)."""
    with st.container(border=True):
        st.subheader(title)
        st.markdown(body)


def topbar() -> None:
    """Top-right auth control for every page: identity + Logout, or Login."""
    inject_theme_css()
    try:
        _, right = st.columns([7, 3])
    except Exception:
        return
    with right:
        tok = st.session_state.get("token")
        if tok:
            st.caption(f"{st.session_state.get('email', '')} ({st.session_state.get('role', '')})")
            if st.button("Logout", key=f"logout_{st.session_state.get('role', 'x')}"):
                for k in ("token", "role", "roles", "email"):
                    st.session_state.pop(k, None)
                st.rerun()
        else:
            if st.button("Login", key="topbar_login"):
                try:
                    st.switch_page("pages/Login.py")
                except Exception:
                    st.info("Open the Login page from the sidebar.")


# Role -> [(feature label, page path)]. Single source for the post-login hub.
ROLE_FEATURES: dict[str, list[tuple[str, str]]] = {
    "student": [
        ("Projects and Milestones", "pages/16_Projects.py"),
        ("Student Dashboard", "pages/08_Student_Dashboard.py"),
        ("My Assignments", "pages/09_Student_Assignments.py"),
        ("My Submissions", "pages/10_Student_Submissions.py"),
        ("Notes", "pages/11_Student_Notes.py"),
        ("My Progress", "pages/12_Student_Progress.py"),
        ("Course Catalog", "pages/15_Course_Catalog.py"),
    ],
    "faculty": [
        ("Planner (Lab 1)", "pages/Planner.py"),
        ("Projects and Milestones", "pages/16_Projects.py"),
        ("Faculty Dashboard", "pages/01_Faculty_Dashboard.py"),
        ("Courses", "pages/02_Faculty_Courses.py"),
        ("Notes & Materials", "pages/03_Faculty_Units_Notes.py"),
        ("Assignments", "pages/04_Faculty_Assignments.py"),
        ("Submissions & Grading", "pages/05_Faculty_Submissions.py"),
        ("Classroom Import", "pages/06_Classroom_Import.py"),
        ("AI Course Assistant", "pages/17_OpenClaw_Assistant.py"),
        ("Agent Activity", "pages/07_Agent_Activity.py"),
        ("Course Catalog", "pages/15_Course_Catalog.py"),
    ],
    "hod": [
        ("Planner (Lab 1)", "pages/Planner.py"),
        ("Projects and Milestones", "pages/16_Projects.py"),
        ("HoD Dashboard", "pages/14_HoD_Dashboard.py"),
        ("AI Course Assistant", "pages/17_OpenClaw_Assistant.py"),
        ("Agent Activity", "pages/07_Agent_Activity.py"),
        ("Course Catalog", "pages/15_Course_Catalog.py"),
    ],
    "admin": [
        ("Planner (Lab 1)", "pages/Planner.py"),
        ("Projects and Milestones", "pages/16_Projects.py"),
        ("HoD Dashboard", "pages/14_HoD_Dashboard.py"),
        ("AI Course Assistant", "pages/17_OpenClaw_Assistant.py"),
        ("Agent Activity", "pages/07_Agent_Activity.py"),
        ("Course Catalog", "pages/15_Course_Catalog.py"),
    ],
}

ROLE_HOME: dict[str, str] = {
    "student": "pages/08_Student_Dashboard.py",
    "faculty": "pages/01_Faculty_Dashboard.py",
    "hod": "pages/14_HoD_Dashboard.py",
    "admin": "pages/14_HoD_Dashboard.py",
}


def role_switcher() -> None:
    """Let an account granted several roles choose which one it is acting as.

    The list of choices comes from the API, which reads it from the database,
    and the API refuses any role the account does not hold -- so this control
    can only ever move between granted roles, never widen its own access.
    """
    roles = [str(r) for r in (st.session_state.get("roles") or []) if r]
    if len(roles) < 2:
        return
    current = st.session_state.get("role", roles[0])
    if current not in roles:
        current = roles[0]
    st.divider()
    st.subheader("You hold more than one role")
    st.caption(f"Currently acting as **{current}**. Switch to use your other role's features.")
    pick = st.selectbox("Act as", options=roles, index=roles.index(current), key="role_pick")
    if pick != current and st.button(f"Switch to {pick.title()}", key="role_switch_btn"):
        try:
            r = requests.post(f"{API}/auth/switch-role", json={"role": pick},
                              headers=token_headers(), timeout=10)
        except Exception as e:
            st.error(str(e)[:300])
            return
        if not r.ok:
            st.error(f"{r.status_code}: {r.text[:300]}")
            return
        t = r.json()
        st.session_state["token"] = t["access_token"]
        st.session_state["role"] = t["role"]
        st.session_state["roles"] = t.get("roles", roles)
        st.success(f"Now acting as {t['role']}. Pick a feature below.")
        st.rerun()


def feature_hub(key_prefix: str = "hub") -> None:
    """Post-login hub: success msg + role feature list + user-chosen redirect."""
    role = st.session_state.get("role", "student")
    email = st.session_state.get("email", "")
    st.success(f"Logged in as {email} ({role}). Choose where to go:")
    role_switcher()
    features = ROLE_FEATURES.get(role, ROLE_FEATURES["student"])
    st.subheader("Features available for your role")
    cols = st.columns(2)
    for i, (label, path) in enumerate(features):
        with cols[i % 2]:
            if st.button(label, key=f"{key_prefix}_{i}"):
                try:
                    st.switch_page(path)
                except Exception:
                    st.info(f"Open {label} from the sidebar.")


def google_login_block(role: str, key_prefix: str) -> None:
    """Realtime Gmail login: Google consent -> one-time code -> our JWT."""
    with st.expander("Sign in with Google (real Gmail account)", expanded=False):
        try:
            r = requests.get(f"{API}/google/login-url", params={"purpose": "login", "role": role}, timeout=10)
        except Exception as e:
            st.error(f"API not reachable: {str(e)[:200]}")
            return
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text[:300])
            except Exception:
                detail = r.text[:300]
            st.error(detail)
            return
        try:
            auth_url = r.json()["auth_url"]
        except Exception:
            st.error("Unexpected response from API.")
            return
        st.link_button("Continue with Google", auth_url, key=f"{key_prefix}_glink")
        st.caption("Google approves -> copy the code shown -> paste it below (expires in 5 minutes).")
        code = st.text_input("Paste code from Google", key=f"{key_prefix}_gcode")
        if st.button("Verify Google code", key=f"{key_prefix}_gverify"):
            if not (code or "").strip():
                st.error("Paste the code first.")
                return
            try:
                xr = requests.post(f"{API}/google/exchange", json={"code": code.strip()}, timeout=15)
            except Exception as e:
                st.error(str(e)[:300])
                return
            if not xr.ok:
                st.error(f"{xr.status_code}: {xr.text[:300]}")
                return
            t = xr.json()
            st.session_state["token"] = t["access_token"]
            st.session_state["role"] = t["role"]
            st.session_state["roles"] = t.get("roles") or [t["role"]]
            st.session_state["email"] = t.get("email", "")
            st.success(f"Signed in with Google as {t.get('email')}")
            st.rerun()
