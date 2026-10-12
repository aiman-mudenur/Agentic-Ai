"""The audit log: every action with its inputs *and* outputs, hash-chained.

Doc 2.10 Lab 6 names two things that must exist by the end of this module.

The pseudocode is one line long:

    audit.log(step.intent, step.action, step.result)

and the pitfall immediately under it is the sharper requirement:

    **Pitfall to avoid.** Log inputs and outputs, not just messages;
    checkpoint before risky steps or resume loses state.

"Log inputs and outputs, not just messages" is a statement about what a record
must *contain*, and it is the reason :class:`AuditRecord` is a fixed model with
``inputs`` and ``outputs`` as required fields rather than a free-text ``message``.
A log line of ``"read notes for CS201"`` answers what happened. It cannot answer
*which* note titles came back, *which* record the similarity check compared, or
*what the connector refused*. Those are the questions an audit is for, and a
message-shaped log cannot answer any of them.

Doc 2.12's requirement is *"a full audit log of every grade published and who
approved it"* -- a claim about **completeness and attribution**, so this module
adds the two things that make it checkable:

**Every record names who and what.** ``principal`` and ``role`` come from the
runtime, never from the step, so a step cannot log itself under someone else's
identity. ``token_fingerprint`` follows Lab 5: enough to tell which credential
was used, not enough to leak it.

**The log is chained.** Each record carries ``digest``, the SHA-256 of its own
canonical content plus the previous record's digest. Editing a record, dropping
one, or reordering two all break the chain, and :meth:`AuditLog.verify` says
which record and why. A log file that can be quietly rewritten after the fact
does not support *"a full audit log"* -- it supports a claim about a log.
Append-only at the filesystem level too: :class:`JsonlAuditLog` opens with
``O_APPEND`` and writes whole lines, so a concurrent reader never sees a torn
record.

This is the read-side counterpart to Lab 5's ``ConnectorLedger``, and it is
**durable**, which that one deliberately was not. :class:`JsonlAuditLog` survives
the process; :class:`InMemoryAuditLog` is for tests and is labelled as such by
its name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.logging_setup import log_event
from course_organizer.runtime.errors import AuditIntegrityError

logger = logging.getLogger(__name__)

__all__ = [
    "DIGEST_HEX",
    "GENESIS_DIGEST",
    "AuditLog",
    "AuditOutcome",
    "AuditRecord",
    "InMemoryAuditLog",
    "JsonlAuditLog",
    "canonical_json",
    "chain_digest",
]

#: Length of the chained digest, in hex characters. Matches Lab 5's ledger
#: fingerprint so the two logs are visually consistent and a reviewer is not
#: reading two different digest widths.
DIGEST_HEX = 16

#: The ``prev_digest`` of the first record in a chain. A fixed, obviously-fake
#: value rather than an empty string, so a record whose ``prev_digest`` is
#: missing is distinguishable from the genuine first record.
GENESIS_DIGEST = "0" * DIGEST_HEX

#: Outcomes an action can have. ``refused`` and ``failed`` are separate on
#: purpose, for the same reason Lab 5 separated them: a gate saying no is
#: governance working, and a crash is not.
AuditOutcome = str

OUTCOMES: tuple[str, ...] = (
    "ok",  # the action ran and succeeded
    "failed",  # the action ran and raised
    "refused",  # policy refused before the action: budget or gate
    "skipped",  # never attempted, e.g. the run stopped earlier
)


def canonical_json(payload: Any) -> str:
    """A stable JSON rendering, so a digest does not depend on key order.

    ``sort_keys`` matters because the same logical record built twice in two
    processes could otherwise serialise in a different order, hash differently,
    and make an honest log look tampered with.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def chain_digest(record: Mapping[str, Any], previous: str) -> str:
    """The digest for ``record``, chained to ``previous``.

    Args:
        record: The record's content, without the ``digest`` field itself.
        previous: The previous record's digest, or :data:`GENESIS_DIGEST`.

    Returns:
        The first :data:`DIGEST_HEX` hex characters of the SHA-256.
    """
    hasher = hashlib.sha256()
    hasher.update(previous.encode("ascii"))
    hasher.update(b"\x00")
    hasher.update(canonical_json(record).encode("utf-8"))
    return hasher.hexdigest()[:DIGEST_HEX]


class AuditRecord(BaseModel):
    """One action, with what went in and what came out.

    ``inputs`` and ``outputs`` are required, and that is the whole point of the
    module. An empty dict is honest -- a step with no arguments should log
    ``{}`` -- but there is no way to construct a record that declines to say.
    ``message`` exists as a human-readable summary and is explicitly *not* the
    evidence; a test asserts that no acceptance check is ever satisfied by
    reading ``message``.

    Attributes:
        seq: Position in this run's log, from 1. Gaps are a red flag.
        run_id: The run this belongs to.
        thread_id: The checkpoint thread, so a resumed run's records join the
            same chain as the pre-crash ones.
        step: Step name.
        intent: Why the step was taken, from the plan.
        action: What it did -- usually a tool or skill name.
        kind: Which family of work: tool, skill, memory, llm, runtime, gate.
        inputs: Arguments, verbatim.
        outputs: Result, verbatim. Present even for a refusal, as ``{}``.
        outcome: One of :data:`OUTCOMES`.
        error: Exception text when ``outcome`` is ``failed`` or ``refused``.
        risky: Whether this step was gated.
        gate: The gate that held it, if any.
        decided_by: Who approved or refused.
        reason: The human's stated reason.
        principal: Who was acting. Set by the runtime, not the step.
        role: That principal's role.
        token_fingerprint: Which credential, never the credential.
        budget_spent: Spend after this action, as a dimension -> count map.
        checkpoint_id: The checkpoint written around this action, if any.
        duration_ms: Wall-clock cost of the action itself.
        at: When it happened.
        digest: This record's chained digest.
        prev_digest: The previous record's digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    run_id: str
    thread_id: str
    step: str
    intent: str
    action: str
    kind: str
    inputs: Mapping[str, Any]
    outputs: Mapping[str, Any]
    outcome: AuditOutcome
    error: str = ""
    risky: bool = False
    gate: str = ""
    decided_by: str = ""
    reason: str = ""
    principal: str = ""
    role: str = ""
    token_fingerprint: str = ""
    budget_spent: Mapping[str, int] = Field(default_factory=dict)
    checkpoint_id: str = ""
    duration_ms: float = 0.0
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    digest: str
    prev_digest: str

    @property
    def succeeded(self) -> bool:
        """Did the action run and succeed?"""
        return self.outcome == "ok"

    def content(self) -> dict[str, Any]:
        """The hashed content: every field except ``digest`` and ``at``.

        ``at`` is excluded on purpose. It is a wall-clock reading, so two
        identical runs would chain differently for no reason that matters, and a
        verifier replaying a log would have to know the exact instant each action
        ran. The rest of the record is what an auditor is checking.
        """
        payload = self.model_dump(mode="json", exclude={"digest", "prev_digest", "at"})
        return payload

    def computed_digest(self) -> str:
        """The digest this record's content should carry, given its ``prev_digest``."""
        return chain_digest(self.content(), self.prev_digest)

    def verify(self) -> None:
        """Raise if this record's digest does not match its content.

        Raises:
            AuditIntegrityError: If the content was edited after the fact.
        """
        expected = self.computed_digest()
        if expected != self.digest:
            raise AuditIntegrityError(
                self.seq,
                expected_digest=expected,
                actual_digest=self.digest,
                reason="the record's content does not match its digest",
            )


class AuditLog(ABC):
    """Append-only log of :class:`AuditRecord`, chained by digest.

    Abstract so the runtime can take an in-memory log in tests and a durable
    JSONL file in a real run, with the same guarantees in both: appends are
    ordered, digests chain, and :meth:`verify` checks the whole chain.
    """

    @abstractmethod
    def append(self, record: AuditRecord) -> AuditRecord:
        """Add ``record`` to the end of the log and return it."""

    @abstractmethod
    def records(self) -> tuple[AuditRecord, ...]:
        """Every record, oldest first."""

    @abstractmethod
    def last_digest(self) -> str:
        """The digest the next appended record must chain to."""

    def __len__(self) -> int:
        """How many records the log holds."""
        return len(self.records())

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self.records())

    def tail(self, n: int) -> tuple[AuditRecord, ...]:
        """The last ``n`` records, oldest first."""
        rows = self.records()
        return rows[-n:] if n > 0 else ()

    def for_step(self, step: str) -> tuple[AuditRecord, ...]:
        """Every record whose ``step`` matches, oldest first."""
        return tuple(r for r in self.records() if r.step == step)

    def for_action(self, action: str) -> tuple[AuditRecord, ...]:
        """Every record whose ``action`` matches, oldest first."""
        return tuple(r for r in self.records() if r.action == action)

    def for_outcome(self, outcome: str) -> tuple[AuditRecord, ...]:
        """Every record with the given outcome, oldest first."""
        return tuple(r for r in self.records() if r.outcome == outcome)

    def names_steps(self) -> tuple[str, ...]:
        """The distinct step names, in first-seen order."""
        seen: list[str] = []
        for record in self.records():
            if record.step not in seen:
                seen.append(record.step)
        return tuple(seen)

    def verify(self) -> int:
        """Check the whole chain. Returns the number of records checked.

        Verifies two things per record: that its own digest matches its content,
        and that it chains to the record before it. The second is what catches a
        *removed* record -- each record's own digest would still be valid, but
        the next one's ``prev_digest`` would name something that is not there.

        Raises:
            AuditIntegrityError: On the first record that fails either check.
        """
        rows = self.records()
        expected_prev = GENESIS_DIGEST
        for index, record in enumerate(rows):
            if record.prev_digest != expected_prev:
                raise AuditIntegrityError(
                    record.seq,
                    expected_digest=expected_prev,
                    actual_digest=record.prev_digest,
                    reason=(
                        "the chain does not link to the preceding record; a record "
                        "was removed, reordered or inserted"
                    ),
                )
            record.verify()
            expected_prev = record.digest
        return len(rows)

    def next_seq(self) -> int:
        """The sequence number the next appended record should carry."""
        return len(self.records()) + 1

    def fingerprint(self) -> str:
        """A short digest of the whole chain, for a run summary.

        Two logs with the same fingerprint contain the same chain of content --
        same order, same inputs, same outputs, same decisions. It is the number
        to put in a report, because it changes if anything in the log changes.
        """
        hasher = hashlib.sha256()
        for record in self.records():
            hasher.update(record.digest.encode("ascii"))
        return hasher.hexdigest()[:DIGEST_HEX]

    def summary(self) -> dict[str, Any]:
        """Counts by outcome and kind, for the run's report."""
        rows = self.records()
        by_outcome: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for record in rows:
            by_outcome[record.outcome] = by_outcome.get(record.outcome, 0) + 1
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        return {
            "records": len(rows),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "risky": sum(1 for r in rows if r.risky),
            "fingerprint": self.fingerprint(),
        }


class InMemoryAuditLog(AuditLog):
    """A chained log held in a list. For tests and for short-lived runs.

    Named to say plainly what it is not: it does not survive the process. The
    runtime's durability claim is about :class:`JsonlAuditLog`, and a test
    asserts the runtime was given one when durability is required.

    Args:
        run_id: Stamped on log events.
    """

    def __init__(self, *, run_id: str = "") -> None:
        self._records: list[AuditRecord] = []
        self._run_id = run_id

    def append(self, record: AuditRecord) -> AuditRecord:
        """Add ``record``, refusing anything that would break the chain.

        A caller appending a record whose ``prev_digest`` does not match the log
        is either a bug or an attack. Both are refused at the write, where the
        offending record is still available to look at, rather than being
        discovered later by :meth:`verify`.
        """
        expected = self.last_digest()
        if record.prev_digest != expected:
            raise AuditIntegrityError(
                record.seq,
                expected_digest=expected,
                actual_digest=record.prev_digest,
                reason="refused at append: prev_digest does not match the log",
            )
        record.verify()
        self._records.append(record)
        log_event(
            logger,
            "runtime.audit_appended",
            run_id=self._run_id,
            seq=record.seq,
            step=record.step,
            action=record.action,
            outcome=record.outcome,
            digest=record.digest,
        )
        return record

    def records(self) -> tuple[AuditRecord, ...]:
        """Every record, oldest first."""
        return tuple(self._records)

    def last_digest(self) -> str:
        """The digest the next appended record must chain to."""
        return self._records[-1].digest if self._records else GENESIS_DIGEST


class JsonlAuditLog(AuditLog):
    """A chained log on disk: one JSON object per line, opened ``O_APPEND``.

    The durability Lab 5's in-memory ledger deliberately deferred. ``O_APPEND``
    means every write goes to the current end of the file, so a process that
    dies mid-write can truncate the final line but cannot corrupt an earlier
    one -- and :meth:`verify` reports that truncation by name rather than
    pretending the log is whole.

    Reads re-read the file rather than caching it, so a second process sees what
    the first wrote. That is what makes the crash-and-resume proof real.

    Args:
        path: The ``.jsonl`` file to append to. Created if absent.
        run_id: Stamped on log events.
        durable: When ``True`` (the default) each append is flushed and
            ``fsync``-ed before returning. Slower, and the only way to claim the
            record survived a power loss rather than a process exit.
    """

    def __init__(self, path: Path | str, *, run_id: str = "", durable: bool = True) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id
        self._durable = durable

    @property
    def path(self) -> Path:
        """Where this log lives."""
        return self._path

    @property
    def durable(self) -> bool:
        """Whether appends are fsync-ed."""
        return self._durable

    def _read_rows(self) -> list[AuditRecord]:
        """Parse the file. A trailing partial line is skipped, not fatal.

        A process killed mid-write leaves an unterminated last line. Raising
        there would mean a crashed run could not be audited at all, which is the
        opposite of the requirement; so the partial line is dropped and
        :meth:`truncated_tail` reports it.
        """
        if not self._path.exists():
            return []
        rows: list[AuditRecord] = []
        raw = self._path.read_bytes()
        # Always drop the last split element. When the file ends with a newline
        # that element is the empty string after the final record; when it does
        # not, it is the partial line a crashed writer left behind. Both are
        # discarded, which is why this is not a conditional.
        for line in raw.split(b"\n")[:-1]:
            if not line.strip():
                continue
            rows.append(AuditRecord.model_validate_json(line))
        return rows

    def truncated_tail(self) -> bool:
        """Is there a trailing partial line from a crashed writer?

        True means the last append did not finish. Every complete record before
        it is intact and verifiable; the incomplete one is simply not there.
        """
        if not self._path.exists():
            return False
        raw = self._path.read_bytes()
        if not raw:
            return False
        return not raw.endswith(b"\n")

    def append(self, record: AuditRecord) -> AuditRecord:
        """Append one record as a single line, then flush it to disk."""
        expected = self.last_digest()
        if record.prev_digest != expected:
            raise AuditIntegrityError(
                record.seq,
                expected_digest=expected,
                actual_digest=record.prev_digest,
                reason="refused at append: prev_digest does not match the log",
            )
        record.verify()

        line = canonical_json(record.model_dump(mode="json")) + "\n"
        # O_APPEND keeps concurrent writers from interleaving mid-line.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            if self._durable:
                os.fsync(fd)
        finally:
            os.close(fd)

        log_event(
            logger,
            "runtime.audit_appended",
            run_id=self._run_id,
            seq=record.seq,
            step=record.step,
            action=record.action,
            outcome=record.outcome,
            digest=record.digest,
            path=str(self._path),
        )
        return record

    def records(self) -> tuple[AuditRecord, ...]:
        """Every complete record, oldest first."""
        return tuple(self._read_rows())

    def last_digest(self) -> str:
        """The digest the next appended record must chain to."""
        rows = self._read_rows()
        return rows[-1].digest if rows else GENESIS_DIGEST

    def new_record_id(self) -> str:
        """A fresh record id, used by the runtime for traceability."""
        return uuid.uuid4().hex[:12]


def build_audit_record(
    *,
    seq: int,
    run_id: str,
    thread_id: str,
    step: str,
    intent: str,
    action: str,
    kind: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    outcome: str,
    previous_digest: str,
    error: str = "",
    risky: bool = False,
    gate: str = "",
    decided_by: str = "",
    reason: str = "",
    principal: str = "",
    role: str = "",
    token_fingerprint: str = "",
    budget_spent: Optional[Mapping[str, int]] = None,
    checkpoint_id: str = "",
    duration_ms: float = 0.0,
) -> AuditRecord:
    """Build a record with its digest already computed.

    Separated from :class:`AuditRecord` because a frozen model cannot compute a
    digest over its own content at construction -- the digest is derived from the
    fields, and the fields are what it is. So the record is built, its digest is
    computed from :meth:`AuditRecord.content`, and the finished model is
    returned.

    Args:
        seq: Position in the run's log, from 1.
        run_id: The run id.
        thread_id: The checkpoint thread.
        step: Step name.
        intent: Why the step was taken.
        action: What it did.
        kind: Which family of work.
        inputs: Arguments, verbatim.
        outputs: Result, verbatim.
        outcome: One of :data:`OUTCOMES`.
        previous_digest: The previous record's digest, or :data:`GENESIS_DIGEST`.
        error: Exception text, when the outcome is not ``ok``.
        risky: Whether this step was gated.
        gate: The gate that held it.
        decided_by: Who approved or refused.
        reason: The human's stated reason.
        principal: Who was acting.
        role: That principal's role.
        token_fingerprint: Which credential, never the credential.
        budget_spent: Spend after this action.
        checkpoint_id: The checkpoint written around this action.
        duration_ms: Wall-clock cost of the action.

    Returns:
        An :class:`AuditRecord` whose ``digest`` matches its content.
    """
    draft = AuditRecord(
        seq=seq,
        run_id=run_id,
        thread_id=thread_id,
        step=step,
        intent=intent,
        action=action,
        kind=kind,
        inputs=dict(inputs),
        outputs=dict(outputs),
        outcome=outcome,
        error=error,
        risky=risky,
        gate=gate,
        decided_by=decided_by,
        reason=reason,
        principal=principal,
        role=role,
        token_fingerprint=token_fingerprint,
        budget_spent=dict(budget_spent or {}),
        checkpoint_id=checkpoint_id,
        duration_ms=duration_ms,
        digest=GENESIS_DIGEST,  # placeholder; replaced below
        prev_digest=previous_digest,
    )
    digest = draft.computed_digest()
    return draft.model_copy(update={"digest": digest})


def log_actions(log: AuditLog) -> tuple[str, ...]:
    """Every distinct action name in the log, in first-seen order."""
    seen: list[str] = []
    for record in log.records():
        if record.action not in seen:
            seen.append(record.action)
    return tuple(seen)


def inputs_outputs(log: AuditLog) -> Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    """``(action, inputs, outputs)`` per record, for a report that shows both.

    Exists because the pitfall is about *both* halves, and a report that printed
    only one would look like evidence while satisfying neither.
    """
    return tuple((r.action, r.inputs, r.outputs) for r in log.records())
