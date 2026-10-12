"""The LLM boundary: one protocol, two interchangeable backends.

Why this exists
---------------
The document never names an LLM provider — Chapter 2 is deliberately
language- and vendor-neutral ("Pseudocode is language-neutral"). So the choice
is ours, and we must be able to *change* it without touching a single agent.

Two backends implement the same `LLMClient` protocol:

* :class:`ScriptedLLM` — deterministic, offline, zero-cost, zero-latency
  variance. It does real rule-based extraction and plan assembly; it is not a
  stub that echoes strings. Every lab and every test runs on this by default,
  which is why the project needs no API key to be complete and reproducible.
* :class:`GeminiLLM` — the real model, via `langchain-google-genai`
  (Gemini free tier). Drop-in: same protocol, same schemas.

This separation is also what makes the Lab 8 acceptance criterion measurable —
we can run the parallel/sequential comparison on `scripted` and get the same
verdict every time, then re-run on `live` to see whether it still holds.

Structured output
-----------------
Both backends are asked for a *typed* object (`BaseModel` subclass), never a
prose paragraph. That is what lets Lab 2+ consume a plan, and it is the
concrete difference between an agent pipeline and a chatbot transcript.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from course_organizer.config import Settings
from course_organizer.logging_setup import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the model cannot produce output matching the schema."""


class RateLimiter:
    """A simple requests-per-minute limiter.

    The Gemini free tier allows roughly 15 requests/minute. Lab 8 fans out many
    workers, so without this the parallel run would simply get throttled. Kept
    deliberately small and dependency-free so it is easy to explain in a viva.
    """

    def __init__(self, rpm: int = 15) -> None:
        """Create a limiter allowing `rpm` requests per rolling minute."""
        self._min_interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0

    def acquire(self) -> None:
        """Block until the next request may be issued."""
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed:
                    self._next_allowed = now + self._min_interval
                    return
                wait = self._next_allowed - now
            time.sleep(wait)


class BudgetExceeded(LLMError):
    """Raised when a run exhausts its LLM call budget (Lab 6 "budgets")."""


class LLMClient(ABC):
    """The one interface every agent is allowed to talk to.

    Agents must never construct a provider client directly; they receive an
    `LLMClient`. That is the same discipline the document demands of the data
    layer in Lab 5 ("no agent touches a source directly"), applied to the model
    layer.
    """

    name: str = "abstract"

    def __init__(self, *, max_calls: int = 40) -> None:
        self.max_calls = max_calls
        self.calls_used = 0
        self._budget_lock = threading.Lock()

    def _charge(self, *, step: str) -> None:
        """Count one call against the budget.

        Raises:
            BudgetExceeded: When the budget is spent. Lab 6 turns this into a
                hard stop rather than an unbounded run.
        """
        with self._budget_lock:
            if self.calls_used >= self.max_calls:
                raise BudgetExceeded(
                    f"LLM call budget exhausted ({self.max_calls} calls) at step {step!r}. "
                    "Raise RUNTIME_BUDGET_CALLS in .env, or lower the fan-out."
                )
            self.calls_used += 1

    def budget_remaining(self) -> int:
        """How many calls are left in this run's budget."""
        return max(0, self.max_calls - self.calls_used)

    @abstractmethod
    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        step: str = "unspecified",
    ) -> T:
        """Return an instance of `schema` built from the prompt.

        Args:
            system: System prompt describing role and rules.
            user: The user/task content.
            schema: Pydantic model the output must satisfy.
            step: Label used in logs and budget accounting.

        Raises:
            BudgetExceeded: If the run's call budget is spent.
            LLMError: If no schema-valid output could be produced.
        """

    def generate_text(self, *, system: str, user: str, step: str = "unspecified") -> str:
        """Return free text. Used only for the deliberate anti-pattern demo.

        Raises:
            NotImplementedError: Subclasses may leave this unsupported.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support text output")


# ═══════════════════════════════════════════════════════════════════════════
# Scripted backend — deterministic, offline
# ═══════════════════════════════════════════════════════════════════════════


class ScriptedLLM(LLMClient):
    """Deterministic offline backend, driven by registered builders.

    Why this is not "fake": the work it does is genuinely rule-based
    extraction and assembly — finding course codes and unit references in free
    text, then composing a plan skeleton from those facts. It is the same code
    path the live backend drives; only the *reasoner* differs. Its guarantees
    are 100% reproducibility, zero cost, and no rate limit, which is what makes
    the labs testable and the viva demo unbreakable.

    A builder is registered per schema:

        @ScriptedLLM.builder_for(ClarifiedRequest)
        def _build(payload, ctx) -> ClarifiedRequest: ...
    """

    name = "scripted"

    _builders: dict[type[BaseModel], Callable[[dict[str, Any], "ScriptedLLM"], BaseModel]] = {}

    def __init__(self, *, max_calls: int = 40) -> None:
        super().__init__(max_calls=max_calls)

    @classmethod
    def builder_for(
        cls, schema: type[BaseModel]
    ) -> Callable[[Callable[[dict[str, Any], "ScriptedLLM"], BaseModel]], Callable[..., BaseModel]]:
        """Register a deterministic builder for a schema.

        Returns a decorator, so registration reads as a one-liner next to the
        agent code that consumes it.
        """

        def decorate(
            fn: Callable[[dict[str, Any], "ScriptedLLM"], BaseModel]
        ) -> Callable[..., BaseModel]:
            cls._builders[schema] = fn
            return fn

        return decorate

    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        step: str = "unspecified",
    ) -> T:
        """Build a schema-valid object without calling any network API.

        Args:
            system: Ignored by the scripted backend, kept for interface parity.
            user: The task content. Interpreted as a JSON object when possible,
                otherwise treated as raw text.
            schema: The model to produce.
            step: Label for logging and budget accounting.

        Raises:
            LLMError: If no builder is registered for `schema`.
            ValidationError: If the builder's output fails schema validation —
                    surfaced rather than swallowed, because a silent schema
                    failure is exactly how an agent pipeline rots.
        """
        self._charge(step=step)
        started = time.perf_counter()

        builder = self._builders.get(schema)
        if builder is None:
            raise LLMError(
                f"ScriptedLLM has no builder registered for {schema.__name__}. "
                f"Registered: {[s.__name__ for s in self._builders]}. "
                "Either register one, or set LLM_BACKEND=live."
            )

        payload = _coerce_payload(user)
        try:
            result = builder(payload, self)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise LLMError(f"scripted builder for {schema.__name__} failed: {exc}") from exc

        if not isinstance(result, schema):  # type: ignore[arg-type]
            raise LLMError(
                f"scripted builder returned {type(result).__name__}, expected {schema.__name__}"
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        log_event(
            logger,
            "llm_call",
            backend=self.name,
            step=step,
            schema=schema.__name__,
            elapsed_ms=round(elapsed_ms, 2),
            calls_used=self.calls_used,
        )
        return result  # type: ignore[return-value]

    def generate_text(self, *, system: str, user: str, step: str = "unspecified") -> str:
        """Concatenate the payload — used only by the anti-pattern comparison."""
        self._charge(step=step)
        payload = _coerce_payload(user)
        if isinstance(payload, dict):
            parts = [f"{k}: {v}" for k, v in payload.items()]
            return "\n".join(parts)
        return str(payload)


def _coerce_payload(user: str) -> dict[str, Any] | str:
    """Interpret the `user` argument as JSON when it looks like JSON.

    The scripted backend receives the same strings a live model would receive,
    but we cheat slightly and pass structured context as JSON, which keeps the
    builders honest (they still have to do the reasoning) without pretending
    they parse natural language.
    """
    stripped = user.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        import json

        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return user


# ═══════════════════════════════════════════════════════════════════════════
# Gemini backend — the real model
# ═══════════════════════════════════════════════════════════════════════════


class GeminiLLM(LLMClient):
    """Real Gemini via `langchain-google-genai`, using structured output.

    Uses `ChatGoogleGenerativeAI.with_structured_output(schema)`, so the model
    is constrained to emit valid JSON for our Pydantic model rather than prose
    we would have to parse. Imported lazily so the package still imports on a
    machine that has not installed the provider SDK.
    """

    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        super().__init__(max_calls=settings.runtime_budget_calls)
        api_key = settings.require_live_llm()
        self._model_name = settings.gemini_model
        self._rate_limiter = RateLimiter(rpm=settings.rate_limit_rpm)
        self._chain: Any = None
        self._raw: Any = None
        self._api_key = api_key
        self._settings = settings

    def _ensure_chain(self) -> None:
        """Build the model on first use, so construction never hits the network.

        Raises:
            LLMError: If `langchain-google-genai` is not installed.
        """
        if self._chain is not None:
            return
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover - depends on install
            raise LLMError(
                "langchain-google-genai is not installed. Run: "
                "pip install langchain-google-genai==4.4.0"
            ) from exc

        self._raw = ChatGoogleGenerativeAI(
            model=self._model_name,
            google_api_key=self._api_key,
            temperature=self._settings.gemini_temperature,
        )
        logger.info("Gemini backend ready (model=%s)", self._model_name)

    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        step: str = "unspecified",
    ) -> T:
        """Call Gemini and return a validated instance of `schema`.

        Raises:
            BudgetExceeded: If the run's call budget is spent.
            LLMError: If the provider call fails or returns unusable output.
        """
        self._ensure_chain()
        self._charge(step=step)
        self._rate_limiter.acquire()
        started = time.perf_counter()

        # with_structured_output is bound per-schema, so bind lazily and cache.
        chain = self._raw.with_structured_output(schema)
        try:
            result = chain.invoke(
                [("system", system), ("human", user)],
            )
        except Exception as exc:  # noqa: BLE001 - provider errors are varied
            raise LLMError(
                f"Gemini call failed at step {step!r} (model={self._model_name}). "
                "Common causes: invalid/expired GOOGLE_API_KEY, free-tier rate "
                f"limit (RPM={self._settings.rate_limit_rpm}), or a region "
                f"restriction. Original error: {exc}"
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        log_event(
            logger,
            "llm_call",
            backend=self.name,
            step=step,
            schema=schema.__name__,
            model=self._model_name,
            elapsed_ms=round(elapsed_ms, 2),
            calls_used=self.calls_used,
        )

        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
        raise LLMError(
            f"Gemini returned {type(result).__name__} for {schema.__name__}, "
            "which is not schema-valid."
        )

    def generate_text(self, *, system: str, user: str, step: str = "unspecified") -> str:
        """Return free-form text from Gemini (used only by the anti-pattern demo)."""
        self._ensure_chain()
        self._charge(step=step)
        self._rate_limiter.acquire()
        result = self._raw.invoke([("system", system), ("human", user)])
        return str(getattr(result, "content", result))


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def get_llm(settings: Optional[Settings] = None, *, max_calls: int | None = None) -> LLMClient:
    """Return the configured LLM backend.

    Falls back to :class:`ScriptedLLM` with a warning if `live` was requested
    but no key is present, so a missing key degrades the run instead of
    crashing it. That fallback is deliberate: a lab session should never die
    because of a credential.
    """
    settings = settings or Settings()
    budget = max_calls if max_calls is not None else settings.runtime_budget_calls

    if settings.using_live_llm():
        try:
            return GeminiLLM(settings)
        except LLMError as exc:
            logger.warning("Live backend unavailable (%s); using scripted.", exc)
            return ScriptedLLM(max_calls=budget)

    return ScriptedLLM(max_calls=budget)


__all__ = [
    "BudgetExceeded",
    "GeminiLLM",
    "LLMClient",
    "LLMError",
    "RateLimiter",
    "ScriptedLLM",
    "get_llm",
]
