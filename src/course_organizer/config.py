"""Configuration, loaded from the environment. API keys are NEVER hardcoded.

Every setting has a safe default so that all 8 labs run with an empty `.env`.
Add a key only when you actually have it; the relevant lab will pick it up
without any code change.

Source of truth: `.env` (gitignored) copied from `.env.example`.
"""

from __future__ import annotations

import logging
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Repository root = two levels up from this file (src/course_organizer/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

LLMBackend = Literal["scripted", "live"]
SourceAdapterName = Literal["local", "google"]
EmbeddingBackendName = Literal["fastembed", "hashing"]


class Settings(BaseSettings):
    """Runtime settings for the course organiser.

    Attributes:
        google_api_key: Gemini API key. Only needed when `llm_backend == "live"`.
        gemini_model: Free-tier Gemini model id.
        llm_backend: `"scripted"` (offline, deterministic) or `"live"` (Gemini).
        source_adapter: Which data source backs the connector. Lab 5 proves the
            swap: change this and no agent code changes.
        mcp_connector_token: Shared secret every agent must present to the
            connector. Left as None, the connector generates an ephemeral
            in-memory token for this process; no file is ever written.
        mcp_connector_role: Which role ``mcp_connector_token`` belongs to, and
            therefore which scopes it holds. Read by the connector *server* at
            startup, never as an endpoint argument, so a caller cannot raise its
            own privileges by asking. Defaults to ``course_faculty``.
        embedding_backend: Which similarity backend Lab 4 uses. `"hashing"` is
            the default because it is hermetic -- no 69 MB model download, no
            network, byte-identical scores on every machine, which is what makes
            the test suite and CI reproducible. `"fastembed"` is the learned
            backend and is the one to use for any real similarity screen; see
            :data:`course_organizer.memory.embeddings.TRIAGE_THRESHOLD_HASHING`
            for the measured reason the hashing backend is triage-only.
        runtime_budget_calls: Hard ceiling on LLM calls per run (Lab 6 budgets).
        rate_limit_rpm: Requests-per-minute cap. Free-tier Gemini is ~15.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM ────────────────────────────────────────────────────────────────
    google_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_temperature: float = 0.0
    llm_backend: LLMBackend = "scripted"

    # ── Data source (Lab 5 adapter swap) ───────────────────────────────────
    source_adapter: SourceAdapterName = "local"
    google_oauth_client_id: Optional[str] = None
    google_oauth_client_secret: Optional[str] = None
    google_drive_folder_id: Optional[str] = None
    google_classroom_course_id: Optional[str] = None
    # Where the authorised-user token JSON is cached. Deliberately outside the
    # project tree and never committed: it is a refresh token, which is a
    # long-lived credential, and it sits in the same place a venv would if the
    # project were synced. Lab 5 only reads this when SOURCE_ADAPTER=google.
    google_token_path: Path = Path.home() / ".course-organizer" / "google_token.json"

    # ── Connector (Lab 5) ──────────────────────────────────────────────────
    mcp_connector_token: Optional[str] = None
    mcp_connector_role: str = "course_faculty"

    # ── Similarity (Lab 4) ─────────────────────────────────────────────────
    # Hermetic by default: the hashing backend needs no download and gives the
    # same score on every machine. Switch to "fastembed" for a learned model.
    embedding_backend: EmbeddingBackendName = "hashing"

    # ── Runtime (Lab 6) ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    runtime_budget_calls: int = 40
    rate_limit_rpm: int = 15

    # ── Paths ──────────────────────────────────────────────────────────────
    seed_dir: Path = PROJECT_ROOT / "data" / "seed"
    workspace_dir: Path = PROJECT_ROOT / "data" / "workspace"
    embeddings_cache_dir: Path = PROJECT_ROOT / ".fastembed_cache"

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        """Normalise the log level so `debug` and `DEBUG` both work."""
        return value.upper()

    @field_validator("workspace_dir", "embeddings_cache_dir", mode="before")
    @classmethod
    def _coerce_path(cls, value: object) -> object:
        """Accept a plain string from the environment as a Path."""
        return Path(value) if isinstance(value, str) else value

    # ── Derived helpers ────────────────────────────────────────────────────

    @property
    def audit_dir(self) -> Path:
        """Where the append-only audit log is written (Lab 6)."""
        return self.workspace_dir / "audit"

    @property
    def checkpoint_dir(self) -> Path:
        """Where durable LangGraph checkpoints live (Lab 6)."""
        return self.workspace_dir / "checkpoints"

    @property
    def versions_dir(self) -> Path:
        """Where published, versioned workspaces are written (Lab 7/12)."""
        return self.workspace_dir / "versions"

    @property
    def runs_dir(self) -> Path:
        """Per-run artefacts (one subdirectory per run id)."""
        return self.workspace_dir / "runs"

    def using_live_llm(self) -> bool:
        """True when Gemini calls are wanted and a key is actually present."""
        return self.llm_backend == "live" and bool(self.google_api_key)

    def ensure_directories(self) -> None:
        """Create every runtime directory. Safe to call repeatedly."""
        for directory in (
            self.workspace_dir,
            self.audit_dir,
            self.checkpoint_dir,
            self.versions_dir,
            self.runs_dir,
            self.embeddings_cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def require_live_llm(self) -> str:
        """Return the Gemini key, or explain precisely what is missing.

        Raises:
            RuntimeError: If `live` was requested without a usable key. The
                message names the exact fix instead of failing deep inside the
                provider SDK.
        """
        key = (self.google_api_key or "").strip()
        if not key:
            raise RuntimeError(
                "LLM_BACKEND=live but GOOGLE_API_KEY is empty.\n"
                "  Fix: copy .env.example to .env and set GOOGLE_API_KEY.\n"
                "  Free key (no card needed): https://aistudio.google.com\n"
                "  Or set LLM_BACKEND=scripted to run fully offline."
            )
        return key

    def get_or_create_connector_token(self) -> str:
        """Return the connector token, generating an ephemeral one if absent.

        **No file is written.** This method used to persist a generated token
        into `.env`, and that behaviour was removed in Lab 5 for two reasons,
        both of which are worth stating because the old version looked helpful:

        * A library that edits a developer's configuration file as a side effect
          of being *constructed* is a surprise. A one-line
          ``get_settings()`` at the top of a script would rewrite `.env`, and a
          test run would rewrite it too, racing anything else editing it.
        * `.env` is exactly where a credential must not accumulate. A generated
          token has no value to an attacker and a great deal of value to the
          developer, so writing it buys stability nobody needs and risks losing.

        The trade-off is real and is stated rather than hidden: an ephemeral token
        means a *separately spawned* connector server will not recognise this
        process's token. Lab 5 handles that by passing the token through the
        environment of the server it spawns, and a real deployment sets
        ``MCP_CONNECTOR_TOKEN`` to one value both sides share. An in-process
        client and its connector share the same object anyway, so the labs are
        unaffected.

        Returns:
            The configured token, or a fresh in-memory one.
        """
        if self.mcp_connector_token:
            return self.mcp_connector_token

        token = secrets.token_urlsafe(32)
        self.mcp_connector_token = token
        logger.info(
            "MCP_CONNECTOR_TOKEN is not set; using an ephemeral in-memory "
            "token for this process. Nothing was written to .env. A "
            "separately-spawned connector server will need "
            "MCP_CONNECTOR_TOKEN set to the same value on both sides."
        )
        return token


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that `get_settings()` is cheap and every module sees identical
    configuration. Call `get_settings.cache_clear()` in tests that mutate env.
    """
    settings = Settings()
    if settings.llm_backend == "live" and not settings.google_api_key:
        logger.warning(
            "LLM_BACKEND=live but no GOOGLE_API_KEY found - falling back to the "
            "scripted backend so labs keep running."
        )
    return settings


__all__ = [
    "PROJECT_ROOT",
    "LLMBackend",
    "Settings",
    "SourceAdapterName",
    "get_settings",
]
