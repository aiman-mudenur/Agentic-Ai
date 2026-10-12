"""The skill layer: schema-typed, callable, reusable capability.

Document 2.10, Lab 3 requires:

    Goal:  "Package repeatable capability as reusable, schema-typed skills."
    What it adds: "A plan/blueprint skill and a formatting skill that renders
                  the organised course workspace (notes, assignments, grades)
                  -- both reusable across cases."
    Inputs:  "Parsed inputs; template spec."
    Outputs: "A locked plan object; a formatted ... shell."
    Agents / nodes: "Planning and assembly emerge as skills."
    Deliverable: "Two callable skills with schemas, invoked by the agent.
                  Accept: The same skill works for two different cases
                  without code changes."
    Pseudocode:

        skill plan(inputs) -> Plan:        return expand(inputs).lock()
        skill format_organised(result, template) -> Document

So a skill here is three things at once: a typed input model, a typed output
model, and a pure handler. That is the same shape as Lab 2's `ToolSpec`, and
that is deliberate -- a reviewer who learned the tool layer in Lab 2 already
knows how to read this one. The difference is what a skill is *for*:

===============  ==============================  ==============================
                 Tool (Lab 2)                    Skill (this lab)
===============  ==============================  ==============================
Answers          "what does the store say?"     "how do I build the artefact?"
Effect           read-only by construction       may transform a result
Consumers        the agent, per fact            any agent in the pipeline
Failure          a fact could not be retrieved  a capability was misused
Reuse axis       the data source is swap-able   the *case* is swap-able
===============  ==============================  ==============================

Nothing stops a skill from being written for exactly one course. That is
precisely the failure the document names: "Keep skills case-agnostic and typed;
hard-coding one case breaks reuse." It cannot be caught by reading the code,
so it is caught by a test -- see
``tests/test_lab03_skill_creation.py::test_no_course_identifier_is_hard_coded``,
which parses this package and fails if a course code or a course name appears
in it at all.

The registry keeps an append-only ledger, for the same reason Lab 2's does:
2.12 requires a full audit log, and it is far cheaper to start recording now
than to retrofit. A skill call is not a tool call -- a skill transforms, and
transformation is where drift is born -- so skills get their own record shape.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import UnionType
from typing import Any, Callable, Generic, Optional, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillError(Exception):
    """Raised when a skill cannot be invoked correctly.

    Covers three cases that are all *programming* errors rather than runtime
    conditions to be audited and reported:

    * an unknown skill name,
    * input that fails the skill's declared input model,
    * a handler that returns the wrong type, i.e. breaks its own contract.

    A handler that raises for a legitimate reason it handled well -- and
    returned a typed error for -- does not land here. That asymmetry is the
    same one `course_organizer.tools.base.ToolError` draws, and it is
    deliberate: "you called me wrong" is a different fact from "I could not
    do what you asked", and conflating them makes the audit log useless.
    """

    def __init__(self, message: str, *, code: str = "skill_error") -> None:
        """Record a machine-readable code alongside the message.

        Args:
            message: Human-readable explanation.
            code: Stable identifier for the failure class, so a caller can
                branch on it without matching on message text.
        """
        super().__init__(message)
        self.code = code


class SkillContractError(SkillError):
    """A handler returned something that is not its declared output model.

    Separate from `SkillError` because it means the *skill* is broken, not
    the *caller*. A caller bug should not be able to mask a skill bug.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="contract_violation")


# ---------------------------------------------------------------------------
# Skill contracts
# ---------------------------------------------------------------------------

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


def _annotation_text(annotation: Any) -> str:
    """Render a type annotation compactly, for the skill index.

    Args:
        annotation: A typing annotation, possibly a bare class.

    Returns:
        A short human label. The `types.UnionType`/`typing.Union` cases are
        unwrapped to their members, since Pydantic renders `X | None` for an
        optional field and the `None` reads as noise in an index line.
    """
    if annotation is type(None):
        return "None"
    if annotation is Ellipsis:
        return "..."
    if isinstance(annotation, type):
        return annotation.__name__

    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if origin in (Union, UnionType) and type(None) in args:
            remaining = [a for a in args if a is not type(None)]
            inner = ", ".join(_annotation_text(a) for a in remaining)
            return f"{inner} | None"
        name = getattr(origin, "__name__", str(origin))
        inner = ", ".join(_annotation_text(a) for a in args)
        return f"{name}[{inner}]" if inner else name

    return getattr(annotation, "__name__", str(annotation))


@dataclass(frozen=True)
class SkillCall:
    """One recorded skill invocation.

    Attributes:
        call_id: Short unique id, so a later artefact can cite the call that
            produced it -- the same discipline Lab 2 applied to `ToolCall`.
        skill: The skill's name.
        run_id: The owning run.
        input_payload: The validated input, as JSON-compatible data.
        output_type: Name of the declared output model. Recorded even on
            failure, because a mismatch is itself the finding.
        ok: Whether the handler produced a conforming result.
        error: Failure text, or None on success.
        elapsed_ms: Wall-clock duration, for the Lab 8 parallel/sequential
            comparison this package will later be part of.
        at: UTC timestamp.
        depth: Nesting depth, so a composite skill is visible as such.
    """

    call_id: str
    skill: str
    run_id: str
    input_payload: Mapping[str, Any]
    output_type: str
    ok: bool
    error: Optional[str]
    elapsed_ms: float
    at: datetime
    depth: int = 0


@dataclass(frozen=True)
class SkillResult(Generic[Out]):
    """The outcome of invoking one skill.

    Attributes:
        skill: The skill that ran.
        call: The ledger entry it produced.
        value: The typed output on success. On failure it is whatever
            `SkillSpec.failure_value` can honestly supply -- often None --
            and must not be read: `ok` is False and `unwrap()` raises. The
            field exists to keep the return type total, not to make a failed
            call look successful to anyone who forgets to check.
    """

    skill: str
    call: SkillCall
    value: Optional[Out]

    @property
    def ok(self) -> bool:
        """Whether the handler completed without raising."""
        return self.call.ok

    @property
    def call_id(self) -> str:
        """The ledger id, for citing this call in a document."""
        return self.call.call_id

    def unwrap(self) -> Out:
        """Return the value, raising if the call failed.

        Returns:
            The typed output.

        Raises:
            SkillError: If the handler failed. Carries the recorded message.
                This is the only way to read `value` from a failed result, and
                the only correct one.
        """
        if not self.call.ok:
            raise SkillError(
                f"skill {self.skill!r} failed: {self.call.error}",
                code="handler_failure",
            )
        assert self.value is not None  # narrowed by the ok check above
        return self.value


@dataclass(frozen=True)
class SkillContext:
    """What a handler is given besides its own typed input.

    Attributes:
        run_id: The owning run's id.
        depth: Current nesting depth, incremented on composite invocation.
        calls: The ledger, so a composite skill can record the components it
            delegated to rather than hiding them.
    """

    run_id: str
    depth: int = 0
    calls: list[SkillCall] = field(default_factory=list)


@dataclass(frozen=True)
class SkillSpec(Generic[In, Out]):
    """A callable capability with declared input and output types.

    Attributes:
        name: Unique within a registry. Skills are named after the document's
            own pseudocode (`plan`, `format_organised`) so the code can be read
            against 2.10 without a translation layer.
        description: One line on when to reach for it.
        input_model: Pydantic model the raw input is validated into.
        output_model: Pydantic model the handler must return.
        handler: Pure function `(input, context) -> output_model`.
        case_agnostic: Declared intent, verified by a test rather than
            assumed. Recorded here because a claim nobody checks is a
            comment; this one is checked.
        tags: Free-form labels, for discovery and for the notebook's index.
    """

    name: str
    description: str
    input_model: type[In]
    output_model: type[Out]
    handler: Callable[[In, SkillContext], Out]
    case_agnostic: bool = True
    tags: tuple[str, ...] = ()

    def failure_value(self) -> Any:
        """The `value` a failed call carries, or None if none is meaningful.

        Returns:
            A default-constructed `output_model` where that is a sensible
            degraded state, otherwise `None`.

        Used when a handler raises, so that a failed call is recorded with a
        well-typed result rather than a fabricated one. The distinction matters:
        `Document()` is a usable "nothing rendered yet" value because every
        field has a default, but `LockedPlan()` is not, because a blueprint with
        no course and no scope is not a degraded blueprint -- it is not a
        blueprint. Inventing defaults for it would put a plausible-looking
        object into the audit trail, which is the opposite of what a ledger is
        for.
        """
        try:
            return self.output_model()
        except ValidationError:
            return None

    def describe(self) -> str:
        """A readable declaration, for the notebook and the CLI index.

        Returns:
            A multi-line block: name, purpose, input fields with their
            annotations, output type, and the case-agnostic flag.
        """
        fields = ", ".join(
            f"{name}: {_annotation_text(spec.annotation)}"
            for name, spec in self.input_model.model_fields.items()
        )
        tags = ", ".join(self.tags) if self.tags else "-"
        return (
            f"skill {self.name!r}\n"
            f"    does      : {self.description}\n"
            f"    input     : {self.input_model.__name__}({fields})\n"
            f"    output    : {self.output_model.__name__}\n"
            f"    tags      : {tags}\n"
            f"    reusable  : {'yes' if self.case_agnostic else 'NO - NOT REUSABLE'}"
        )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class SkillLedger:
    """Append-only, ordered record of every skill invocation.

    Mutating the past is impossible by construction: `record` only appends,
    and entries are frozen dataclasses. There is no update and no delete,
    which is the point -- 2.12 asks for an audit log, and an audit log you
    can edit is not one.
    """

    def __init__(self, run_id: str = "unassigned") -> None:
        """Create an empty ledger.

        Args:
            run_id: The owning run's id, stamped onto every entry.
        """
        self.run_id = run_id
        self._entries: list[SkillCall] = []

    def __len__(self) -> int:
        """Number of recorded calls."""
        return len(self._entries)

    def __iter__(self):
        """Iterate entries in call order."""
        return iter(self._entries)

    @property
    def entries(self) -> tuple[SkillCall, ...]:
        """Every entry, as an immutable snapshot."""
        return tuple(self._entries)

    def names(self) -> list[str]:
        """Skill names in call order, including repeats.

        Repeats are kept. "How many times was this skill invoked" is a
        question an audit log has to be able to answer.
        """
        return [entry.skill for entry in self._entries]

    def by_skill(self, name: str) -> tuple[SkillCall, ...]:
        """Every entry for one skill, in order."""
        return tuple(entry for entry in self._entries if entry.skill == name)

    def record(self, call: SkillCall) -> SkillCall:
        """Append one entry.

        Args:
            call: The entry to append.

        Returns:
            The same entry, so callers can chain.
        """
        self._entries.append(call)
        return call

    def describe(self) -> str:
        """A readable audit table.

        Returns:
            One line per call, or a note when nothing ran.
        """
        if not self._entries:
            return "no skill calls were recorded"

        rows: list[tuple[str, str, str, str, str, str]] = [
            (
                entry.call_id,
                entry.skill,
                str(entry.depth),
                f"{entry.elapsed_ms:.2f}ms",
                "ok" if entry.ok else "FAILED",
                entry.error or "",
            )
            for entry in self._entries
        ]
        headers = ("call_id", "skill", "depth", "elapsed", "result", "error")
        widths = [
            max(len(headers[i]), max(len(row[i]) for row in rows))
            for i in range(len(headers))
        ]

        def line(values: tuple[str, ...]) -> str:
            return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

        out = ["SKILL CALL LEDGER", line(headers), "-" * (sum(widths) + 2 * (len(widths) - 1))]
        out.extend(line(row) for row in rows)
        out.append(f"({len(rows)} call(s))")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """The single place a skill is looked up, validated and recorded.

    Deliberately a registry rather than plain functions, for the same reason
    Lab 2's is: the agent must not be able to reach a capability by any route
    that skips validation and the ledger. A test asserts the Lab 3 agent
    invokes skills only through this object.
    """

    def __init__(self, run_id: Optional[str] = None) -> None:
        """Create an empty registry.

        Args:
            run_id: Run id for the ledger. Generated if omitted.
        """
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.ledger = SkillLedger(self.run_id)
        self._skills: dict[str, SkillSpec] = {}

    # -- registration ------------------------------------------------------

    def register(self, spec: SkillSpec) -> SkillSpec:
        """Add a skill.

        Args:
            spec: The declaration to add.

        Returns:
            The same spec, so a module can register-and-return at import time.

        Raises:
            SkillError: If a skill with this name already exists. Silent
                replacement would let a later import silently redefine a
                capability, which is indistinguishable from working code.
        """
        if spec.name in self._skills:
            raise SkillError(
                f"skill {spec.name!r} is already registered",
                code="duplicate_skill",
            )
        self._skills[spec.name] = spec
        return spec

    # -- lookup ------------------------------------------------------------

    def get(self, name: str) -> SkillSpec:
        """Look up one skill.

        Args:
            name: The skill name.

        Returns:
            Its declaration.

        Raises:
            SkillError: If unknown, listing what is available. The listing is
                the whole value of the error: a caller who guessed a name
                needs to see the real ones.
        """
        if name not in self._skills:
            raise SkillError(
                f"unknown skill {name!r}; available: {self.names()}",
                code="unknown_skill",
            )
        return self._skills[name]

    def names(self) -> list[str]:
        """Registered skill names, sorted."""
        return sorted(self._skills)

    def describe_all(self) -> str:
        """Every declaration as a readable block."""
        return "\n".join(self._skills[name].describe() for name in self.names())

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        name: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        context: Optional[SkillContext] = None,
        depth: int = 0,
    ) -> SkillResult[Any]:
        """Run a skill and record the call.

        The order of operations is the contract:

        1. look the skill up -- unknown is a `SkillError`,
        2. validate the input against the declared model -- invalid is a
           `SkillError`,
        3. run the handler -- a raise is *recorded* and returned as a failed
           `SkillResult`, because a capability that failed at runtime is an
           auditable fact, not a crash,
        4. check the returned type against the declared output model -- a
           mismatch is a `SkillContractError`, raised rather than recorded,
           because it means the skill is broken rather than misused,
        5. append to the ledger.

        Args:
            name: The skill to run.
            payload: A validated input model, or a raw mapping to validate.
            context: Optional shared context. A new one is made if omitted.
            depth: Nesting depth for the ledger.

        Returns:
            A `SkillResult`. On success its `value` is the declared output
            type; on failure `value` is `SkillSpec.failure_value()` and must
            not be read.

        Raises:
            SkillError: Unknown name, or input that fails validation.
            SkillContractError: The handler returned the wrong type.
        """
        spec = self.get(name)
        ctx = context or SkillContext(run_id=self.run_id, depth=depth)

        if isinstance(payload, BaseModel):
            validated = spec.input_model.model_validate(payload.model_dump())
        else:
            try:
                validated = spec.input_model.model_validate(dict(payload))
            except ValidationError as exc:
                raise SkillError(
                    f"invalid input for skill {name!r}: "
                    f"{exc.error_count()} problem(s). "
                    f"First: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}",
                    code="invalid_input",
                ) from exc

        call_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        ok, error = True, None
        value: Any = None

        # Step 3: run the handler. A raise is recorded and returned, because a
        # capability that failed at runtime is an auditable fact.
        try:
            value = spec.handler(validated, ctx)
        except Exception as exc:  # noqa: BLE001 - recorded, then reported
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("Skill %s failed: %s", name, error)

        if ok and not isinstance(value, spec.output_model):
            # Step 4 is deliberately OUTSIDE the handler's try. A type mismatch
            # means the skill is broken, not that the run failed, so it is
            # raised rather than recorded -- otherwise a skill bug would look
            # like ordinary runtime trouble in the audit log.
            raise SkillContractError(
                f"skill {name!r} returned {type(value).__name__}, "
                f"expected {spec.output_model.__name__}"
            )

        if not ok:
            # A failed call has no value, and the only honest representation of
            # that is None. An earlier version claimed every output model here
            # was default-constructible and called the constructor; it was not
            # -- `LockedPlan` and `Document` both have required fields, because
            # a plan with no course and a document with no id are not degraded
            # versions of themselves, they are nonsense.
            #
            # So: default-construct the model where that yields something
            # meaningful, and fall back to None where it does not. Either way
            # `ok` is False and `unwrap()` raises, so no caller can mistake the
            # stand-in for a real result. `model_construct()` is never used --
            # it skips validation and yields an object whose required fields
            # are merely absent, which fails later and further from the cause.
            value = spec.failure_value()

        call = SkillCall(
            call_id=call_id,
            skill=name,
            run_id=self.run_id,
            input_payload=validated.model_dump(mode="json"),
            output_type=spec.output_model.__name__,
            ok=ok,
            error=error,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 4),
            at=datetime.now(tz=timezone.utc),
            depth=ctx.depth,
        )
        self.ledger.record(call)
        return SkillResult(skill=name, call=call, value=value)
