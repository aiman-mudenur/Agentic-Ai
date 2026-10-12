"""The runtime controller: skills + tools + memory behind budgets, gates and a log.

Doc 2.10 Lab 6's pseudocode, in full:

    run = Runtime(budget=B, gates=[publish])
    for step in agent.loop():
        audit.log(step.intent, step.action, step.result)
        if step.risky: wait_for_approval(step)
        checkpoint(run.state)

and the pitfall printed directly underneath it:

    **Pitfall to avoid.** Log inputs and outputs, not just messages;
    checkpoint before risky steps or resume loses state.

The pseudocode and the pitfall disagree about order, and that disagreement is the
lab's actual content. Read literally, the loop audits **before** it gates, so a
refused risky step is never logged with the inputs that were proposed for it;
and it checkpoints **after** the gate, so a run that crashes inside
``wait_for_approval`` comes back to a state that does not record having asked.
The pitfall line is the correction to both. This module implements the corrected
order and says which clause of the document each decision answers.

The order this module actually runs, for every step:

    1. reserve budget                     -> refused *before* the action
    2. check wall clock                   -> catch an overrun already spent
    3. if risky: checkpoint, then gate    -> the pitfall's second clause
    4. run the step
    5. charge what it actually cost
    6. audit record with inputs AND outputs -> the pitfall's first clause
    7. checkpoint and advance the cursor

Two properties are worth naming because they are what make a resume honest, and
both are asserted by tests rather than described:

* **A risky step is checkpointed before the gate is consulted.** If the
  process dies while waiting for a human, the resumed run knows the question was
  asked. Without that checkpoint, resume re-asks and the audit contains one
  question and two answers.
* **The audit chain position travels in the checkpoint.** A resumed run appends
  to the *same* hash chain, so "every action is logged" stays true across the
  crash instead of becoming two half-logs in one file.

Lab 7 wraps this controller in a LangGraph node graph so a human can answer the
gate from outside the process. Nothing here changes for that: this class owns
ordering, and the graph owns topology.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from course_organizer.logging_setup import log_event
from course_organizer.runtime.audit import (
    GENESIS_DIGEST,
    AuditLog,
    AuditRecord,
    build_audit_record,
)
from course_organizer.runtime.budget import Budget, BudgetLedger, BudgetSnapshot
from course_organizer.runtime.checkpoint import (
    SOURCE_LOOP,
    SOURCE_RESUME,
    CheckpointRef,
    CheckpointStore,
    RunState,
)
from course_organizer.runtime.errors import (
    BudgetExceeded,
    GateRefused,
    ResumeDivergence,
    RuntimeBoundaryError,
    _terminate,
)
from course_organizer.runtime.gates import (
    UNANSWERED,
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GATE",
    "DEFAULT_PRINCIPAL",
    "HUMAN_AUTHORITY",
    "RunReport",
    "Runtime",
    "Step",
    "StepContext",
    "StepOutcome",
    "StepStatus",
]

#: Doc 2.10's Chapter 2 header names the human authority for this worklet:
#: *"Human authority: course faculty."* Every approval in Lab 6 is attributed to
#: this name unless a gate says otherwise, and an unattributed approval would
#: fail doc 2.12's *"and who approved it"*.
HUMAN_AUTHORITY = "course_faculty"

#: The gate name the document's own pseudocode uses: ``gates=[publish]``.
DEFAULT_GATE = "publish"

#: Who the runtime acts as when no principal is supplied. The Coordinator, which
#: the Glossary defines as the agent that *"owns the run-state, plan, budgets and
#: the audit log"*.
DEFAULT_PRINCIPAL = "apniLeap AURA Coordinator"

#: Statuses a step can end in. ``blocked`` is distinct from ``refused`` for the
#: same reason the gate's ``deferred`` is: nobody answered is not a "no".
StepStatus = str

STEP_STATUSES: tuple[str, ...] = ("ok", "failed", "refused", "deferred", "skipped")

#: Which budget dimension each kind of step is charged to. A step whose kind is
#: absent here is charged nothing, which is a deliberate hole: a new kind of work
#: should have to be added to this table before it can consume budget, so that
#: "the budget bounds everything" is a property of the table rather than a hope.
KIND_DIMENSION: Mapping[str, str] = {
    "tool": "tool_calls",
    "skill": "skill_calls",
    "connector": "connector_calls",
    "llm": "llm_calls",
    "memory": "skill_calls",
}


@dataclass(frozen=True)
class StepContext:
    """What a step is given when the runtime calls it.

    A step receives its prior outputs rather than being handed the whole runtime,
    so a step cannot checkpoint, charge a budget or write to the audit log. That
    is the point of the boundary: every action in this worklet is logged because
    the logging happens around the call, not because the step remembered to.

    Attributes:
        step: The step's name.
        index: Its position in the plan.
        artifacts: Outputs of every completed step, keyed by step name.
        state: The current :class:`~course_organizer.runtime.checkpoint.RunState`.
        principal: Who is acting.
        role: That principal's role.
        intent: The step's intent, repeated here so a step can log it without
            reaching back for the plan.
    """

    step: str
    index: int
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    state: Optional[RunState] = None
    principal: str = DEFAULT_PRINCIPAL
    role: str = ""
    intent: str = ""

    def prior(self, name: str, default: Any = None) -> Any:
        """One earlier step's output, or ``default``."""
        return self.artifacts.get(name, default)


@dataclass(frozen=True)
class Step:
    """One unit of work, with what it costs and whether it is risky.

    Not a Pydantic model: it holds a callable, which has no useful JSON form. The
    things that *do* need to be serialisable -- name, intent, kind, cost, risk --
    are plain fields the runtime copies into the audit record and the checkpoint
    metadata, so the persisted evidence is complete even though the plan itself
    is not stored.

    Attributes:
        name: Step name, unique within a plan. Used as the artifact key.
        intent: Why the step is being taken. Goes on the audit record.
        action: What the step does, usually a tool or skill name. Goes on the
            audit record.
        kind: Which family of work: ``tool``, ``skill``, ``connector``, ``llm``,
            ``memory``. Selects the budget dimension charged.
        run: The callable. Receives a :class:`StepContext`, returns a mapping
            that becomes the step's audited ``outputs``.
        risky: Whether this step needs authorisation before it runs. Doc 2.10's
            ``if step.risky``.
        gate: Which gate to consult. Required when ``risky`` is true.
        cost: Extra budget to charge beyond the one call the ``kind`` implies,
            e.g. ``{"records_read": 8}``.
        summary: The one-line consequence a human reads at the gate. Built by
            the caller of the plan, not by the step.
        allow_empty_output: Whether ``run`` may return an empty mapping. Steps
            that produce an artifact are held to a non-empty ``outputs`` so that
            "every action is logged with inputs and outputs" cannot be satisfied
            vacuously by a step that logged nothing.
    """

    name: str
    intent: str
    action: str
    kind: str
    run: Callable[[StepContext], Mapping[str, Any]]
    risky: bool = False
    gate: str = ""
    cost: Mapping[str, int] = field(default_factory=dict)
    summary: str = ""
    allow_empty_output: bool = False


@dataclass(frozen=True)
class StepOutcome:
    """How one step ended.

    Attributes:
        step: The step's name.
        index: Its position in the plan.
        status: One of :data:`STEP_STATUSES`.
        outputs: What it produced, as audited.
        error: Exception text, when it failed or was refused.
        decision: The gate's answer, if a gate was consulted.
        request: The approval request, if a gate was consulted.
        record: The audit record for this step.
        checkpoint_before: The checkpoint written *before* a risky step.
        checkpoint_after: The checkpoint written after.
        duration_ms: Wall-clock cost of the step itself.
    """

    step: str
    index: int
    status: StepStatus
    outputs: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    decision: Optional[ApprovalDecision] = None
    request: Optional[ApprovalRequest] = None
    record: Optional[AuditRecord] = None
    checkpoint_before: Optional[CheckpointRef] = None
    checkpoint_after: Optional[CheckpointRef] = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Did the step run and succeed?"""
        return self.status == "ok"


@dataclass(frozen=True)
class RunReport:
    """Everything one run did, in a form a report or a test can read.

    Attributes:
        run_id: Which run.
        thread_id: The resume key.
        status: ``completed``, ``stopped`` (a gate or budget stopped it), or
            ``failed``.
        outcomes: One per step that was *attempted*, in order. Steps skipped by a
            resume are absent, which is how "did not re-run" is checked.
        skipped: Names of steps a resume did not re-execute.
        audit_fingerprint: Digest of the whole audit chain at the end.
        audit_records: How many records the chain holds, across all processes.
        budget: Per-dimension spend, limit and refusal count.
        gate_answers: Every non-approval decision, as rendered strings.
        checkpoints: How many checkpoints this process wrote.
        duration_ms: The run's own wall clock.
        escalated: Whether the run stopped for a reason the document says must
            reach course faculty -- a budget overrun, per doc 2.12's MAX_ATTEMPTS
            mitigation.
        stopped_at: The step the run stopped at, empty if it did not stop.
        diagnosis: Why it stopped, in one sentence, addressed to whoever picks
            the run up. Doc 2.12 says to escalate "with a diagnosis"; a bare
            ``escalated=True`` is the flag without the diagnosis, and a report
            that writes the reason only to a log tail cannot be read by the
            faculty member it is meant for.
    """

    run_id: str
    thread_id: str
    status: str
    outcomes: tuple[StepOutcome, ...] = ()
    skipped: tuple[str, ...] = ()
    audit_fingerprint: str = ""
    audit_records: int = 0
    budget: tuple[BudgetSnapshot, ...] = ()
    gate_answers: tuple[str, ...] = ()
    checkpoints: int = 0
    duration_ms: float = 0.0
    escalated: bool = False
    stopped_at: str = ""
    diagnosis: str = ""

    @property
    def ok(self) -> bool:
        """Did the run reach its last step?"""
        return self.status == "completed"

    def outcome_for(self, step: str) -> Optional[StepOutcome]:
        """The outcome for a named step, or ``None`` if it was not attempted."""
        for outcome in self.outcomes:
            if outcome.step == step:
                return outcome
        return None

    def steps_run(self) -> tuple[str, ...]:
        """Which steps actually ran, in order."""
        return tuple(o.step for o in self.outcomes)

    def outputs_of(self, step: str) -> Mapping[str, Any]:
        """A step's audited outputs, or ``{}``."""
        found = self.outcome_for(step)
        return found.outputs if found else {}

    def statuses(self) -> tuple[str, ...]:
        """Every status, in order."""
        return tuple(o.status for o in self.outcomes)


class Runtime:
    """Executes a plan under a budget, behind gates, into an audit log.

    Construct it once per process, then :meth:`run` or :meth:`resume`. The
    ordering guarantees live in :meth:`execute_step`, which is the only place a
    step is called from -- so "the gate is consulted before the action" and "a
    risky step is checkpointed before the gate" are structural rather than a
    convention that a future step author could route around.

    Args:
        budget: The ceilings to enforce.
        gates: Approval gates by name. A step names one with ``Step.gate``.
        audit: Where every action is recorded.
        checkpoints: Where run state is stored.
        principal: Who the runtime acts as. Stamped on every audit record by the
            runtime, never by a step, so a step cannot log itself under someone
            else's identity.
        role: That principal's role, for the connector's access model.
        token_fingerprint: Which credential was in use, if any. Lab 5's
            convention: enough to tell which token, never the token.
        run_id: Which run. A random one is generated if not given. Because
            ``thread_id`` defaults to it, **a resuming caller must pass
            ``thread_id`` explicitly** -- two runtimes built with the same
            arguments get different ``run_id`` values, and only the caller knows
            which thread it is coming back to.
        thread_id: The resume key. Defaults to ``run_id``.
        course_code: The course, carried so a resume can assert it is the same
            work.
        plan_sha256: Digest of the plan, so a resume against a different plan is
            refused rather than silently mismatched.

    Raises:
        ValueError: If the same gate name is registered twice, if a gate name is
            empty, or if ``principal`` is blank. A blank principal would put an
            unattributed actor on the audit log, which fails doc 2.12's *"and who
            approved it"* at the root.
    """

    def __init__(
        self,
        *,
        budget: Optional[Budget] = None,
        gates: Optional[Union[Mapping[str, ApprovalGate], Sequence[tuple[str, ApprovalGate]]]] = None,
        audit: AuditLog,
        checkpoints: CheckpointStore,
        principal: str = DEFAULT_PRINCIPAL,
        role: str = "",
        token_fingerprint: str = "",
        run_id: str = "",
        thread_id: str = "",
        course_code: str = "",
        plan_sha256: str = "",
    ) -> None:
        if not principal.strip():
            raise ValueError(
                "principal must not be blank: doc 2.12 requires the audit log to "
                "record *who* approved each action, and an unnamed principal "
                "cannot satisfy it."
            )
        self._budget = budget if budget is not None else Budget.defaults()
        self._gates: dict[str, ApprovalGate] = {}
        for gate_name, gate in self._gate_pairs(gates):
            if not gate_name.strip():
                raise ValueError("gate names must not be blank")
            if not isinstance(gate, ApprovalGate):
                raise TypeError(
                    f"gate {gate_name!r} is a {type(gate).__name__}, not an "
                    f"ApprovalGate. A step that finds a non-gate registered under "
                    f"its gate name would be ungated in everything but name."
                )
            if gate_name in self._gates:
                raise ValueError(
                    f"gate {gate_name!r} registered twice. A step names a gate by "
                    f"string, so two gates under one name would make the choice "
                    f"ambiguous on the audit log. Pass a sequence of pairs rather "
                    f"than a dict if you need to register the same name twice by "
                    f"mistake -- a dict collapses the duplicate before this check "
                    f"can see it."
                )
            self._gates[gate_name] = gate
        self._audit = audit
        self._checkpoints = checkpoints
        self._principal = principal
        self._role = role
        self._token_fingerprint = token_fingerprint
        self._run_id = run_id or self._next_run_id()
        self._thread_id = thread_id or self._run_id
        self._course_code = course_code
        self._plan_sha256 = plan_sha256

        self._ledger = BudgetLedger(self._budget, run_id=self._run_id)
        self._state = RunState(
            thread_id=self._thread_id,
            run_id=self._run_id,
            course_code=course_code,
            plan_sha256=plan_sha256,
        )
        self._outcomes: list[StepOutcome] = []
        self._gate_answers: list[str] = []
        self._checkpoints_written = 0
        self._resumed_from: Optional[CheckpointRef] = None

    @staticmethod
    def _gate_pairs(
        gates: Optional[Union[Mapping[str, ApprovalGate], Sequence[tuple[str, ApprovalGate]]]],
    ) -> tuple[tuple[str, ApprovalGate], ...]:
        """Normalise ``gates`` to pairs, preserving any duplicate names.

        A ``dict`` cannot express a duplicate key, so accepting pairs as well is
        what makes the duplicate check in ``__init__`` reachable at all. It also
        keeps registration order, which a report can then show.
        """
        if gates is None:
            return ()
        if isinstance(gates, Mapping):
            return tuple(gates.items())
        return tuple((name, gate) for name, gate in gates)

    def _next_run_id(self) -> str:
        """A unique id for this run, when the caller did not supply one.

        Random, and deliberately not derived from anything about *this object*.
        An earlier version built the id from ``id(self)``, which CPython reuses
        after garbage collection, so two runtimes created in sequence could land
        on the same ``run_id`` -- and since ``thread_id`` defaults to ``run_id``,
        that is a collision on the resume key, where the second run would adopt
        the first's checkpoint. A collision here is not a cosmetic id clash; it
        is one run silently resuming another.
        """
        return f"run-{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------- accessors

    @property
    def run_id(self) -> str:
        """Which run this runtime is executing."""
        return self._run_id

    @property
    def thread_id(self) -> str:
        """The resume key."""
        return self._thread_id

    @property
    def principal(self) -> str:
        """Who the runtime acts as."""
        return self._principal

    @property
    def budget(self) -> Budget:
        """The ceilings in force."""
        return self._budget

    @property
    def ledger(self) -> BudgetLedger:
        """The budget ledger, for a caller that wants its own reservations."""
        return self._ledger

    @property
    def audit(self) -> AuditLog:
        """The audit log."""
        return self._audit

    @property
    def store(self) -> CheckpointStore:
        """The checkpoint store."""
        return self._checkpoints

    @property
    def state(self) -> RunState:
        """The current run state. Frozen; advanced by :meth:`run`."""
        return self._state

    @property
    def outcomes(self) -> tuple[StepOutcome, ...]:
        """Every step outcome this process produced."""
        return tuple(self._outcomes)

    @property
    def gates(self) -> tuple[str, ...]:
        """The registered gate names, in registration order."""
        return tuple(self._gates)

    def gate(self, name: str) -> ApprovalGate:
        """One registered gate.

        Raises:
            KeyError: If no gate is registered under ``name``.
        """
        return self._gates[name]

    def checkpoint_history(self) -> tuple[CheckpointRef, ...]:
        """Every checkpoint on this thread, newest first, across all processes."""
        return self._checkpoints.history(self._thread_id)

    def resumed_from(self) -> Optional[CheckpointRef]:
        """The checkpoint a :meth:`resume` started from, if it was a resume."""
        return self._resumed_from

    # ----------------------------------------------------------------- resume

    def resume(self, thread_id: Optional[str] = None) -> RunState:
        """Adopt a crashed run's state, including where its audit chain stopped.

        Three things are restored, and the third is the one that is easy to miss:
        the **cursor**, the **budget spend**, and the **audit chain position**.
        Restoring the cursor without the spend lets a resumed run overspend,
        because the crashed process's spending is invisible to it. Restoring the
        spend without the chain position starts a second hash chain in the same
        file. Both failures are silent, and both leave a log that looks complete.

        Args:
            thread_id: The thread to resume. Defaults to this runtime's own.

        Returns:
            The restored state.

        Raises:
            CheckpointUnavailable: If the thread has no checkpoint. The runtime
                will not start over in its place.
            ResumeDivergence: If the checkpoint is for a different course or a
                different plan than this runtime was built for.
        """
        target = thread_id or self._thread_id
        restored = self._checkpoints.require(target)
        history = self._checkpoints.history(target)
        self._resumed_from = history[0] if history else None

        self._assert_same_work(restored)
        self._assert_same_log(restored)

        self._state = restored
        self._ledger.restore(restored.spent)
        log_event(
            logger,
            "runtime.resumed",
            run_id=self._run_id,
            thread_id=target,
            step=restored.step_index,
            cursor=restored.cursor,
            completed=len(restored.completed),
            audit_seq=restored.audit_seq,
            audit_digest=restored.audit_digest,
        )
        return restored

    def _assert_same_work(self, restored: RunState) -> None:
        """Refuse to resume a checkpoint belonging to different work.

        A checkpoint carries the course and a digest of the plan. Applying step
       4's expectations to step 2's inputs would produce a run that completes
        and is wrong, which is worse than one that refuses.
        """
        if self._course_code and restored.course_code and restored.course_code != self._course_code:
            raise ResumeDivergence(
                self._thread_id,
                first_difference="course_code",
                expected=self._course_code,
                actual=restored.course_code,
            )
        if self._plan_sha256 and restored.plan_sha256 and restored.plan_sha256 != self._plan_sha256:
            raise ResumeDivergence(
                self._thread_id,
                first_difference="plan_sha256",
                expected=self._plan_sha256,
                actual=restored.plan_sha256,
            )

    # ------------------------------------------------------------ execution

    def run(self, plan: Sequence[Step]) -> RunReport:
        """Execute ``plan`` from the current cursor and report what happened.

        Steps the state already lists as completed are skipped, not re-run. That
        is the whole point of the cursor: a publish step that runs twice is a
        duplicated side effect, not a slow run.

        A step that raises :class:`RuntimeBoundaryError` -- a budget refusal or a
        gate refusal -- **stops** the run and is reported as ``stopped``. That is
        not this method swallowing an error; it is the run finishing, in the
        sense that matters: it stopped at a governed boundary with its state and
        audit log intact and resumable.

        A step that raises anything else is a bug in the step, and is reported as
        ``failed`` after being audited with its exception text. The run still
        stops, because the document's pitfall is explicit that a step whose
        outcome is unknown must not be followed by a step that assumes it
        succeeded.
        """
        started = time.perf_counter()
        status = "completed"
        escalated = False
        stopped_at = ""
        diagnosis = ""
        pending = list(plan)

        for position, step in enumerate(pending):
            if self._state.is_done(step.name):
                continue
            # The next not-yet-done step after this one, so the cursor stored in
            # the checkpoint names where a resume should pick up.
            following = ""
            for later in pending[position + 1 :]:
                if not self._state.is_done(later.name):
                    following = later.name
                    break
            try:
                outcome = self.execute_step(step, position, next_step=following)
            except BudgetExceeded as exc:
                escalated = True
                # Built from the exception's fields rather than by embedding its
                # message: the message already names the step, so quoting it
                # repeated the step name and doubled the full stop at the seam.
                diagnosis = _terminate(
                    f"budget refused step {step.name!r}: the "
                    f"{exc.dimension!r} budget is exhausted, {exc.spent} of "
                    f"{exc.limit} spent and this step requests {exc.requested}. "
                    f"Nothing ran, so there is nothing to undo. Raise the ceiling "
                    f"deliberately or shorten the plan; do not lower MAX_ATTEMPTS "
                    f"to get past it"
                )
                log_event(
                    logger,
                    "runtime.run_stopped",
                    run_id=self._run_id,
                    step=step.name,
                    reason="budget",
                    error=str(exc),
                )
                status = "stopped"
                stopped_at = step.name
                break
            except GateRefused as exc:
                # Same reason as above, and a second one: the diagnosis is what a
                # person picks the run up with, so it must add something rather
                # than restate what the exception already said.
                verdict = (
                    f"refused it, decided by {exc.decided_by}"
                    if exc.decided_by != "unanswered"
                    else "left it unanswered, which counts as a refusal"
                )
                reason = f" Reason: {_terminate(exc.reason)}" if exc.reason else ""
                diagnosis = _terminate(
                    f"step {step.name!r} did not run: the {exc.gate!r} gate "
                    f"{verdict}.{reason} The runtime will not retry it, so the run "
                    f"needs a new decision, not another attempt"
                )
                log_event(
                    logger,
                    "runtime.run_stopped",
                    run_id=self._run_id,
                    step=step.name,
                    reason="gate",
                    error=str(exc),
                )
                status = "stopped"
                stopped_at = step.name
                break
            self._outcomes.append(outcome)
            if outcome.status == "failed":
                status = "failed"
                stopped_at = step.name
                # "did not complete", not "raised": a step can fail by returning
                # nothing, and that failure carries no exception at all.
                diagnosis = _terminate(
                    f"step {step.name!r} did not complete: "
                    f"{_terminate(outcome.error)} Checkpoints up to the previous "
                    f"step are intact, so a resume starts from there"
                )
                break

        return self._report(
            status=status,
            escalated=escalated,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            stopped_at=stopped_at,
            diagnosis=diagnosis,
        )

    def execute_step(self, step: Step, index: int = 0, *, next_step: str = "") -> StepOutcome:
        """Run one step under every control. The only place a step is called.

        The order is the module's whole argument, so it is spelled out in
        comments rather than left to be inferred:

        1. reserve the budget for this step's kind
        2. check the wall clock for an overrun already spent
        3. if risky: **checkpoint**, then ask the gate
        4. call the step
        5. charge what it actually cost
        6. audit with inputs *and* outputs
        7. checkpoint and advance the cursor

        Args:
            step: The step to run.
            index: Its position in the plan.
            next_step: The name of the step after this one, written into the
                state's ``cursor``. The cursor is what a resuming process reads
                to know where to continue, so it must name the next step rather
                than being blank -- a blank cursor is indistinguishable from
                "the run finished", and a crash after step 3 of 5 would resume as
                a finished run.

        Returns:
            The outcome, including the audit record and the checkpoints.

        Raises:
            BudgetExceeded: If step 1 or 2 refuses. The step never ran.
            GateRefused: If step 3 refuses or defers. The step never ran.
        """
        dimension = KIND_DIMENSION.get(step.kind)
        if dimension:
            self._ledger.reserve(dimension, 1, step=step.name)
        self._ledger.reserve("steps", 1, step=step.name)
        self._ledger.check("wall_clock_ms", step=step.name)

        context = StepContext(
            step=step.name,
            index=index,
            artifacts=dict(self._state.artifacts),
            state=self._state,
            principal=self._principal,
            role=self._role,
            intent=step.intent,
        )

        before: Optional[CheckpointRef] = None
        decision: Optional[ApprovalDecision] = None
        request: Optional[ApprovalRequest] = None

        if step.risky:
            # The pitfall's second clause: the state is written *before* the
            # question is asked, so a crash inside wait_for_approval() resumes
            # knowing the step was reached.
            self._ledger.reserve("risky_steps", 1, step=step.name)
            before = self._save_state(source=SOURCE_LOOP)
            decision, request = self._consult_gate(step, index, context, before)
            if not decision.approved:
                raise GateRefused(
                    step.name,
                    gate=step.gate,
                    decided_by=decision.decided_by,
                    reason=decision.reason,
                    request=request,
                )

        started = time.perf_counter()
        try:
            outputs = dict(step.run(context))
        except Exception as exc:  # noqa: BLE001 - audited, then re-reported
            duration = (time.perf_counter() - started) * 1000.0
            record = self._audit_step(
                step=step,
                index=index,
                context=context,
                outputs={},
                outcome="failed",
                error=f"{type(exc).__name__}: {exc}",
                risky=step.risky,
                decision=decision,
                request=request,
                duration_ms=duration,
                checkpoint_before=before,
            )
            log_event(
                logger,
                "runtime.step_failed",
                run_id=self._run_id,
                step=step.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            after = self._halt_at(step.name, record)
            return StepOutcome(
                step=step.name,
                index=index,
                status="failed",
                outputs={},
                error=f"{type(exc).__name__}: {exc}",
                decision=decision,
                request=request,
                record=record,
                checkpoint_before=before,
                checkpoint_after=after,
                duration_ms=duration,
            )

        duration = (time.perf_counter() - started) * 1000.0
        self._charge_step(step, outputs, duration)

        if not outputs and not step.allow_empty_output:
            # A step that produced nothing has still been *called*, so it must
            # still be logged -- but as a failure, because "logged with its
            # inputs and outputs" is not satisfied by an empty output.
            record = self._audit_step(
                step=step,
                index=index,
                context=context,
                outputs={},
                outcome="failed",
                error=(
                    "step returned no outputs; an action with no recorded output "
                    "cannot satisfy the acceptance criterion that every action is "
                    "logged with its inputs and outputs"
                ),
                risky=step.risky,
                decision=decision,
                request=request,
                duration_ms=duration,
                checkpoint_before=before,
            )
            after = self._halt_at(step.name, record)
            return StepOutcome(
                step=step.name,
                index=index,
                status="failed",
                outputs={},
                error="step returned no outputs",
                decision=decision,
                request=request,
                record=record,
                checkpoint_before=before,
                checkpoint_after=after,
                duration_ms=duration,
            )

        record = self._audit_step(
            step=step,
            index=index,
            context=context,
            outputs=outputs,
            outcome="ok",
            error="",
            risky=step.risky,
            decision=decision,
            request=request,
            duration_ms=duration,
            checkpoint_before=before,
        )

        artifacts = dict(self._state.artifacts)
        artifacts[step.name] = outputs
        approvals = dict(self._state.approvals)
        if step.risky and decision is not None:
            approvals[step.name] = decision.outcome
        self._state = self._state.with_progress(
            cursor=next_step,
            completed=self._state.completed + (step.name,),
            step_index=self._state.step_index + 1,
            artifacts=artifacts,
            approvals=approvals,
            spent=self._ledger.spent(),
            audit_seq=record.seq + 1,
            audit_digest=record.digest,
        )
        after = self._save_state(source=SOURCE_LOOP)
        log_event(
            logger,
            "runtime.step_ok",
            run_id=self._run_id,
            step=step.name,
            kind=step.kind,
            risky=step.risky,
            duration_ms=round(duration, 3),
            audit_seq=record.seq,
        )
        return StepOutcome(
            step=step.name,
            index=index,
            status="ok",
            outputs=outputs,
            decision=decision,
            request=request,
            record=record,
            checkpoint_before=before,
            checkpoint_after=after,
            duration_ms=duration,
        )

    def _assert_same_log(self, restored: RunState) -> None:
        """Refuse to resume against an audit log that is not this run's log.

        The checkpoint says where the hash chain stopped. The audit log says where
        *its* chain stopped. If those two digests differ, the log handed to this
        runtime is not the log the crashed process was writing to -- a wrong
        path, a fresh file, or two runs sharing one.

        Left unchecked, the first append of the resumed run fails inside
        ``AuditLog.append`` with *"prev_digest does not match the log"*, which
        reads like tampering. It is not tampering; it is a wiring mistake, and the
        operator needs to be told that, because the fix is to point the runtime at
        the right file rather than to investigate an audit log that was never
        attacked.

        The empty-log case is checked too: resuming a checkpoint whose first
        record is ``GENESIS_DIGEST`` against a log that already holds records is
        the same mistake wearing a different hat.

        Raises:
            ResumeDivergence: If the log's last digest is not the checkpoint's.
        """
        on_disk = self._audit.last_digest()
        if on_disk == restored.audit_digest:
            return
        raise ResumeDivergence(
            self._thread_id,
            first_difference="audit_digest",
            expected=restored.audit_digest or GENESIS_DIGEST,
            actual=on_disk,
            reason="the audit log the checkpoint was written against",
        )

    # ----------------------------------------------------------------- gates

    def _consult_gate(
        self,
        step: Step,
        index: int,
        context: StepContext,
        checkpoint_before: Optional[CheckpointRef],
    ) -> tuple[ApprovalDecision, ApprovalRequest]:
        """Ask the gate named by ``step``, and audit the answer.

        A refusal is audited *and checkpointed* before the caller raises. That
        ordering is deliberate: doc 2.12 requires escalation to course faculty on
        a governed refusal, and an escalation needs a record saying what was
        proposed and who said no. A refusal that only existed as an exception
        would force the resumer to re-ask to find out.

        The refused step is deliberately **not** added to ``completed``, so a
        resumed run re-asks the gate. The human may answer differently second
        time; the runtime is not entitled to treat one "no" as "no forever". The
        previous answer is still in the audit log and in ``approvals``.

        Returns:
            The decision, and the request as presented.

        Raises:
            KeyError: If the step names a gate that is not registered. Raised
                rather than auto-approved, because a step that believes it is
                gated but names a gate nobody registered is the exact failure
                the gate exists to prevent.
        """
        gate = self._gates[step.gate]
        request = ApprovalRequest(
            gate=step.gate,
            step=step.name,
            intent=step.intent,
            action=step.action,
            summary=step.summary,
            artifacts={k: _summarise(v) for k, v in self._state.artifacts.items()},
            principal=self._principal,
            role=self._role,
            run_id=self._run_id,
            thread_id=self._thread_id,
            step_index=index,
        )
        decision = gate.request(request)

        if not decision.approved:
            # Audit the refusal before raising, so a run that stops at a gate
            # still has the record of having asked.
            status = "refused" if decision.refused else "deferred"
            self._audit_step(
                step=step,
                index=index,
                context=context,
                outputs={},
                outcome=status,
                error=decision.reason or decision.rendered(),
                risky=True,
                decision=decision,
                request=request,
                duration_ms=0.0,
                checkpoint_before=checkpoint_before,
            )
            self._state = self._state.with_progress(
                cursor=step.name,
                completed=self._state.completed,
                step_index=self._state.step_index,
                approvals={**self._state.approvals, step.name: decision.outcome},
                spent=self._ledger.spent(),
            )
            self._save_state(source=SOURCE_LOOP)

        self._gate_answers.append(f"{step.gate}: {decision.rendered()}")
        return decision, request

    # ---------------------------------------------------------------- budget

    def _charge_step(
        self,
        step: Step,
        outputs: Mapping[str, Any],
        duration_ms: float,
    ) -> None:
        """Charge the costs that are only knowable after the step has finished.

        The one call implied by ``step.kind``, plus ``steps`` (and ``risky_steps``
        for a gated step), were already reserved *and charged* by
        :meth:`execute_step` before the step ran -- ``BudgetLedger.reserve``
        commits as it checks, so there is nothing here to add for them and
        re-charging would double-count.

        What is left are the measured extras:

        * ``step.cost``, the step's declared volume (e.g. ``records_read: 8``).
        * ``records_read``, read from the step's own output because the runtime
          cannot know how many records a tool returned without looking.
          :data:`RECORD_COUNT_KEYS` names the keys looked for, rather than
          sniffing every value, so a step cannot silently introduce a new one
          that escapes the volume budget.
        * ``wall_clock_ms``, the measured duration.

        The wall clock is ``ceil``-ed, so a step that took any measurable time
        costs at least one millisecond. Rounding down would leave a dimension
        reading ``0`` across a run of twenty fast steps -- a budget that looks
        present and bounds nothing.
        """
        for name, amount in step.cost.items():
            self._ledger.charge(name, int(amount))
        for key in RECORD_COUNT_KEYS:
            if key in outputs:
                try:
                    self._ledger.charge("records_read", int(outputs[key]))
                except (TypeError, ValueError):
                    pass
                break
        self._ledger.charge("wall_clock_ms", math.ceil(duration_ms))

    # ----------------------------------------------------------------- audit

    def _audit_step(
        self,
        *,
        step: Step,
        index: int,
        context: StepContext,
        outputs: Mapping[str, Any],
        outcome: str,
        error: str,
        risky: bool,
        decision: Optional[ApprovalDecision],
        request: Optional[ApprovalRequest],
        duration_ms: float,
        checkpoint_before: Optional[CheckpointRef],
    ) -> AuditRecord:
        """Append one record for ``step`` and move the state's chain position.

        The state's ``audit_seq`` and ``audit_digest`` are advanced by the record
        that was just appended, which is what makes the *next* checkpoint carry
        the right chain position for a resuming process.
        """
        record = build_audit_record(
            seq=self._state.audit_seq,
            run_id=self._run_id,
            thread_id=self._thread_id,
            step=step.name,
            intent=step.intent,
            action=step.action,
            kind=step.kind,
            inputs=_inputs_for(step, context, request),
            outputs=outputs,
            outcome=outcome,
            previous_digest=self._state.audit_digest,
            error=error,
            risky=risky,
            gate=step.gate if risky else "",
            decided_by=decision.decided_by if decision else "",
            reason=decision.reason if decision else "",
            principal=self._principal,
            role=self._role,
            token_fingerprint=self._token_fingerprint,
            budget_spent=self._ledger.spent(),
            checkpoint_id=checkpoint_before.checkpoint_id if checkpoint_before else "",
            duration_ms=round(duration_ms, 3),
        )
        self._audit.append(record)
        self._state = self._state.with_audit_position(
            seq=record.seq + 1, digest=record.digest
        )
        return record

    # ------------------------------------------------------------ checkpoints

    def _halt_at(self, step_name: str, record: AuditRecord) -> CheckpointRef:
        """Stop at ``step_name`` after ``record``, and make the stop durable.

        Called on every path where a step was audited but did not succeed: an
        exception, and a step that produced no outputs. Both leave an audit
        record on the chain, and **the checkpoint has to record that too** --
        ``_audit_step`` has already moved the state's ``audit_seq`` and
        ``audit_digest`` forward, and if that position is not persisted, a
        resuming process would try to append at a sequence number the log already
        holds, with a ``prev_digest`` that no longer matches. The append is then
        refused as tampering, by the runtime's own integrity check, on a log that
        nobody tampered with.

        The step is deliberately left out of ``completed`` so a resume retries it,
        and the cursor is pointed back at it so the retry is the next thing that
        happens.
        """
        self._state = self._state.with_progress(
            cursor=step_name,
            completed=self._state.completed,
            step_index=self._state.step_index,
            spent=self._ledger.spent(),
            audit_seq=record.seq + 1,
            audit_digest=record.digest,
        )
        return self._save_state(source=SOURCE_LOOP)

    def _save_state(self, *, source: str = SOURCE_LOOP) -> CheckpointRef:
        """Write the current state and count the write."""
        ref = self._checkpoints.save(self._state, source=source)
        self._checkpoints_written += 1
        return ref

    # ---------------------------------------------------------------- report

    def _report(
        self,
        *,
        status: str,
        escalated: bool,
        duration_ms: float,
        stopped_at: str = "",
        diagnosis: str = "",
    ) -> RunReport:
        """Assemble the report, verifying the audit chain before it claims success.

        The chain is verified *as part of producing the report*, not as a
        separate diagnostic. Lab 6's acceptance criterion is that every action is
        logged with its inputs and outputs; a run that produced that log but
        cannot prove it is intact has not met the criterion, and the report is
        where that gets said.
        """
        verified = self._audit.verify()
        return RunReport(
            run_id=self._run_id,
            thread_id=self._thread_id,
            status=status,
            outcomes=tuple(self._outcomes),
            skipped=tuple(
                name
                for name in self._state.completed
                if name not in {o.step for o in self._outcomes}
            ),
            audit_fingerprint=self._audit.fingerprint(),
            audit_records=verified,
            budget=self._ledger.snapshots(),
            gate_answers=tuple(self._gate_answers),
            checkpoints=self._checkpoints_written,
            duration_ms=round(duration_ms, 3),
            escalated=escalated,
            stopped_at=stopped_at,
            diagnosis=diagnosis,
        )

    def report(self) -> RunReport:
        """The report for whatever this process has done so far."""
        return self._report(status="partial", escalated=False, duration_ms=0.0)


#: Output keys the runtime reads a record count from. Listed rather than
#: sniffed, because a step that invents a new key for its row count would
#: otherwise silently escape the ``records_read`` budget.
RECORD_COUNT_KEYS: tuple[str, ...] = ("records", "record_count", "rows", "count")


def _inputs_for(
    step: Step, context: StepContext, request: Optional[ApprovalRequest]
) -> dict[str, Any]:
    """What to record as this step's ``inputs``.

    The step declares its own arguments via :attr:`Step.cost`-adjacent metadata
    only implicitly, so the runtime cannot know them. What it *can* know is what
    it consulted: the prior artifacts the step was handed. Those are recorded as
    inputs, plus the approval request when there was one -- because for a gated
    step, the thing that was proposed *is* an input to the audit.

    A step that wants its real arguments in the record passes them through its
    output, or Lab 7 supplies them from the graph's state; recording the
    consultable context is what this lab can honestly claim.
    """
    payload: dict[str, Any] = {"prior_artifacts": sorted(context.artifacts)}
    if request is not None:
        payload["approval_request"] = {
            "gate": request.gate,
            "summary": request.summary,
            "step_index": request.step_index,
        }
    return payload


def _summarise(value: Any, *, depth: int = 0) -> Any:
    """A small, JSON-safe view of an artifact for the approval request.

    The human at a gate needs to see *what* they are authorising, not a
    megabyte of note bodies, so collections are counted and mappings are keyed.
    Depth-bounded so a cyclic structure cannot spin here.
    """
    if depth > 2:
        return "<...>"
    if isinstance(value, Mapping):
        return {str(k): _summarise(v, depth=depth + 1) for k, v in list(value.items())[:8]}
    if isinstance(value, (list, tuple, set)):
        return f"<{len(value)} item(s)>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def plan_digest(plan: Iterable[Step]) -> str:
    """A digest of a plan's *shape*, for the checkpoint's ``plan_sha256``.

    Over the names, intents, kinds, risk and cost -- not the callables, which
    have no stable identity across processes. A resumed run in a different
    process rebuilds its steps from the same source, so the shape matches even
    though the function objects do not. If the plan's shape changed, the resume
    is refused by :meth:`Runtime._assert_same_work` rather than applied to
    different work.
    """
    import hashlib

    import json

    shape = [
        {
            "name": s.name,
            "intent": s.intent,
            "action": s.action,
            "kind": s.kind,
            "risky": s.risky,
            "gate": s.gate,
            "cost": dict(sorted(s.cost.items())),
        }
        for s in plan
    ]
    blob = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]