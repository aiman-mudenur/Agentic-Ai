"""Approval gates: the runtime's hard stop before a consequential action.

Doc 2.10 Lab 6's pseudocode is where the gate first appears:

    run = Runtime(budget=B, gates=[publish])
    for step in agent.loop():
        audit.log(step.intent, step.action, step.result)
        if step.risky: wait_for_approval(step)
        checkpoint(run.state)

Two details of that line carry the design. It is ``if step.risky`` -- a
*property of the step*, decided before the step runs, not a list of endpoints the
connector happens to guard. And it is ``wait_for_approval``, which in the
document's own vocabulary means the run **stops**: the Glossary defines a human
gate as *"The approval step; the named human is the final authority"*, and doc
2.12 is unconditional -- *"No worklet takes a consequential action
autonomously - the human authority named in this chapter signs off before
anything is published."*

So this module offers one question and three possible answers, and every gate
here can produce all three:

* ``approved`` -- the step runs.
* ``refused`` -- the step does not run, and the run stops.
* ``deferred`` -- the step does not run, and the run stops, but the reason is
  "nobody answered" rather than "somebody said no". Worth distinguishing: doc
  2.12's remedy for a refusal is escalation to course faculty, whereas a deferral
  is an unanswered question, and an audit that conflates them reports a
  malfunction as a decision.

**Scope note.** Lab 6 asks the gate as an in-process call, because Lab 6's
deliverable is *"budgets, gates, checkpoint/resume, audit log"* and the gate is
one of the four. Lab 7 replaces the question with LangGraph's real
``interrupt()`` so a human can answer from outside the graph. The decision types
here are what both labs share, so nothing downstream has to be rewritten when
the transport changes -- only the gate that answers.

Every gate implementation in this module is a *test or demonstration* gate, and
each says so in its name. None of them is a human. :class:`CallbackGate` is the
one a real caller uses, and its docstring is explicit that the callback is
responsible for doing the asking.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.logging_setup import log_event
from course_organizer.runtime.errors import RuntimeBoundaryError

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_OUTCOMES",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "CallbackGate",
    "DenyAllGate",
    "GateScriptExhausted",
    "PreapprovedGate",
    "RecordingGate",
    "ScriptedGate",
    "UNANSWERED",
]

#: The three answers a gate may give. ``deferred`` is separate from ``refused``
#: because "no" and "nobody answered" call for different follow-up, and an audit
#: that merged them would report a decision that was never made.
APPROVAL_OUTCOMES: tuple[str, ...] = ("approved", "refused", "deferred")

#: The value :attr:`ApprovalDecision.decided_by` carries when nobody replied.
#: A literal string rather than ``None`` so it survives a JSONL round-trip
#: unchanged -- ``None`` would come back as ``null`` and every comparison against
#: it would need a None-check.
UNANSWERED = "unanswered"


class GateScriptExhausted(RuntimeBoundaryError):
    """A scripted gate ran out of answers while the run was still going.

    A test double failing loudly is the point. If the gate quietly returned an
    approval because its script had run out, every later assertion would be
    testing a run the runtime never actually performed.

    Attributes:
        gate: The gate that had no answer left.
        asked: How many requests it received.
    """

    def __init__(self, gate: str, *, asked: int) -> None:
        self.gate = gate
        self.asked = asked

        super().__init__(
            f"gate {gate!r} was asked {asked} time(s) but its script had no "
            f"answer left. This is a test-setup error, not a runtime refusal: "
            f"either the run performed more gated steps than expected, or the "
            f"gate is being asked twice for the same step."
        )


class ApprovalRequest(BaseModel):
    """What the runtime puts in front of the human.

    A frozen model, because this object is placed on the audit log and a decision
    recorded against a request that later mutated would be unverifiable.

    ``summary`` is the sentence a human reads, and it is built by the runtime
    rather than by the step. A step that described its own risk would be grading
    its own homework.

    Attributes:
        gate: Which gate is being consulted, e.g. ``"publish"``.
        step: The step waiting for a decision.
        intent: Why the step is being taken, from the plan.
        action: What the step would do.
        summary: The one-line human-readable description of the consequence.
        artifacts: What the step would act on, for the human to inspect.
        principal: Who is asking.
        role: That principal's role.
        run_id: Which run.
        thread_id: Which resume thread, so a decision can be tied to the state.
        step_index: The step's position, so "the fourth step" is unambiguous.
        requested_at: When the gate was consulted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    step: str
    intent: str = ""
    action: str = ""
    summary: str = ""
    artifacts: Mapping[str, Any] = Field(default_factory=dict)
    principal: str = ""
    role: str = ""
    run_id: str = ""
    thread_id: str = ""
    step_index: int = 0
    requested_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def rendered(self) -> str:
        """One line a human can read before deciding."""
        head = f"[{self.gate}] {self.summary or self.action or self.step}"
        if self.artifacts:
            listed = ", ".join(sorted(self.artifacts)[:4])
            head = f"{head} ({listed})"
        return head


class ApprovalDecision(BaseModel):
    """The human's answer, and who gave it.

    ``decided_by`` defaults to :data:`UNANSWERED` rather than being required, so
    that constructing a decision cannot itself be the place the attribution is
    forgotten -- and :meth:`ApprovalDecision.deferred` fills it in consistently.

    Attributes:
        outcome: One of :data:`APPROVAL_OUTCOMES`.
        decided_by: Who answered. :data:`UNANSWERED` if nobody did.
        reason: The human's stated reason, verbatim. Never paraphrased by the
            runtime, because the reason is often the thing that matters most.
        edits: Edits the human asked for, applied by a later lab -- Lab 12 makes
            overrides effortless and logged. Recorded here so Lab 6's audit can
            show that an approval carried conditions.
        at: When the decision was made.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str
    decided_by: str = UNANSWERED
    reason: str = ""
    edits: Mapping[str, Any] = Field(default_factory=dict)
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def approve(
        cls, *, by: str, reason: str = "", edits: Optional[Mapping[str, Any]] = None
    ) -> ApprovalDecision:
        """An explicit yes, attributed to ``by``."""
        return cls(outcome="approved", decided_by=by, reason=reason, edits=dict(edits or {}))

    @classmethod
    def refuse(cls, *, by: str = UNANSWERED, reason: str = "") -> ApprovalDecision:
        """An explicit no, attributed to ``by``."""
        return cls(outcome="refused", decided_by=by, reason=reason)

    @classmethod
    def deferred(cls, *, reason: str = "") -> ApprovalDecision:
        """Nobody answered. Not the same as a refusal."""
        return cls(outcome="deferred", decided_by=UNANSWERED, reason=reason)

    @property
    def approved(self) -> bool:
        """May the step run?"""
        return self.outcome == "approved"

    @property
    def refused(self) -> bool:
        """Did a human say no?"""
        return self.outcome == "refused"

    @property
    def unanswered(self) -> bool:
        """Was this a deferral rather than a decision?"""
        return self.outcome == "deferred"

    def rendered(self) -> str:
        """One line: the outcome, who, and why."""
        who = "" if self.decided_by == UNANSWERED else f" by {self.decided_by}"
        why = f" -- {self.reason}" if self.reason else ""
        return f"{self.outcome}{who}{why}"


class ApprovalGate(ABC):
    """Something that can answer an :class:`ApprovalRequest`.

    ``name`` is what appears in ``Runtime(budget=B, gates=[publish])`` and in
    every audit record, so it is an attribute rather than something inferred
    from the class.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """This gate's name, as the document refers to it."""

    @abstractmethod
    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Decide ``req``. Must not raise to signal a refusal -- return one."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


class PreapprovedGate(ApprovalGate):
    """Answers "yes" to everything, on behalf of a named human.

    For the runs where the interesting behaviour is *downstream* of the gate. Not
    a stub: it is the correct gate for a lab whose subject is something else, and
    the decision it returns is attributed rather than anonymous, so the audit log
    of such a run still says who signed off.

    Args:
        name: The gate's name.
        by: Who is approving.
        reason: Recorded verbatim on every decision.
        edits: Conditions attached to every approval.
    """

    def __init__(
        self,
        name: str,
        *,
        by: str = "course_faculty",
        reason: str = "",
        edits: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._name = name
        self._by = by
        self._reason = reason
        self._edits = dict(edits or {})
        self.asked: list[ApprovalRequest] = []

    @property
    def name(self) -> str:
        """The gate's name."""
        return self._name

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Approve, having recorded the request."""
        self.asked.append(req)
        log_event(
            logger,
            "runtime.gate_approved",
            gate=self._name,
            step=req.step,
            decided_by=self._by,
        )
        return ApprovalDecision.approve(
            by=self._by, reason=self._reason, edits=self._edits
        )


class DenyAllGate(ApprovalGate):
    """Answers "no" to everything.

    The gate you want when demonstrating that a step marked risky **cannot** run
    without approval. Without it, the demonstration would be "the step was
    approved and then it ran", which shows nothing.

    Args:
        name: The gate's name.
        by: Who is refusing, for the audit record.
        reason: Recorded verbatim on every refusal.
    """

    def __init__(
        self, name: str, *, by: str = "course_faculty", reason: str = "not authorised"
    ) -> None:
        self._name = name
        self._by = by
        self._reason = reason
        self.asked: list[ApprovalRequest] = []

    @property
    def name(self) -> str:
        """The gate's name."""
        return self._name

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Refuse, having recorded the request."""
        self.asked.append(req)
        log_event(
            logger,
            "runtime.gate_refused",
            gate=self._name,
            step=req.step,
            decided_by=self._by,
            reason=self._reason,
        )
        return ApprovalDecision.refuse(by=self._by, reason=self._reason)


class ScriptedGate(ApprovalGate):
    """Answers from a fixed list, one per request, in order.

    The workhorse of the tests: a run can be driven through approve, refuse and
    defer in a single pass, and the same script can be replayed to show a resumed
    run behaves identically.

    Exhaustion raises :class:`GateScriptExhausted` rather than returning a
    default. A double-asked gate is a real bug in the runtime's ordering, and a
    silent default would hide it behind a passing test.

    Args:
        name: The gate's name.
        decisions: The answers, in the order they will be asked for.
    """

    def __init__(self, name: str, decisions: Sequence[ApprovalDecision]) -> None:
        self._name = name
        self._decisions = list(decisions)
        self.asked: list[ApprovalRequest] = []

    @property
    def name(self) -> str:
        """The gate's name."""
        return self._name

    @property
    def remaining(self) -> int:
        """How many scripted answers are still unused.

        Counted from the remaining script alone. Answers are *popped* on use, so
        subtracting the number of requests as well would count each consumed
        answer twice and report a negative number of answers left.
        """
        return len(self._decisions)

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Pop and return the next scripted answer."""
        self.asked.append(req)
        if not self._decisions:
            raise GateScriptExhausted(self._name, asked=len(self.asked))
        decision = self._decisions.pop(0)
        log_event(
            logger,
            f"runtime.gate_{decision.outcome}",
            gate=self._name,
            step=req.step,
            decided_by=decision.decided_by,
            reason=decision.reason,
        )
        return decision


class RecordingGate(ApprovalGate):
    """Wraps another gate and remembers every request it saw.

    Exists to answer a question the audit log cannot: *what did the runtime
    actually put in front of the human?* The audit records the decision; this
    records the question. If a step was gated but never asked -- because it was
    skipped, or because the gate was consulted after the action -- the two
    disagree, and that disagreement is the finding.

    Args:
        inner: The gate to delegate to.
    """

    def __init__(self, inner: ApprovalGate) -> None:
        self._inner = inner
        self.requests: list[ApprovalRequest] = []
        self.decisions: list[ApprovalDecision] = []

    @property
    def name(self) -> str:
        """The wrapped gate's name."""
        return self._inner.name

    @property
    def inner(self) -> ApprovalGate:
        """The gate being wrapped."""
        return self._inner

    def steps_asked(self) -> tuple[str, ...]:
        """Which steps the runtime brought to this gate, in order."""
        return tuple(r.step for r in self.requests)

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Record ``req``, then delegate."""
        self.requests.append(req)
        decision = self._inner.request(req)
        self.decisions.append(decision)
        return decision


class CallbackGate(ApprovalGate):
    """Delegates to a callable -- how a real caller plugs in its own asking.

    The callback receives the :class:`ApprovalRequest` and must return an
    :class:`ApprovalDecision`. Doing the actual asking is the callback's job: Lab
    6's runtime is synchronous and in-process, so whatever blocks for a human
    (a prompt, a web request, a queue read) happens inside the callback. Lab 7
    replaces this with ``interrupt()``; the decision types are unchanged, so the
    steps, the audit records and the tests above it do not move.

    Args:
        name: The gate's name.
        ask: Called with each request.
        on_error: What to do if the callable raises. ``"defer"`` -- the default --
            treats an exception as *nobody answered*, so a broken prompt cannot
            accidentally authorise a publish. ``"raise"`` propagates.
    """

    def __init__(
        self,
        name: str,
        ask: Callable[[ApprovalRequest], ApprovalDecision],
        *,
        on_error: str = "defer",
    ) -> None:
        if on_error not in {"defer", "raise"}:
            raise ValueError(f"on_error must be 'defer' or 'raise', got {on_error!r}")
        self._name = name
        self._ask = ask
        self._on_error = on_error
        self.asked: list[ApprovalRequest] = []

    @property
    def name(self) -> str:
        """The gate's name."""
        return self._name

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Call the delegate, handling a raising callable per ``on_error``."""
        self.asked.append(req)
        try:
            decision = self._ask(req)
        except Exception as exc:  # noqa: BLE001 - the on_error policy decides
            if self._on_error == "raise":
                raise
            log_event(
                logger,
                "runtime.gate_ask_failed",
                gate=self._name,
                step=req.step,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ApprovalDecision.deferred(
                reason=f"the gate could not be reached: {type(exc).__name__}"
            )
        if not isinstance(decision, ApprovalDecision):
            raise TypeError(
                f"gate {self._name!r} callback returned {type(decision).__name__}, "
                f"expected ApprovalDecision"
            )
        return decision


def gate_names(gates: Mapping[str, ApprovalGate] | Sequence[ApprovalGate]) -> tuple[str, ...]:
    """The names of ``gates``, in the order given."""
    if isinstance(gates, Mapping):
        return tuple(gates)
    return tuple(g.name for g in gates)