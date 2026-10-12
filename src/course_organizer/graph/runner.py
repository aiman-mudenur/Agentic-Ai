"""One command in, one result out.

Doc 2.10's acceptance criterion (line 690): *"One command yields a validated
organised course workspace (notes, assignments, grades) draft end to end."* This
module is what that sentence asks for.

Three things it does that a bare ``graph.invoke`` would not:

**It finds the gate.** A run stops at ``gate`` and the caller gets back a
suspended result. :func:`GraphRun` wraps that so the caller reads
``run.awaiting_gate`` rather than reaching into ``__interrupt__``.

**It resumes properly.** ``resume`` passes ``Command(resume=payload)`` on the
same thread. Because the gate node re-enters from the top and ``interrupt``
returns immediately, ``ingest``/``plan``/``workers``/``validate``/``assemble``
do not run again. :meth:`GraphRun.nodes_ran` records what actually executed, so
a test can assert that rather than trusting the mechanism.

**It persists the checkpointer to a file.** The SQLite saver is opened with
:func:`~langgraph.checkpoint.sqlite.SqliteSaver.from_conn_string` and the
checkpointer is passed to ``compile()``. Without that the run works and cannot
be resumed from another process, which is the one property the gate must have.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from course_organizer.graph.builder import (
    RECURSION_BACKSTOP,
    build_graph,
    graph_config,
    graph_edges,
)
from course_organizer.graph.context import RunContext, build_run_context
from course_organizer.graph.state import (
    DECISION_APPROVE,
    CourseStatus,
    DraftGradeRow,
    Escalation,
    FacultyDecision,
    GateRequest,
    ValidationOutcome,
)

LOGGER = logging.getLogger(__name__)

#: Default name for the checkpointer file inside a run directory.
CHECKPOINT_FILENAME = "graph.sqlite"

#: The Lab 7 types that legitimately cross the checkpoint boundary.
#:
#: LangGraph serialises state values with ormsgpack and refuses, by default, to
#: deserialise a type it does not recognise: it logs
#: ``"Deserializing unregistered type ... This will be blocked in a future
#: version"``. Every pydantic model and enum the state holds is one of those, so
#: a run would emit four warnings on every resume.
#:
#: Declaring them is the fix the warning itself asks for -- not suppression,
#: and not a switch to the strict mode that would block them outright. The
#: allowlist is also a security control: it names exactly which classes this
#: project is willing to rebuild from a checkpoint file, so a corrupted or
#: hostile database cannot name an arbitrary importable type. Anything added to
#: :class:`~course_organizer.graph.state.CourseState` must be added here too,
#: and ``test_every_checkpointed_type_is_declared_to_langgraph`` enforces it.
CHECKPOINTED_TYPES: tuple[type, ...] = (
    CourseStatus,
    DraftGradeRow,
    Escalation,
    FacultyDecision,
    GateRequest,
    ValidationOutcome,
)


def checkpoint_serializer() -> Any:
    """Build the serializer with Lab 7's checkpointed types declared.

    Returns:
        A :class:`~langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer` that
        accepts exactly :data:`CHECKPOINTED_TYPES`.
    """
    return JsonPlusSerializer(
        allowed_msgpack_modules=list(CHECKPOINTED_TYPES)
    )


@dataclass
class GraphRun:
    """A handle on one execution of the graph.

    Attributes:
        run_id: Identifies the run; also the default output subdirectory.
        thread_id: LangGraph's resumption unit. Defaults to the run id.
        context: The per-run dependencies.
        state: The last state returned by ``invoke``. Empty until it runs.
        interrupts: Whatever ``invoke`` returned under ``__interrupt__``.
    """

    run_id: str
    thread_id: str
    context: RunContext
    state: dict[str, Any] = field(default_factory=dict)
    interrupts: tuple[Any, ...] = ()

    # -- status ----------------------------------------------------------
    @property
    def awaiting_gate(self) -> bool:
        """Did the run stop at the human gate?"""
        return bool(self.interrupts)

    @property
    def gate_request(self) -> Optional[GateRequest]:
        """The reviewer's request, if the run suspended at the gate.

        Returns ``None`` rather than raising when there is no interrupt, because
        "did it ask?" and "what did it ask?" are separate questions and a caller
        checking the first should not have to guard the second.
        """
        if not self.interrupts:
            return None
        value = getattr(self.interrupts[0], "value", None)
        if isinstance(value, GateRequest):
            return value
        if isinstance(value, dict):
            try:
                return GateRequest(**value)
            except Exception:  # noqa: BLE001 - a malformed payload is not fatal
                LOGGER.warning("gate payload did not validate as a GateRequest")
                return None
        return None

    @property
    def status(self) -> CourseStatus:
        """Where the run got to. Defaults to ``DRAFT`` before it starts."""
        raw = self.state.get("status")
        if isinstance(raw, CourseStatus):
            return raw
        try:
            return CourseStatus(raw)
        except (ValueError, TypeError):
            return CourseStatus.DRAFT

    @property
    def published(self) -> bool:
        """Was anything actually published? Requires an approval on record."""
        return self.status is CourseStatus.PUBLISHED

    # -- execution -------------------------------------------------------
    def start(self, graph: Any, **initial: Any) -> dict[str, Any]:
        """Invoke from the beginning.

        Args:
            graph: The compiled graph.
            **initial: Extra state to seed the run with. Anything already in
                the context wins -- ``course_code`` comes from the context
                unless the caller explicitly overrides it, so a typo in a seed
                value cannot silently point the run at a different course.

        Returns:
            The final state.
        """
        seed: dict[str, Any] = {
            "course_code": self.context.course_code,
            "run_id": self.run_id,
            "human_authority": self.context.human_authority,
        }
        seed.update(initial)
        cfg = graph_config(self.thread_id, run_id=self.run_id)
        result = graph.invoke(
            seed,
            cfg,
            recursion_limit=RECURSION_BACKSTOP,
        )
        return self._absorb(result)

    def resume(
        self, graph: Any, decision: FacultyDecision | str | dict[str, Any]
    ) -> dict[str, Any]:
        """Resume a suspended run with the faculty's answer.

        Args:
            graph: The compiled graph.
            decision: A :class:`FacultyDecision`, a mapping, or one of the bare
                strings ``approve``/``edit``/``override``/``refuse``.

        Returns:
            The final state.

        Raises:
            RuntimeError: If the run is not suspended. Resuming a finished run
                is a caller bug, and LangGraph would answer it by silently
                replaying from the last checkpoint.
        """
        if not self.interrupts:
            raise RuntimeError(
                f"run {self.run_id} is not suspended at a gate; nothing to "
                f"resume (status={self.status.value})"
            )
        payload = decision if isinstance(decision, FacultyDecision) else decision
        cfg = graph_config(self.thread_id, run_id=self.run_id)
        result = graph.invoke(
            Command(resume=payload),
            cfg,
            recursion_limit=RECURSION_BACKSTOP,
        )
        return self._absorb(result)

    def _absorb(self, result: dict[str, Any]) -> dict[str, Any]:
        """Split LangGraph's combined return into state and interrupts.

        ``invoke`` returns one dict carrying both the state written so far and
        an ``__interrupt__`` key holding a tuple of ``Interrupt`` objects.
        Mixing them would put a tuple in the state that a node might later try
        to treat as a model, so they are separated here.
        """
        result = dict(result)
        interrupts = result.pop("__interrupt__", ()) or ()
        self.interrupts = tuple(interrupts)
        self.state = result
        LOGGER.info(
            "run %s: interrupts=%d status=%s",
            self.run_id,
            len(self.interrupts),
            self.status.value,
        )
        return self.state

    # -- evidence --------------------------------------------------------
    def nodes_ran(self) -> list[str]:
        """Every node that actually executed, in order.

        Appended by each node, so a resumed run's list is the proof that the
        pre-gate nodes did not re-run: it gains ``gate`` and ``publish`` but not
        a second ``workers``.
        """
        return list(self.state.get("nodes_ran", []) or [])

    def attempts_made(self) -> int:
        """How many validation passes the run made."""
        return int(self.state.get("attempts", 0) or 0)

    def summary(self) -> str:
        """One multi-part line for logs and evidence files."""
        parts = [
            f"run={self.run_id}",
            f"status={self.status.value}",
            f"nodes={len(self.nodes_ran())}",
            f"attempts={self.attempts_made()}",
        ]
        if self.awaiting_gate:
            parts.append("gate=awaiting_faculty")
        if self.published:
            parts.append(f"published={self.state.get('published_path', '')}")
        escalation = self.state.get("escalation")
        if escalation is not None:
            parts.append(f"escalated={getattr(escalation, 'reason', '')}")
        return " ".join(parts)


def checkpoint_path(root: Path, run_id: str) -> Path:
    """``<root>/<run_id>/graph.sqlite``.

    One file per run, so two runs never contend for one saver and a stale
    checkpoint cannot be resumed as if it belonged to a fresh run.
    """
    return Path(root) / run_id / CHECKPOINT_FILENAME


@contextmanager
def open_checkpointer(path: Path):
    """Yield an opened SQLite checkpointer, with its tables created.

    ``SqliteSaver.from_conn_string`` returns a *context manager*, not a saver --
    calling it bare yields a ``_GeneratorContextManager`` with no ``setup`` on
    it. That helper also gives no way to pass a serializer, which is the only
    way to declare Lab 7's checkpointed types, so the connection is opened here
    instead. The ``check_same_thread=False`` flag matches what
    ``from_conn_string`` passes and is not optional: LangGraph's Pregel loop
    reads and writes the checkpoint from its own thread.

    Args:
        path: The ``.sqlite`` file. Parent directories are created.

    Yields:
        The saver, ready to pass to ``compile()``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(conn, serde=checkpoint_serializer())
        saver.setup()
        yield saver
    finally:
        conn.close()


def run_pipeline(
    *,
    course_code: str = "CS201",
    run_id: Optional[str] = None,
    max_attempts: int = 3,
    human_authority: str = "course_faculty",
    output_dir: Optional[Path] = None,
    root: Optional[Path] = None,
    fail_until_attempt: int = 0,
) -> GraphRun:
    """Build the graph, run it to the gate, and return the handle.

    Args:
        course_code: Which course to organise.
        run_id: Defaults to ``lab07-<8 hex>``, unique per invocation.
        max_attempts: The bound on the back-edge.
        human_authority: Who the gate names.
        output_dir: Where ``publish`` writes.
        root: Parent for the checkpointer. Defaults to ``out/lab07``.
        fail_until_attempt: Injects the validation defect for the first
            ``fail_until_attempt - 1`` passes so the back-edge can be
            demonstrated. ``0`` (the default) runs clean.

    Returns:
        The :class:`GraphRun`, stopped at the gate.

    Note:
        The checkpointer is closed when the function returns. The run is
        suspended at the gate at that point, so its state is safely on disk and
        :func:`resume_run` reopens the file to continue. Holding a SQLite
        connection open across an ``invoke`` that returns a suspended graph is
        what leaves a ``database is locked`` behind on some systems.
    """
    from course_organizer.graph.context import default_output_dir

    run_id = run_id or f"lab07-{uuid.uuid4().hex[:8]}"
    base = Path(root) if root is not None else default_output_dir(run_id).parent
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir(run_id)
    ctx = build_run_context(
        run_id,
        course_code=course_code,
        max_attempts=max_attempts,
        human_authority=human_authority,
        output_dir=target_dir,
    )
    run = GraphRun(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        context=ctx,
    )
    saver = None
    with open_checkpointer(checkpoint_path(base, run_id)) as saver:
        graph = build_graph(ctx, checkpointer=saver)
        run.start(graph, fail_until_attempt=fail_until_attempt)
    return run


def resume_run(run_id: str, decision: Any, *, root: Optional[Path] = None) -> GraphRun:
    """Reopen a suspended run from disk and resume it.

    This is the cross-process path, and it is the one that proves the
    checkpointer was wired in properly. It rebuilds the context from the run id
    -- safe because the context holds no run-specific mutable state beyond the
    registries -- reopens the SQLite saver and resumes the same thread.

    Args:
        run_id: The run to resume.
        decision: The faculty's answer.
        root: Parent directory holding ``<run_id>/graph.sqlite``. Defaults to
            ``out/lab07``.

    Returns:
        The completed run.
    """
    from course_organizer.graph.context import default_output_dir

    base = Path(root) if root is not None else default_output_dir(run_id).parent
    # Same directory `run_pipeline` would have used, so the resumed run writes
    # beside the checkpoint it was resumed from instead of falling back to the
    # repo default and scattering artefacts.
    ctx = build_run_context(run_id, output_dir=base / run_id)
    run = GraphRun(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        context=ctx,
    )
    with open_checkpointer(checkpoint_path(base, run_id)) as saver:
        graph = build_graph(ctx, checkpointer=saver)
        # LangGraph's resume needs no seed state: the thread already holds it.
        cfg = graph_config(run.thread_id, run_id=run_id)
        result = graph.invoke(
            Command(resume=decision),
            cfg,
            recursion_limit=RECURSION_BACKSTOP,
        )
        run._absorb(result)
    return run


def load_gate_request(run_id: str, *, root: Optional[Path] = None) -> Optional[GateRequest]:
    """Read a suspended run's pending gate request without resuming it.

    Needed by the CLI so it can *show* the reviewer what is waiting and then ask.
    Uses ``get_state_history`` and takes the newest snapshot's interrupts,
    because a thread that is suspended at an interrupt has that interrupt in its
    latest checkpoint.
    """
    from course_organizer.graph.context import default_output_dir

    base = Path(root) if root is not None else default_output_dir(run_id).parent
    path = checkpoint_path(base, run_id)
    if not path.exists():
        return None
    with open_checkpointer(path) as saver:
        ctx = build_run_context(run_id, output_dir=base / run_id)
        graph = build_graph(ctx, checkpointer=saver)
        cfg = graph_config(f"thread-{run_id}", run_id=run_id)
        for snapshot in graph.get_state_history(cfg):
            for interrupt in getattr(snapshot, "interrupts", ()) or ():
                value = getattr(interrupt, "value", None)
                if isinstance(value, GateRequest):
                    return value
                if isinstance(value, dict):
                    try:
                        return GateRequest(**value)
                    except Exception:  # noqa: BLE001
                        continue
            return None
        return None


__all__ = [
    "CHECKPOINTED_TYPES",
    "CHECKPOINT_FILENAME",
    "GraphRun",
    "checkpoint_path",
    "checkpoint_serializer",
    "load_gate_request",
    "open_checkpointer",
    "resume_run",
    "run_pipeline",
]