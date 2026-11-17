"""Send one-time Gmail login codes (stdlib smtplib only, no new dependency).

Configure via environment (.env, never committed):
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google App Password, not the login password
    SMTP_FROM=you@gmail.com                 # optional, defaults to SMTP_USER

When not configured, callers fall back to showing the code on-screen.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_USER") and os.getenv("SMTP_APP_PASSWORD"))


def send_login_code(to_email: str, code: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_APP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user)
    if not user or not pwd:
        raise RuntimeError("Email sending is not configured (set SMTP_USER / SMTP_APP_PASSWORD).")
    msg = EmailMessage()
    msg["Subject"] = "Your Course Organizer sign-in code"
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(
        "Your Course Organizer verification code is:\n\n"
        f"    {code}\n\n"
        "Enter it in the app within 5 minutes. "
        "If you did not request this, ignore this email."
    )
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
