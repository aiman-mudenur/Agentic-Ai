"""Budgets: countable resources, checked *before* the action that spends them.

Doc 2.10 Lab 6 lists *"enforcing budgets"* as what the runtime adds, and its
pseudocode opens with ``run = Runtime(budget=B, gates=[publish])``. Doc 2.12
names the risk this answers:

    | Runaway loops | the regenerate loop never converges | Bound MAX_ATTEMPTS
    (Lab 7/10); on exhaustion, escalate to course faculty with a diagnosis. |

and the Glossary defines the Coordinator as the agent that *"owns the run-state,
plan, budgets and the audit log."*

Two design decisions carry the weight.

**Check before, not after.** :meth:`BudgetLedger.reserve` is what the runtime
calls, and it raises :class:`~course_organizer.runtime.errors.BudgetExceeded`
*before* the step executes. A budget consulted after the work is a report. This
is the same ordering as the connector's scope check in Lab 5 -- the check that
must not be paid for with the thing it is checking -- and it is deliberately the
same shape so there is one idea to hold rather than two.

**Dimensions, not one number.** The document writes ``budget=B``, singular, and
never says what B counts. A single integer would have to be either tool calls,
LLM calls, or wall-clock seconds, and each choice silently fails to bound one of
the other two. So :class:`BudgetDimension` names the countable things this
runtime can actually spend, and :class:`Budget` holds one ceiling per dimension.
Which ceilings to default to is this project's choice and is labelled as such
wherever it appears; the *mechanism* is what the document asks for.

The spend is recorded on the audit log at the moment it happens, so a run that
ends at 39 of 40 calls leaves evidence of what the 39 were.
"""

from __future__ import annotations

import logging
from typing import Iterable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from course_organizer.logging_setup import log_event
from course_organizer.runtime.errors import BudgetExceeded

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LIMITS",
    "Budget",
    "BudgetDimension",
    "BudgetDimensionName",
    "BudgetLedger",
    "BudgetSnapshot",
]

#: The countable resources this runtime knows how to charge.
#:
#: ``steps`` and ``risky_steps`` bound *loop shape* (doc 2.12's MAX_ATTEMPTS).
#: ``tool_calls``/``skill_calls``/``connector_calls``/``llm_calls`` bound the four
#: kinds of work the runtime dispatches. ``records_read`` bounds volume, because a
#: single call that returns the whole cohort is a different risk from 40 calls
#: returning one record each. ``wall_clock_ms`` bounds the run's duration.
BudgetDimension = Literal[
    "steps",
    "risky_steps",
    "tool_calls",
    "skill_calls",
    "connector_calls",
    "llm_calls",
    "records_read",
    "wall_clock_ms",
]

BudgetDimensionName = str

#: This project's starting ceilings. The document says ``budget=B`` and does not
#: define B; these are the numbers Lab 6 uses, and every one of them is visible
#: in the run's output and overridable per-run. ``steps`` is deliberately the
#: tightest of the call dimensions: the bound that matters for a runaway loop is
#: the number of iterations, and a generous call ceiling would let a loop spin
#: inside the step ceiling without ever tripping a call ceiling.
DEFAULT_LIMITS: Mapping[str, int] = {
    "steps": 24,
    "risky_steps": 4,
    "tool_calls": 12,
    "skill_calls": 8,
    "connector_calls": 12,
    "llm_calls": 8,
    "records_read": 200,
    "wall_clock_ms": 120_000,
}

#: Dimensions that count discrete dispatches rather than volume or duration.
#: Reported as a count of calls in the run output so the two families are never
#: summed into one meaningless total.
COUNT_DIMENSIONS: frozenset[str] = frozenset(
    {
        "steps",
        "risky_steps",
        "tool_calls",
        "skill_calls",
        "connector_calls",
        "llm_calls",
    }
)


class Budget(BaseModel):
    """One ceiling per countable resource.

    A dimension may be omitted, and an omitted dimension is **unbounded**. That
    is the opposite of a default, deliberately: a budget that silently imposes a
    ceiling the caller never asked for will refuse a step the caller believed was
    allowed, and the report would then be explaining a limit that exists nowhere
    in the code the caller wrote. :meth:`DEFAULT_LIMITS` supplies the starting
    point explicitly, via :meth:`defaults`.

    Attributes:
        limits: Dimension name to ceiling. Absent means unbounded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    limits: Mapping[str, int] = Field(default_factory=dict)

    @field_validator("limits", mode="before")
    @classmethod
    def _limits_are_positive_ints(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        """Reject a non-int, a zero or a negative ceiling.

        Runs in ``"before"`` mode deliberately. Pydantic coerces ``True`` to
        ``1`` while validating the ``int`` annotation, so an ``"after"`` validator
        -- the default -- sees ``1`` and never learns a bool was written. A budget
        of ``steps=True`` is a bug that would otherwise read as a budget of one.

        A zero ceiling is the other interesting case: it reads like a real limit
        but refuses every step of that dimension immediately, including the
        first, which looks like a misconfiguration rather than the hard stop the
        author probably meant. Requiring ``>= 1`` turns that into a
        construction-time error naming the dimension.
        """
        if not isinstance(value, Mapping):
            raise ValueError(f"limits must be a mapping, got {type(value).__name__}")
        for name, limit in value.items():
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError(
                    f"budget {name!r} limit must be an int, got {limit!r} of type "
                    f"{type(limit).__name__}"
                )
            if limit < 1:
                raise ValueError(
                    f"budget {name!r} limit must be >= 1, got {limit}. Use an "
                    f"omitted dimension for 'unbounded'."
                )
        return dict(value)

    @classmethod
    def defaults(cls) -> Budget:
        """This project's starting ceilings, from :data:`DEFAULT_LIMITS`."""
        return cls(limits=dict(DEFAULT_LIMITS))

    @classmethod
    def only(cls, **limits: int) -> Budget:
        """A budget naming only the dimensions given; the rest unbounded."""
        return cls(limits=limits)

    def ceiling(self, dimension: str) -> Optional[int]:
        """The ceiling for ``dimension``, or ``None`` if it is unbounded."""
        return self.limits.get(dimension)

    def bounded(self) -> tuple[str, ...]:
        """The bounded dimension names, in the order given."""
        return tuple(self.limits)

    def rendered(self) -> str:
        """One line per dimension, so a run report can show the limits it ran under."""
        if not self.limits:
            return "unbounded (no limits set)"
        return ", ".join(f"{k}={v}" for k, v in self.limits.items())


class BudgetSnapshot(BaseModel):
    """Spend and ceiling at one instant, for one dimension.

    Attributes:
        dimension: The dimension this snapshot describes.
        spent: Units consumed so far.
        limit: The ceiling, or ``None`` when unbounded.
        refused: How many ``reserve`` calls this dimension turned away.
    """

    model_config = ConfigDict(frozen=True)

    dimension: str
    spent: int = 0
    limit: Optional[int] = None
    refused: int = 0

    @property
    def remaining(self) -> Optional[int]:
        """Units left before the ceiling, or ``None`` when unbounded."""
        return None if self.limit is None else max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        """Is this dimension at or past its ceiling?"""
        return self.limit is not None and self.spent >= self.limit

    def rendered(self) -> str:
        """A compact ``spent/limit`` cell for the run's budget table."""
        lim = "inf" if self.limit is None else str(self.limit)
        return f"{self.spent}/{lim}"


class BudgetLedger:
    """Tracks spend, and refuses before rather than after.

    Not a Pydantic model: this is mutable bookkeeping that belongs to exactly
    one run, and freezing it would mean copying it at every step. The *state* a
    checkpoint needs is captured separately by :meth:`spent`, which returns a
    plain immutable dict.

    Args:
        budget: The ceilings to enforce.
        run_id: Stamped on every log event this ledger emits.
    """

    def __init__(self, budget: Budget, *, run_id: str = "") -> None:
        self._budget = budget
        self._run_id = run_id
        self._spent: dict[str, int] = {}
        self._refused: dict[str, int] = {}

    # ---------------------------------------------------------------- queries

    @property
    def budget(self) -> Budget:
        """The ceilings in force."""
        return self._budget

    @property
    def run_id(self) -> str:
        """The run id stamped on this ledger's log events."""
        return self._run_id

    def spent(self) -> dict[str, int]:
        """A copy of the spend, for checkpointing.

        A copy rather than the live dict, because a checkpoint that aliases the
        ledger's internal dict would be mutated by the next step -- and a
        checkpoint that changes after it was written cannot be trusted on
        resume.
        """
        return dict(self._spent)

    def snapshot(self, dimension: str) -> BudgetSnapshot:
        """Spend, ceiling and refusal count for one dimension."""
        return BudgetSnapshot(
            dimension=dimension,
            spent=self._spent.get(dimension, 0),
            limit=self._budget.ceiling(dimension),
            refused=self._refused.get(dimension, 0),
        )

    def snapshots(self) -> tuple[BudgetSnapshot, ...]:
        """Every dimension that has been spent or bounded, spend first.

        Bounded-but-unspent dimensions are included deliberately: a budget the
        run never came close to is information, and a table that only lists
        columns the run happened to fill in would hide it.
        """
        names: list[str] = [d for d in self._spent if self._spent[d]]
        names += [d for d in self._budget.limits if d not in names]
        return tuple(self.snapshot(d) for d in names)

    def remaining(self, dimension: str) -> Optional[int]:
        """Units left for ``dimension``, or ``None`` when unbounded."""
        return self.snapshot(dimension).remaining

    def refused(self, dimension: str | None = None) -> int:
        """How many reservations were refused, in total or for one dimension."""
        if dimension is not None:
            return self._refused.get(dimension, 0)
        return sum(self._refused.values())

    # ------------------------------------------------------------ enforcement

    def reserve(self, dimension: str, amount: int = 1, *, step: str | None = None) -> None:
        """Charge ``amount`` of ``dimension`` if it fits under the ceiling.

        This is the method the runtime calls *before* the action, and it both
        checks and consumes. A step is atomic from the budget's point of view --
        either it ran and gets charged, or it never started -- so there is no
        window in which a reservation could be made and then abandoned; keeping
        ``reserve`` and ``charge`` as separate committing operations would let a
        caller reserve, then charge again and double-count. (This corrects an
        earlier version of this module in which ``reserve`` only checked, which
        made the "checked before the action" property true but the accounting
        invisible unless the caller remembered a second ``charge``.)

        Args:
            dimension: Which resource is about to be spent.
            amount: How much of it.
            step: The step name, for the error and the log line.

        Raises:
            BudgetExceeded: If the charge would pass a ceiling. Nothing is
                recorded as spent, because nothing was spent.
        """
        if amount < 0:
            raise ValueError(f"cannot reserve a negative amount ({amount}) of {dimension!r}")

        limit = self._budget.ceiling(dimension)
        if limit is not None:
            current = self._spent.get(dimension, 0)
            if current + amount > limit:
                self._refused[dimension] = self._refused.get(dimension, 0) + 1
                log_event(
                    logger,
                    "runtime.budget_refused",
                    run_id=self._run_id,
                    dimension=dimension,
                    spent=current,
                    limit=limit,
                    requested=amount,
                    step=step or "",
                )
                raise BudgetExceeded(
                    dimension, limit=limit, spent=current, requested=amount, step=step
                )

        self._spent[dimension] = self._spent.get(dimension, 0) + amount

    def check(self, dimension: str, *, step: str | None = None) -> None:
        """Raise if ``dimension`` is already at or past its ceiling.

        The one dimension that cannot be reserved in advance is
        ``wall_clock_ms``: the runtime cannot know how long a step will take, and
        refusing on the strength of a guess would be a different policy. So the
        runtime calls this *before* a step to catch a run that has already
        overrun, then charges the measured duration afterwards. A budget that only
        reports an overrun once the step has finished has already spent it, which
        is the failure :meth:`reserve` exists to avoid; this is the honest version
        for the one quantity that is unknowable in advance.

        Raises:
            BudgetExceeded: If the dimension is already exhausted. ``requested``
                is 0, because nothing was asked for -- the overrun happened
                earlier.
        """
        limit = self._budget.ceiling(dimension)
        if limit is None:
            return
        current = self._spent.get(dimension, 0)
        if current >= limit:
            self._refused[dimension] = self._refused.get(dimension, 0) + 1
            log_event(
                logger,
                "runtime.budget_exhausted",
                run_id=self._run_id,
                dimension=dimension,
                spent=current,
                limit=limit,
                step=step or "",
            )
            raise BudgetExceeded(
                dimension, limit=limit, spent=current, requested=0, step=step
            )

    def charge(self, dimension: str, amount: int = 1) -> None:
        """Record spend that has already happened, without a ceiling check.

        For costs that are only knowable *after* the work -- the records a tool
        returned, the wall clock a step consumed. The pre-action ceiling for the
        step's own kind was already enforced by :meth:`reserve`; this adds the
        measured extras on top and deliberately does not re-check, because a
        caller that could reach here twice for the same unit would double-count
        it. Callers that can fail between the two should use :meth:`reserve`.
        """
        if amount < 0:
            raise ValueError(f"cannot charge a negative amount ({amount}) of {dimension!r}")
        self._spent[dimension] = self._spent.get(dimension, 0) + amount

    def spend(self, dimension: str, amount: int = 1, *, step: str | None = None) -> None:
        """Reserve one unit of ``dimension``. An alias kept for readability.

        Since :meth:`reserve` now checks *and* charges, ``spend`` and ``reserve``
        are the same call. The alias exists because the runtime reads better as
        "spend this before the step" while the ledger's contract is "reserve
        against the ceiling"; there is no second code path.
        """
        self.reserve(dimension, amount, step=step)

    def restore(self, spent: Mapping[str, int]) -> None:
        """Adopt a checkpointed spend map.

        Only for resume. Refusal counts are deliberately **not** restored: they
        describe this process's attempts, and a resumed run that inherited the
        crashed process's refusals would report having been refused something it
        never asked for.
        """
        for name, amount in spent.items():
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError(f"cannot restore non-negative int spend for {name!r}: {amount!r}")
        self._spent = dict(spent)
        self._refused = {}

    def remaining_capacity(self, dimensions: Iterable[str]) -> dict[str, Optional[int]]:
        """Remaining units for the named dimensions, for a run report."""
        return {d: self.remaining(d) for d in dimensions}
