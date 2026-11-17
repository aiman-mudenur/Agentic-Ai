"""Secure configuration for the optional OpenClaw Gateway integration.

OpenClaw is an *add-on* orchestration layer. Every setting here is optional and
defaults to a disabled/safe state, so the existing FastAPI + Streamlit app keeps
working when OpenClaw is not installed or not configured.

Protocol: Gateway WebSocket (JSON text frames) with the ``connect`` handshake,
operator role, and ``operator.read`` / ``operator.write`` scopes. See
``docs/openclaw_integration.md`` for the wire reference and setup steps.

Secrets are read from the environment / ``.env`` and are never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class OpenClawConfig:
    enabled: bool
    gateway_url: str
    token: str
    role: str
    scopes: tuple[str, ...]
    client_id: str
    client_version: str
    min_protocol: int
    max_protocol: int
    connect_timeout_s: float
    request_timeout_s: float
    agent_id: Optional[str]
    assistant_prompt: str
    allowed_tools: tuple[str, ...]
    # Device-identity handshake fields (additive; old callers keep working).
    client_mode: str = "cli"
    platform: str = "windows"
    device_family: Optional[str] = None
    device_identity_path: Optional[str] = None
    chat_timeout_s: float = 300.0
    session_key: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.gateway_url and self.token)

    def redacted(self) -> dict:
        """Config summary safe to return to a browser/UI (no token)."""
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "gateway_url": self.gateway_url,
            "role": self.role,
            "scopes": list(self.scopes),
            "agent_id": self.agent_id,
            "protocol": f"{self.min_protocol}-{self.max_protocol}",
            "connect_timeout_s": self.connect_timeout_s,
            "request_timeout_s": self.request_timeout_s,
            "allowed_tools": list(self.allowed_tools),
            "client_id": self.client_id,
            "client_mode": self.client_mode,
            "platform": self.platform,
        }


#: Restricted backend tools exposed to the assistant. This is an allowlist:
#: anything not listed here is rejected even if the model asks for it.
DEFAULT_ALLOWED_TOOLS = (
    "list_assignments",
    "get_submission_status",
    "find_late_submissions",
    "read_github_activity",
    "summarize_course_notes",
)

#: Tools that propose a grade/publish change. They never mutate directly;
#: they create a pending approval that a human must accept.
APPROVAL_REQUIRED_TOOLS = ("set_grade", "approve_grade", "publish_release")

DEFAULT_ASSISTANT_PROMPT = (
    "You are the Course Organizer AI Course Assistant for faculty. "
    "Answer using ONLY the restricted course tools listed below. "
    "Never use memory_search, memory tools, shell, or raw database access: "
    "they are unavailable and must not be attempted. "
    "For questions about assignments, submissions, GitHub activity, or notes, "
    "call the matching tool and report its result in plain language with "
    "course codes, titles, counts, and due dates. "
    "If a question is outside these tools, say so briefly and suggest using "
    "the relevant Course Organizer page instead. "
    "For grade/publishing changes, propose the change and require explicit "
    "human approval before it is applied."
)


def load_openclaw_config() -> OpenClawConfig:
    """Load OpenClaw settings from environment/.env with safe defaults."""
    enabled = _env_bool("OPENCLAW_ENABLED", False)
    gateway_url = (os.getenv("OPENCLAW_GATEWAY_URL") or "ws://127.0.0.1:18789").strip()
    token = (os.getenv("OPENCLAW_GATEWAY_TOKEN") or "").strip()
    role = (os.getenv("OPENCLAW_ROLE") or "operator").strip() or "operator"
    scopes_raw = (os.getenv("OPENCLAW_SCOPES") or "operator.read,operator.write").strip()
    scopes = tuple(s.strip() for s in scopes_raw.split(",") if s.strip()) or ("operator.read",)
    client_id = (os.getenv("OPENCLAW_CLIENT_ID") or "cli").strip()
    client_version = (os.getenv("OPENCLAW_CLIENT_VERSION") or "1.0.0").strip()
    client_mode = (os.getenv("OPENCLAW_CLIENT_MODE") or "cli").strip().lower() or "cli"
    platform = (os.getenv("OPENCLAW_PLATFORM") or "windows").strip() or "windows"
    device_family = (os.getenv("OPENCLAW_DEVICE_FAMILY") or "").strip() or None
    device_identity_path = (os.getenv("OPENCLAW_DEVICE_IDENTITY_PATH") or "").strip() or None
    chat_timeout_s = float(_env_int("OPENCLAW_CHAT_TIMEOUT_MS", 300000) / 1000.0)
    session_key = (os.getenv("OPENCLAW_SESSION_KEY") or "").strip() or None
    min_protocol = _env_int("OPENCLAW_MIN_PROTOCOL", 4)
    max_protocol = _env_int("OPENCLAW_MAX_PROTOCOL", 4)
    connect_timeout_s = float(_env_int("OPENCLAW_CONNECT_TIMEOUT_MS", 10000) / 1000.0)
    request_timeout_s = float(_env_int("OPENCLAW_REQUEST_TIMEOUT_MS", 30000) / 1000.0)
    agent_id = (os.getenv("OPENCLAW_AGENT_ID") or "").strip() or None
    assistant_prompt = (os.getenv("OPENCLAW_ASSISTANT_PROMPT") or DEFAULT_ASSISTANT_PROMPT).strip()
    tools_raw = (os.getenv("OPENCLAW_ALLOWED_TOOLS") or ",".join(DEFAULT_ALLOWED_TOOLS)).strip()
    allowed_tools = tuple(t.strip() for t in tools_raw.split(",") if t.strip()) or DEFAULT_ALLOWED_TOOLS

    return OpenClawConfig(
        enabled=enabled,
        gateway_url=gateway_url,
        token=token,
        role=role,
        scopes=scopes,
        client_id=client_id,
        client_version=client_version,
        min_protocol=min_protocol,
        max_protocol=max_protocol,
        connect_timeout_s=connect_timeout_s,
        request_timeout_s=request_timeout_s,
        agent_id=agent_id,
        assistant_prompt=assistant_prompt,
        allowed_tools=allowed_tools,
        client_mode=client_mode,
        platform=platform,
        device_family=device_family,
        device_identity_path=device_identity_path,
        chat_timeout_s=chat_timeout_s,
        session_key=session_key,
    )


CONFIG = load_openclaw_config()
