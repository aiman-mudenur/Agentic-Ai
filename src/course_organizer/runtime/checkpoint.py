"""Checkpoints: the state a crashed run resumes from, stored outside the process.

Doc 2.10 Lab 6's deliverable is *\"Runtime wrapping the agent: budgets, gates,
checkpoint/resume, audit log\"* and the pitfall immediately under it says which
half is easy to get wrong:

    **Pitfall to avoid.** Log inputs and outputs, not just messages;
    checkpoint before risky steps or resume loses state.

The first clause belongs to :mod:`course_organizer.runtime.audit`. This module
is the second clause, and "resume loses state" is more specific than it looks. A
checkpoint that holds only the latest output is not enough to resume a run that
has four completed steps and a budget half spent, because the resumed process
needs three separate things:

1. **Where to continue.** ``cursor`` says the next step to run, so resume does
   not re-run the four that already succeeded. Re-running a *publish* step is
   not a performance problem, it is a duplicated side effect.
2. **What the earlier steps produced.** ``artifacts`` carries the accumulated
   outputs forward, because step N reads step N-1's result and cannot recompute
   it without re-running N-1.
3. **Where the audit chain left off.** ``audit_digest`` and ``audit_seq`` are the
   *resume* deliverable that is easiest to forget. Without them a resumed run
   starts a second, independent hash chain in the same file, and the log that
   claims to be one record of one run becomes two interleaved unverifiable
   halves. Carrying the digest forward is what makes "every action is logged"
   true across a crash.

Storage is :class:`~langgraph.checkpoint.sqlite.SqliteSaver`, the package
``langgraph-checkpoint-sqlite`` provides, used here directly rather than through
a graph -- Lab 7 owns the node graph; Lab 6 owns durability. Three facts about
that class were established by reading and running its source, not from its
docs, and all three are load-bearing here:

- ``config["configurable"]`` must contain ``checkpoint_ns``. A config carrying
  only ``thread_id`` raises ``KeyError: 'checkpoint_ns'`` inside ``put``.
- ``put`` **returns** the config that carries the new ``checkpoint_id``, and
  ``put_writes`` needs that value. So the correct sequence is to chain the
  returned config forward, not to reuse the one you passed in.
- **The checkpoint id must sort in write order.** Every read is
  ``SELECT ... ORDER BY checkpoint_id DESC LIMIT 1`` -- a *string* comparison on
  the id, with no timestamp or rowid to fall back on. So "the latest
  checkpoint" is defined purely by how the id sorts. This store therefore builds
  ids as a zero-padded write ordinal followed by the audit digest, and allocates
  the ordinal from the table itself rather than from a counter held in
  :class:`RunState`, so a resuming process continues the same sequence instead of
  restarting it. An id derived from anything unordered -- a hash, a uuid -- would
  make resume load whichever checkpoint happened to sort highest, which produces
  a plausible-looking run that continues from the wrong step.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.logging_setup import log_event
from course_organizer.runtime.audit import GENESIS_DIGEST
from course_organizer.runtime.errors import CheckpointUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKPOINT_NS",
    "CheckpointRef",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "RunState",
    "SqliteCheckpointStore",
    "sqlite_path",
]

#: The ``checkpoint_ns`` Lab 6 runs under. Namespaced so that if Lab 7 starts
#: putting its own graph checkpoints in the same database file, the two do not
#: interleave as if they were one thread's history.
CHECKPOINT_NS = "course-organizer/runtime"

#: The ``source`` value written into checkpoint metadata for a normal run step.
SOURCE_LOOP = "loop"

#: The ``source`` value written when a run resumes from a checkpoint.
SOURCE_RESUME = "update"


def _utcnow() -> str:
    """An ISO-8601 UTC timestamp, matching LangGraph's own checkpoint ``ts``."""
    return datetime.now(timezone.utc).isoformat()


def sqlite_path(root: Path | str, *, run_id: str) -> Path:
    """Where a run's checkpoint database lives, under ``root``.

    Split out so the tests, the lab module and the notebook all agree on the
    layout instead of each inventing a filename.
    """
    return Path(root) / "runtime" / "checkpoints" / f"{run_id}.sqlite"


class RunState(BaseModel):
    """Everything a resumed run needs that the previous process would have held.

    Frozen and JSON-safe by construction, because it is written into a SQLite
    checkpoint and read back by a different interpreter. Anything non-JSON here
    would fail at the worst moment -- mid-crash -- rather than at the call site
    that put it in.

    Attributes:
        thread_id: The resume key. Stable across a crash; this is what
            :meth:`CheckpointStore.load` is asked for.
        run_id: Which run this state belongs to.
        course_code: The course being organised, carried so a resumed run can
            assert it is resuming the *same* work.
        plan_sha256: Digest of the plan being executed. A resume against a
            different plan raises :class:`ResumeDivergence` rather than applying
            step N's expectations to step N's old inputs.
        step_index: How many steps have completed.
        cursor: Name of the next step to run, or ``""`` when the run is done.
        completed: Step names already finished, in order.
        artifacts: Accumulated outputs, keyed by the step or action that made
            them. This is what stops resume recomputing earlier work.
        approvals: Step name to ``"approved"`` or ``"refused"``.
        spent: Budget spend, as dimension to count.
        audit_seq: The sequence number the next audit record must carry.
        audit_digest: The digest the next audit record must chain to. Carried
            so a resumed run continues one chain instead of starting a second.
        updated_at: When this state was written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str
    run_id: str
    course_code: str = ""
    plan_sha256: str = ""
    step_index: int = 0
    cursor: str = ""
    completed: tuple[str, ...] = ()
    artifacts: Mapping[str, Any] = Field(default_factory=dict)
    approvals: Mapping[str, str] = Field(default_factory=dict)
    spent: Mapping[str, int] = Field(default_factory=dict)
    audit_seq: int = 1
    audit_digest: str = GENESIS_DIGEST
    updated_at: str = Field(default_factory=_utcnow)

    def with_progress(
        self,
        *,
        cursor: str,
        completed: tuple[str, ...],
        step_index: int,
        artifacts: Optional[Mapping[str, Any]] = None,
        approvals: Optional[Mapping[str, str]] = None,
        spent: Optional[Mapping[str, int]] = None,
        audit_seq: Optional[int] = None,
        audit_digest: Optional[str] = None,
    ) -> RunState:
        """A copy advanced by one step.

        A method rather than six call sites building the same ``model_copy``, so
        that "the cursor moves and the completed list grows together" is one
        decision made in one place instead of six decisions a future edit could
        make inconsistently.
        """
        return self.model_copy(
            update={
                "cursor": cursor,
                "completed": tuple(completed),
                "step_index": step_index,
                "artifacts": dict(self.artifacts if artifacts is None else artifacts),
                "approvals": dict(self.approvals if approvals is None else approvals),
                "spent": dict(self.spent if spent is None else spent),
                "audit_seq": self.audit_seq if audit_seq is None else audit_seq,
                "audit_digest": self.audit_digest if audit_digest is None else audit_digest,
                "updated_at": _utcnow(),
            }
        )

    def with_audit_position(self, *, seq: int, digest: str) -> RunState:
        """A copy that remembers where the audit chain got to.

        Only the chain position moves; the cursor does not. That distinction is
        the difference between a checkpoint written *after* an action and one
        written *before* it, which doc 2.10 Lab 6's pitfall calls out directly.
        """
        return self.model_copy(update={"audit_seq": seq, "audit_digest": digest, "updated_at": _utcnow()})

    def done(self) -> bool:
        """Has the run reached its last step?"""
        return not self.cursor

    def artifact(self, key: str, default: Any = None) -> Any:
        """One accumulated output, or ``default``."""
        return self.artifacts.get(key, default)

    def is_done(self, step: str) -> bool:
        """Has ``step`` already completed?"""
        return step in self.completed

    def approval_for(self, step: str) -> str:
        """``"approved"``, ``"refused"`` or ``""`` for a step."""
        return self.approvals.get(step, "")


class CheckpointRef(BaseModel):
    """A pointer to one stored checkpoint.

    Attributes:
        thread_id: Which run thread.
        checkpoint_id: The stored checkpoint's id, as this store built it.
        write_seq: The write ordinal, parsed back out of ``checkpoint_id``. It
            is what makes the id sort in write order, and it is reported so a
            report can show the order without re-deriving it from the string.
        step: Which step index the state was at when it was written.
        cursor: The state's cursor, so a report need not re-read the payload.
        source: The metadata ``source`` this checkpoint was written under.
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str
    checkpoint_id: str
    write_seq: int = 0
    step: int = -1
    cursor: str = ""
    source: str = SOURCE_LOOP


#: Width of the write ordinal in a checkpoint id. Fixed, so that lexicographic
#: ordering of ids matches write ordering for every plausible run length: at
#: twelve digits this addresses 10**12 writes per thread.
WRITE_SEQ_WIDTH = 12


def make_checkpoint_id(write_seq: int, digest: str) -> str:
    """Build a checkpoint id that sorts in write order.

    The ordinal comes first and is zero-padded to :data:`WRITE_SEQ_WIDTH`, so it
    dominates the string comparison that ``ORDER BY checkpoint_id`` performs. The
    digest suffix is not for ordering -- it is so that two checkpoints written
    at the same instant remain distinguishable, which the table's primary key
    ``(thread_id, checkpoint_ns, checkpoint_id)`` requires.

    Args:
        write_seq: This thread's write count, from 1.
        digest: The audit digest current when the state was saved.

    Returns:
        An id of the form ``000000000003-1a2b3c4d5e6f``.
    """
    if write_seq < 1:
        raise ValueError(f"write_seq starts at 1, got {write_seq}")
    return f"{write_seq:0{WRITE_SEQ_WIDTH}d}-{digest[:12]}"


def parse_write_seq(checkpoint_id: str) -> int:
    """The write ordinal embedded in a checkpoint id, or 0 if there is none."""
    head = checkpoint_id.split("-", 1)[0]
    return int(head) if head.isdigit() else 0


class CheckpointStore(ABC):
    """Durable, ordered, per-thread storage for :class:`RunState`.

    Abstract so the runtime is written against the capability -- save, load,
    history -- and not against SQLite. The Lab 6 acceptance criterion is about
    a run surviving a crash, and a store that cannot outlive the process cannot
    demonstrate it, which is why ``InMemoryCheckpointStore`` is documented as
    test-only rather than offered as an option.
    """

    @abstractmethod
    def save(self, state: RunState, *, source: str = SOURCE_LOOP) -> CheckpointRef:
        """Write ``state`` and return a pointer to it."""

    @abstractmethod
    def load(self, thread_id: str) -> Optional[RunState]:
        """The most recent state for ``thread_id``, or ``None`` if it has none."""

    @abstractmethod
    def history(self, thread_id: str) -> tuple[CheckpointRef, ...]:
        """Every checkpoint for a thread, newest first."""

    @abstractmethod
    def thread_ids(self) -> tuple[str, ...]:
        """Every thread this store holds state for."""

    @abstractmethod
    def delete_thread(self, thread_id: str) -> None:
        """Forget a thread entirely."""

    def exists(self, thread_id: str) -> bool:
        """Is there anything to resume from?"""
        return self.load(thread_id) is not None

    def require(self, thread_id: str) -> RunState:
        """The latest state for ``thread_id``, or raise.

        Raises:
            CheckpointUnavailable: If the thread has no checkpoint. The runtime
                uses this instead of ``load`` so a typo'd thread id surfaces
                rather than silently beginning a fresh run.
        """
        state = self.load(thread_id)
        if state is None:
            raise CheckpointUnavailable(thread_id, self.thread_ids())
        return state

    def __contains__(self, thread_id: object) -> bool:
        return isinstance(thread_id, str) and self.exists(thread_id)


class InMemoryCheckpointStore(CheckpointStore):
    """Per-thread history in a dict. **Test-only** -- it dies with the process.

    Named so the durability claim cannot be read off it by accident: Lab 6's
    acceptance criterion is *\"a crashed run resumes from its last checkpoint\"*,
    and this class cannot satisfy it. A test asserts the runtime's own default
    store is the SQLite one.

    Args:
        run_id: Stamped on log events.
    """

    def __init__(self, *, run_id: str = "") -> None:
        self._threads: dict[str, list[tuple[int, RunState]]] = {}
        self._run_id = run_id

    def _next_ordinal(self, thread_id: str) -> int:
        """The next write ordinal, counted from this thread's history.

        Same contract as the SQLite store's, so a test that passes against this
        one is testing the same ordering rule.
        """
        return len(self._threads.get(thread_id, [])) + 1

    def save(self, state: RunState, *, source: str = SOURCE_LOOP) -> CheckpointRef:
        """Append ``state`` to its thread's history."""
        ordinal = self._next_ordinal(state.thread_id)
        self._threads.setdefault(state.thread_id, []).append((ordinal, state))
        return CheckpointRef(
            thread_id=state.thread_id,
            checkpoint_id=make_checkpoint_id(ordinal, state.audit_digest),
            write_seq=ordinal,
            step=state.step_index,
            cursor=state.cursor,
            source=source,
        )

    def load(self, thread_id: str) -> Optional[RunState]:
        """The most recent state, or ``None``."""
        rows = self._threads.get(thread_id)
        return rows[-1][1] if rows else None

    def history(self, thread_id: str) -> tuple[CheckpointRef, ...]:
        """Every checkpoint for a thread, newest first."""
        rows = self._threads.get(thread_id, [])
        refs = [
            CheckpointRef(
                thread_id=thread_id,
                checkpoint_id=make_checkpoint_id(ordinal, state.audit_digest),
                write_seq=ordinal,
                step=state.step_index,
                cursor=state.cursor,
                source=SOURCE_LOOP,
            )
            for ordinal, state in rows
        ]
        return tuple(reversed(refs))

    def thread_ids(self) -> tuple[str, ...]:
        """Every thread held."""
        return tuple(self._threads)

    def delete_thread(self, thread_id: str) -> None:
        """Forget a thread."""
        self._threads.pop(thread_id, None)


class SqliteCheckpointStore(CheckpointStore):
    """Durable checkpoints in a SQLite file, via LangGraph's ``SqliteSaver``.

    The file is the point. ``SqliteSaver`` is a context manager holding an open
    ``sqlite3.Connection``, so this class is too, and :meth:`session` is the
    supported way in:

        with SqliteCheckpointStore(path).session() as store:
            store.save(state)

    Using it without the context manager leaves the connection to garbage
    collection, which on CPython means the file can still be open when a
    resuming process tries to read it -- the one failure that would make the
    crash-and-resume proof unreliable for reasons that have nothing to do with
    the runtime.

    Args:
        path: The ``.sqlite`` file. Parent directories are created.
        run_id: Stamped on log events.
    """

    def __init__(self, path: Path | str, *, run_id: str = "") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id
        self._saver: Any = None
        self._connection: Any = None

    @property
    def path(self) -> Path:
        """Where the database lives."""
        return self._path

    @property
    def durable(self) -> bool:
        """Always ``True``. This class is the durable one."""
        return True

    @contextmanager
    def session(self) -> Iterator[SqliteCheckpointStore]:
        """Open the connection, create the schema, and close it on exit.

        ``SqliteSaver.from_conn_string`` is itself a context manager yielding the
        saver; this nests inside it rather than holding the raw connection, so
        the saver's own setup and teardown stay in charge.
        """
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(self._path)) as saver:
            saver.setup()
            self._saver = saver
            try:
                yield self
            finally:
                self._saver = None

    def _require_saver(self) -> Any:
        """The open saver, or a clear error naming the fix."""
        if self._saver is None:
            raise RuntimeError(
                "SqliteCheckpointStore used outside session(). Open it with "
                "'with SqliteCheckpointStore(path).session() as store:' -- the "
                "sqlite3 connection is owned by the session, and using the store "
                "outside it would leave the file open to garbage collection."
            )
        return self._saver

    def _config(self, thread_id: str, checkpoint_id: str = "") -> dict[str, Any]:
        """A LangGraph config carrying the keys ``put`` actually requires.

        ``checkpoint_ns`` is mandatory -- confirmed by reading
        ``SqliteSaver.put``, which indexes it without a default -- and
        ``checkpoint_id`` is included only when chaining forward from a ``put``.
        """
        configurable: dict[str, Any] = {
            "thread_id": thread_id,
            "checkpoint_ns": CHECKPOINT_NS,
        }
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def _next_ordinal(self, thread_id: str) -> int:
        """The next write ordinal for ``thread_id``, read from the table.

        Read rather than held in :class:`RunState` for two reasons. A crash
        between the save and any state update would leave a held counter behind
        the table, and the next save would reuse an id and hit the primary key;
        and a resuming process starts with no counter at all, so a held one would
        restart at 1 and collide with the crashed run's rows immediately. The
        table is the single source of truth for both cases.
        """
        saver = self._require_saver()
        with saver.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id, CHECKPOINT_NS),
            )
            row = cur.fetchone()
        return 1 if row is None else parse_write_seq(str(row[0])) + 1

    def save(self, state: RunState, *, source: str = SOURCE_LOOP) -> CheckpointRef:
        """Write ``state`` as the newest checkpoint for its thread.

        The state is stored under a single ``__lab06_state`` channel as its JSON
        form rather than splatted across channels, because the runtime reads it
        as one document. The id comes from :func:`make_checkpoint_id` fed by the
        table's own write ordinal, so ``get_tuple`` -- which takes the highest
        id -- returns *this* state and not an arbitrary sibling.

        Two checkpoints may share a step: the runtime writes one before a risky
        step and one after, which is what doc 2.10 Lab 6's pitfall asks for. The
        ordinal, not the step, is what keeps them ordered.
        """
        saver = self._require_saver()
        ordinal = self._next_ordinal(state.thread_id)
        checkpoint_id = make_checkpoint_id(ordinal, state.audit_digest)
        payload = state.model_dump(mode="json")
        checkpoint: dict[str, Any] = {
            "v": 1,
            "id": checkpoint_id,
            "ts": state.updated_at,
            "channel_values": {"__lab06_state": payload},
            "channel_versions": {"__lab06_state": str(state.audit_seq)},
            "versions_seen": {},
        }
        metadata = {
            "source": source,
            "step": state.step_index,
            "run_id": state.run_id,
            "course_code": state.course_code,
            "cursor": state.cursor,
            "audit_digest": state.audit_digest,
        }
        saver.put(self._config(state.thread_id), checkpoint, metadata, {})
        log_event(
            logger,
            "runtime.checkpoint_saved",
            run_id=self._run_id,
            thread_id=state.thread_id,
            step=state.step_index,
            write_seq=ordinal,
            cursor=state.cursor,
            checkpoint_id=checkpoint_id,
        )
        return CheckpointRef(
            thread_id=state.thread_id,
            checkpoint_id=checkpoint_id,
            write_seq=ordinal,
            step=state.step_index,
            cursor=state.cursor,
            source=source,
        )

    def _state_from(self, checkpoint: Mapping[str, Any]) -> RunState:
        """Rehydrate a stored checkpoint into a :class:`RunState`."""
        return RunState.model_validate(checkpoint["channel_values"]["__lab06_state"])

    def load(self, thread_id: str) -> Optional[RunState]:
        """The most recent state, or ``None`` if the thread is unknown.

        Correct only because ids sort in write order -- see the module docstring.
        """
        saver = self._require_saver()
        found = saver.get_tuple(self._config(thread_id))
        if found is None:
            return None
        return self._state_from(found.checkpoint)

    def history(self, thread_id: str) -> tuple[CheckpointRef, ...]:
        """Every checkpoint for a thread, newest first.

        ``SqliteSaver.list`` orders by ``checkpoint_id DESC``; this preserves
        that rather than re-sorting, because the order is a property of the
        store the runtime depends on and re-sorting would hide a change in it.
        """
        saver = self._require_saver()
        refs: list[CheckpointRef] = []
        for tup in saver.list(self._config(thread_id)):
            metadata = tup.metadata or {}
            checkpoint_id = str(tup.checkpoint["id"])
            refs.append(
                CheckpointRef(
                    thread_id=thread_id,
                    checkpoint_id=checkpoint_id,
                    write_seq=parse_write_seq(checkpoint_id),
                    step=int(metadata.get("step", -1)),
                    cursor=str(metadata.get("cursor", "")),
                    source=str(metadata.get("source", SOURCE_LOOP)),
                )
            )
        return tuple(refs)

    def thread_ids(self) -> tuple[str, ...]:
        """Every thread with a checkpoint.

        Read from the table itself rather than a side index, so a thread written
        by another process is visible -- which is exactly the situation a resume
        creates.
        """
        saver = self._require_saver()
        with saver.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ?",
                (CHECKPOINT_NS,),
            )
            found = cur.fetchall()
        return tuple(str(row[0]) for row in found)

    def delete_thread(self, thread_id: str) -> None:
        """Forget a thread's history."""
        self._require_saver().delete_thread(thread_id)

    def count(self, thread_id: str) -> int:
        """How many checkpoints a thread has."""
        return len(self.history(thread_id))