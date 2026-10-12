import requests
import streamlit as st

st.title("⚙ Admin")
st.info("Seed users: faculty@gmail.com · student@gmail.com · admin@gmail.com (passwords: faculty123 / student123 / admin123)")
st.caption("Real users sign up from Faculty / Student Dashboard pages with any real email id.")
try:
    r = requests.get("http://127.0.0.1:8000/health", timeout=5)
    st.success("API is reachable ✅") if r.ok else st.error(f"API health: {r.status_code}")
except Exception as e:
    st.error(f"API not reachable. Start Terminal 1 first. ({str(e)[:200]})")
