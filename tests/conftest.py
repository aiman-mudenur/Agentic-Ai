"""Shared pytest fixtures.

Every test runs against the deterministic `scripted` LLM backend and the
`hashing` embedding backend, so the suite needs no API key, no network, and
produces identical results on every machine. That is a deliberate property: the
acceptance criteria in the document must be re-checkable by a reviewer without
credentials, and without pinning the embedder a developer who set
`EMBEDDING_BACKEND=fastembed` for real work would get different scores -- and
some different verdicts -- from the lab they were trying to re-verify.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow `pytest` to work from a clean checkout without `pip install -e .`
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from course_organizer.llm import ScriptedLLM  # noqa: E402
from course_organizer.labs import lab01_planner_loop as lab01  # noqa: E402
from course_organizer.labs import lab02_tool_using_agent as lab02  # noqa: E402
from course_organizer.tools.compute import build_default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the scripted backend regardless of the developer's `.env`.

    Without this, a machine with `LLM_BACKEND=live` in `.env` would hit the
    network during `pytest` and the suite would stop being deterministic.
    """
    monkeypatch.setenv("LLM_BACKEND", "scripted")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_BACKEND", "hashing")


@pytest.fixture(scope="session")
def fastembed_available() -> bool:
    """Is the learned model already on this machine?

    Checked once per session rather than per test: the fastembed suite needs a
    ~69 MB model, and a test suite must never trigger a download. On a machine
    that has run Lab 4 with `EMBEDDING_BACKEND=fastembed` the learned-model
    tests execute for real; everywhere else they skip with a message saying
    exactly which command would enable them.

    Returns:
        True if `build_embedder("fastembed")` succeeds here.
    """
    from course_organizer.memory.embeddings import EmbeddingError, build_embedder

    try:
        build_embedder("fastembed")
    except (EmbeddingError, OSError):  # pragma: no cover - machine dependent
        return False
    return True


@pytest.fixture
def llm() -> ScriptedLLM:
    """A fresh scripted backend with a generous budget for tests."""
    return ScriptedLLM(max_calls=200)


@pytest.fixture
def lab02_registry():
    """A fresh Lab 2 tool registry with its own ledger.

    A new registry per test keeps the ledger assertions exact: a test that
    expects three calls cannot be confused by calls left over from another.
    """
    return build_default_registry(run_id="test-lab02")


@pytest.fixture
def poisoned_request() -> str:
    """The poisoned free-text request from the Lab 2 acceptance criterion."""
    return lab02.POISONED_REQUEST


@pytest.fixture
def poisoned_result(llm: ScriptedLLM):
    """One full Lab 2 run over `poisoned_request`.

    Scoped per test so each gets a clean registry, ledger and claim set.
    """
    return lab02.run_tool_using_agent(
        lab02.POISONED_REQUEST, llm=llm, run_id="test-poisoned"
    )


@pytest.fixture
def vague_request() -> str:
    """The deliberately vague request used by the doc's acceptance criterion."""
    return lab01.VAGUE_REQUEST


@pytest.fixture
def concrete_request() -> str:
    """A request with every core parameter already present.

    The agent should therefore ask *no* questions. This is the control case
    that proves the clarify loop is not just asking reflexively.
    """
    return (
        "Organise CS201 Data Structures for units 1-3, assignments ASG-01 and "
        "ASG-02, treating 2026-10-15 as today."
    )
