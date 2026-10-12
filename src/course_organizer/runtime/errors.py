"""Errors the governed runtime raises, and why each one is its own type.

Every class here exists because an operator or an auditor needs to tell these
failures apart. "The run stopped" is not actionable; "the budget for
``tool_calls`` was exhausted, 40 of 40 spent, 1 more requested" is.

Doc 2.10 Lab 6 states the deliverable as *"Runtime wrapping the agent: budgets,
gates, checkpoint/resume, audit log"* and accepts it with *"Every action is
logged with inputs/outputs; a crashed run resumes from its last checkpoint."*
Both halves of that acceptance criterion are about the failure path: what the
runtime records when it stops, and what it can still do after a crash. A single
opaque ``RuntimeError`` would satisfy neither, because an audit reader could not
tell a **policy** refusal (the gate said no) from a **capacity** refusal (the
budget ran out) from an **integrity** failure (the audit chain no longer
verifies). Those three demand different responses, so they are different types.

The distinction that matters most is :class:`BudgetExceeded` versus
:class:`GateRefused`. A budget is the Coordinator's own limit on itself, and
running out is a normal, expected outcome to be reported to a human. A gate
refusal is a *human declining to authorise a consequential action*, and it is
never an error to retry silently -- doc 2.12: *"No worklet takes a consequential
action autonomously - the human authority named in this chapter signs off before
anything is published."*
"""

from __future__ import annotations

__all__ = [
    "AuditIntegrityError",
    "BudgetExceeded",
    "CheckpointUnavailable",
    "GateRefused",
    "ResumeDivergence",
    "RuntimeBoundaryError",
]


class RuntimeBoundaryError(RuntimeError):
    """Base class for every governed-runtime failure.

    Catch this to mean "the runtime refused or could not continue the run". The
    subclasses are the useful ones; this one exists so a caller can trap the
    whole family without knowing the taxonomy in advance.
    """


def _terminate(text: str) -> str:
    """Return ``text`` ending in exactly one full stop.

    These messages are composed out of pieces that are themselves sentences: a
    budget refusal quotes the ceiling arithmetic, a gate refusal quotes the
    human's reason verbatim. Gluing two terminated sentences together produces
    ``...overspending.. Nothing ran``, and a doubled full stop is the one piece
    of a sentence a reader is certain nobody proofread.

    Args:
        text: The clause to close. May be empty, and may already end in ``.``,
            ``!``, ``?`` or a closing quote after one of those.

    Returns:
        The clause, trimmed, with a single trailing full stop. Empty stays empty.
    """
    body = text.strip()
    if not body:
        return ""
    for suffix in ('."', ".'", '.")', ".'"):
        if body.endswith(suffix):
            return body
    return body if body[-1] in ".!?" else body + "."


class BudgetExceeded(RuntimeBoundaryError):
    """A budget dimension would be exceeded by the requested action.

    Raised **before** the action runs, by :meth:`Budget.reserve`. That ordering
    is the whole point: a budget that is checked after the work is a report, not
    a budget. Doc 2.12 lists *"Runaway loops"* as a risk whose mitigation is
    *"Bound MAX_ATTEMPTS (Lab 7/10); on exhaustion, escalate to course faculty
    with a diagnosis."* -- so this error carries enough detail to be that
    diagnosis, and the runtime records the refusal on the audit log rather than
    raising and forgetting.

    Attributes:
        dimension: Which countable resource ran out, e.g. ``"tool_calls"``.
        limit: The configured ceiling for that dimension.
        spent: How much was already spent when the request arrived.
        requested: How much the refused action would have cost.
        step: Name of the step that was refused, if it was known.
    """

    def __init__(
        self,
        dimension: str,
        *,
        limit: int,
        spent: int,
        requested: int,
        step: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.limit = limit
        self.spent = spent
        self.requested = requested
        self.step = step

        where = f" before step {step!r}" if step else ""
        super().__init__(
            _terminate(
                f"budget {dimension!r} exhausted{where}: {spent} of {limit} "
                f"spent, and the next action requests {requested}. Doc 2.12 "
                f"requires MAX_ATTEMPTS to be bounded and the run escalated to "
                f"course faculty on exhaustion; the runtime refused rather than "
                f"overspending"
            )
        )


class GateRefused(RuntimeBoundaryError):
    """A step marked risky was not authorised, so it did not run.

    This is the runtime's hard stop. Doc 2.10 Lab 6's pseudocode reads
    ``if step.risky: wait_for_approval(step)``, and the document's guardrails
    make the human the final authority for anything consequential. A refusal is
    therefore a **successful** exercise of governance, not a malfunction: it is
    recorded on the audit log with the approver's identity and reason, and the
    run stops rather than proceeding.

    The runtime never retries a refused gate on its own. A retry would be the
    runtime asking a human a question it already knows the answer to.

    Attributes:
        step: The step that was held back.
        gate: Name of the gate that refused, e.g. ``"publish"``.
        decided_by: Who decided, or ``"unanswered"`` if nobody replied.
        reason: The human's stated reason, verbatim.
        request: The approval request as presented, for the audit record.
    """

    def __init__(
        self,
        step: str,
        *,
        gate: str,
        decided_by: str = "unanswered",
        reason: str = "",
        request: object | None = None,
    ) -> None:
        self.step = step
        self.gate = gate
        self.decided_by = decided_by
        self.reason = reason
        self.request = request

        by = f" by {decided_by}" if decided_by != "unanswered" else ""
        why = f" Reason: {_terminate(reason)}" if reason else ""
        super().__init__(
            _terminate(
                f"step {step!r} held at the {gate!r} gate{by} and was not run.{why} "
                f"Doc 2.12: no consequential action is taken without the named "
                f"human authority's approval. The refusal is on the audit log; "
                f"the runtime will not retry it"
            )
        )


class CheckpointUnavailable(RuntimeBoundaryError):
    """A resume was asked for a thread that has no checkpoint to resume from.

    Raised instead of silently starting a fresh run, because starting over is
    the one behaviour a resume must never have: doc 2.10 Lab 6's pitfall is that
    a resume which loses state *"loses state"* silently, and a caller who typo'd
    a thread id deserves to hear about it rather than get a clean-looking empty
    result.

    Attributes:
        thread_id: The thread that was asked for.
        known_threads: What was actually available, so the caller can see the
            typo rather than guess at it.
    """

    def __init__(self, thread_id: str, known_threads: tuple[str, ...] = ()) -> None:
        self.thread_id = thread_id
        self.known_threads = known_threads

        known = ", ".join(known_threads) if known_threads else "(none)"
        super().__init__(
            f"no checkpoint for thread {thread_id!r}. Threads on this store: "
            f"{known}. Refusing to start a new run in its place -- a resume "
            f"that silently begins from zero is the failure doc 2.10 Lab 6 warns "
            f"about."
        )


class ResumeDivergence(RuntimeBoundaryError):
    """A resumed run produced a different result than the run it replaced.

    Resume is only honest if it is *equivalent*. This fires when the runtime
    re-executes a plan against a checkpoint and the result does not match what
    the uninterrupted run produced -- which means either the checkpoint is not
    capturing the state the steps depend on, or a step is not a pure function
    of that state.

    It is raised for a second thing, at resume time: the runtime's audit log is
    not the log the checkpoint was written against. That is a wiring mistake
    rather than a divergence of results, but it is the same failure to the caller
    -- "the thing you handed me does not go with the thing you are resuming" --
    and one type is better than two that are told apart by message text.
    :attr:`reason` says which, so the message names the actual problem.

    Attributes:
        thread_id: The resumed thread.
        first_difference: Name of the field that differed.
        expected: The value from the uninterrupted run.
        actual: The value the resumed run produced.
        reason: What was being compared. Defaults to the result comparison; pass
            ``"the audit log"`` when the mismatch is the log's chain position.
    """

    def __init__(
        self,
        thread_id: str,
        *,
        first_difference: str,
        expected: object,
        actual: object,
        reason: str = "the uninterrupted run",
    ) -> None:
        self.thread_id = thread_id
        self.first_difference = first_difference
        self.expected = expected
        self.actual = actual
        self.reason = reason

        super().__init__(
            f"resumed run {thread_id!r} diverged from {reason} at "
            f"{first_difference!r}: expected {expected!r}, got {actual!r}. A "
            f"checkpoint that does not capture every input a step reads from "
            f"produces exactly this."
        )


class AuditIntegrityError(RuntimeBoundaryError):
    """The audit log's hash chain does not verify.

    :mod:`course_organizer.runtime.audit` chains each record to the digest of
    the one before it, so a record that has been edited, removed or reordered in
    the log file is detectable rather than merely improbable. Doc 2.12 requires
    *"a full audit log of every grade published and who approved it"* -- and a
    log that can be silently rewritten after the fact does not support that
    claim, so a broken chain is reported as loudly as a failed write.

    Attributes:
        seq: Sequence number of the first record that failed to verify.
        expected_digest: The digest the chain said this record should have.
        actual_digest: The digest it carries.
        reason: A short human description of what went wrong.
    """

    def __init__(
        self,
        seq: int,
        *,
        expected_digest: str,
        actual_digest: str,
        reason: str,
    ) -> None:
        self.seq = seq
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        self.reason = reason

        super().__init__(
            f"audit chain broken at record {seq}: {reason}. Expected digest "
            f"{expected_digest[:16]}..., log carries {actual_digest[:16]}.... "
            f"Doc 2.12 requires a full audit log of what was published and who "
            f"approved it; a log that fails its own integrity check cannot be "
            f"relied on for that."
        )
