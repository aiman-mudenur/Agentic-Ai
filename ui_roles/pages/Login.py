import sys
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, ROLE_HOME, feature_hub, google_login_block

st.title("Login")
st.caption("Pick your role tab. After a successful login you see what your role can do, then choose a page.")

# Logged-in users land on their hub (feature list + chosen redirect).
if st.session_state.get("token"):
    feature_hub("login")
    st.stop()

tab_student, tab_faculty, tab_hod, tab_admin = st.tabs(
    ["Login as Student", "Login as Faculty", "Login as HoD", "Login as Admin"]
)


def _go_home(role: str) -> None:
    try:
        st.switch_page(ROLE_HOME.get(role, ROLE_HOME["student"]))
    except Exception:
        st.rerun()


def _password_form(key_prefix: str) -> None:
    u = st.text_input("Email / Username", key=f"{key_prefix}_u")
    p = st.text_input("Password", type="password", key=f"{key_prefix}_p")
    if st.button("Login", key=f"{key_prefix}_login"):
        if not u.strip() or not p:
            st.error("Enter email and password.")
            return
        try:
            r = requests.post(f"{API}/auth/login", data={"username": u.strip(), "password": p}, timeout=10)
        except Exception as e:
            st.error(str(e)[:300])
            return
        if not r.ok:
            st.error(f"{r.status_code}: {r.text[:300]}")
            return
        t = r.json()
        st.session_state["token"] = t["access_token"]
        st.session_state["role"] = t["role"]
        st.session_state["roles"] = t.get("roles") or [t["role"]]
        st.session_state["email"] = t.get("email", u)
        st.success("Logged in — see your features below, then choose a page.")
        st.rerun()


def _signup_form(role: str, key_prefix: str) -> None:
    with st.expander("No account yet? Sign up with your email", expanded=False):
        e = st.text_input("Email id", placeholder="xyz@gmail.com", key=f"{key_prefix}_su_e")
        n = st.text_input("Full name", key=f"{key_prefix}_su_n")
        pw = st.text_input("Password (min 6 chars)", type="password", key=f"{key_prefix}_su_p")
        if st.button("Create account", key=f"{key_prefix}_signup"):
            if not e.strip() or len(pw) < 6:
                st.error("Enter a valid email and a 6+ character password.")
                return
            try:
                r = requests.post(f"{API}/auth/signup",
                                  json={"email": e.strip(), "password": pw, "full_name": n, "role": role},
                                  timeout=10)
            except Exception as ex:
                st.error(str(ex)[:300])
                return
            if not r.ok:
                st.error(f"{r.status_code}: {r.text[:300]}")
                return
            t = r.json()
            st.session_state["token"] = t["access_token"]
            st.session_state["role"] = t["role"]
            st.session_state["roles"] = t.get("roles") or [t["role"]]
            st.session_state["email"] = t.get("email", e)
            st.success("Account created — see your features below, then choose a page.")
            st.rerun()


def _role_tab(role: str, key_prefix: str, blurb: str, allow_signup: bool = True) -> None:
    st.caption(blurb)
    _password_form(key_prefix)
    google_login_block(role if role != "admin" else "hod", key_prefix)
    if allow_signup and role != "admin":
        _signup_form(role, key_prefix)


with tab_student:
    _role_tab("student", "login_stu", "Students: enrolled courses, submissions, notes, approved grades, progress.")

with tab_faculty:
    _role_tab("faculty", "login_fac", "Faculty: assignments, grading queue, notes, course creation, Classroom import.")

with tab_hod:
    _role_tab("hod", "login_hod", "HoD: read-only overview, approvals, audit log.", allow_signup=False)

with tab_admin:
    st.caption("Admin accounts sign in here and are routed by their stored role (HoD overview for hod/admin).")
    _role_tab("admin", "login_adm", "", allow_signup=False)
