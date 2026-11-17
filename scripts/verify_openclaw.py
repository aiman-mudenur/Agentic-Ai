"""End-to-end smoke test for the OpenClaw AI Course Assistant.

Run from the repo root with the repo venv:

    .venv\\Scripts\\python.exe scripts\\verify_openclaw.py
    .venv\\Scripts\\python.exe scripts\\verify_openclaw.py "List my assignments"

It generates a short-lived JWT for the first active faculty account (no password
needed), calls the API health/tools/approvals/chat endpoints, and prints concise
results. No tokens or secrets are printed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlmodel import Session, select

from api.security import create_access_token
from db.engine import engine
from models.auth import User

BASE = "http://127.0.0.1:8000"


def main() -> int:
    message = " ".join(sys.argv[1:]) or "Reply with exactly: COURSE-ASSISTANT-OK"

    with Session(engine) as session:
        faculty = session.exec(
            select(User).where(User.is_active == True, User.role == "faculty")  # noqa: E712
        ).first()
    if not faculty:
        print("No active faculty user found; seed demo data first.")
        return 1

    headers = {
        "Authorization": "Bearer "
        + create_access_token({"sub": faculty.email, "role": faculty.role})
    }

    def get(path: str, timeout: int = 30):
        try:
            r = requests.get(BASE + path, headers=headers, timeout=timeout)
            return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
        except Exception as exc:
            return None, str(exc)[:200]

    def post(path: str, payload: dict, timeout: int = 320):
        try:
            r = requests.post(BASE + path, headers=headers, json=payload, timeout=timeout)
            return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
        except Exception as exc:
            return None, str(exc)[:200]

    print("API health:", *get("/health"))
    status, health = get("/openclaw/health")
    if isinstance(health, dict):
        print(
            "OpenClaw health:",
            status,
            "| status:",
            health.get("status"),
            "| connected:",
            health.get("connected"),
            "| configured:",
            health.get("configured"),
        )
        if health.get("error"):
            print("  error:", health["error"])
    else:
        print("OpenClaw health:", status, health)

    status, tools = get("/openclaw/tools")
    if isinstance(tools, dict):
        print("Allowed tools:", status, ", ".join(tools.get("allowed_tools", [])))
    else:
        print("Allowed tools:", status, tools)

    status, approvals = get("/openclaw/approvals")
    print("Pending approvals:", status, len(approvals) if isinstance(approvals, list) else approvals)

    started = time.time()
    status, chat = post("/openclaw/chat", {"message": message})
    elapsed = time.time() - started
    print(f"Chat ({elapsed:.1f}s):", status)
    if isinstance(chat, dict):
        print("  reply:", str(chat.get("reply") or chat.get("detail") or "")[:500])
        print("  state:", chat.get("state"), "| error:", chat.get("error"))
    else:
        print(" ", chat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
