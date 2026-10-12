"""The tool layer: a typed, audited boundary between agents and data.

Why this module exists
----------------------
Document 2.10, Lab 2, gives the goal precisely:

    "Give the agent tools it must call to act, rather than answering from
     memory."

and the acceptance criterion is behavioural:

    "Key numbers and lookups always go through tools, never hand-done in the
     prompt."

It also names the failure mode:

    **Pitfall to avoid.** Never assert a fact in the prompt that a tool should
    verify.

A pitfall stated in prose is not enforced. This module makes it **structurally
enforced** by four decisions:

1. **Typed in, typed out.** Every tool declares a Pydantic input model and a
   Pydantic output model (:class:`ToolSpec`). A tool cannot return an untyped
   dict, so downstream labs always receive something they can validate.
2. **An append-only ledger.** Every invocation -- successful or not -- is
   recorded in a :class:`ToolLedger` with its full input and output. The ledger
   is the *evidence* for Lab 2's acceptance criterion: you can assert that a
   number came from a tool, because the call is on the record.
3. **Handlers never see the store directly.** A handler receives a
   :class:`ToolContext`, which carries the registry but no filesystem handle.
   The only code allowed to open the seed directory is ``seed_store.py``.
4. **Errors are data, not exceptions.** A failed call is recorded with
   ``ok=False`` and an error string, so a failed lookup is auditable rather than
   silently swallowed. Malformed *input* is still an exception, because that is
   a programming error the caller should fix, not a runtime fact to log.

This is also the seam Lab 5 extends: swapping the data source changes
``seed_store.py`` and nothing above it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Iterator, Mapping, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from course_organizer.logging_setup import log_event

logger = logging.getLogger(__name__)

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


# ==========================================================================
# Errors
# ==========================================================================


class ToolError(RuntimeError):
    """Raised for a tool-layer failure the caller must handle.

    Covers unknown tool names and schema-invalid *inputs*. Handler-level
    failures do not raise: they are returned as ``ToolResult.ok is False`` so
    they appear in the ledger.
    """


# ==========================================================================
# The ledger -- the evidence trail
# ==========================================================================


@dataclass(frozen=True)
class ToolCall:
    """One recorded tool invocation.

    Attributes:
        call_id: Unique id, so an output can be traced back to its call even
            after several calls have interleaved.
        tool: Registered tool name.
        run_id: The run that issued the call, matching the logging run id.
        input_payload: Fully-validated input, as JSON-compatible data.
        ok: False if the handler reported a failure.
        error: Failure reason when ``ok`` is False.
        elapsed_ms: Wall-clock duration, for the Lab 8 sequential-vs-parallel
            comparison.
        at: Timezone-aware UTC timestamp.
        depth: Nesting level. 0 for a top-level agent call, 1 for a call made
            by another tool. This is how a composite tool's internals stay
            visible instead of hiding behind one entry.
    """

    call_id: str
    tool: str
    run_id: str
    input_payload: dict[str, Any]
    ok: bool
    error: Optional[str]
    elapsed_ms: float
    at: datetime
    depth: int

    def summary(self) -> str:
        """One-line human-readable form for logs and the notebook."""
        flag = "ok " if self.ok else "FAIL"
        return f"[{flag}] {self.tool}(depth={self.depth}) {self.elapsed_ms:.2f}ms {self.error or ''}".rstrip()


@dataclass
class ToolLedger:
    """Append-only record of every tool call made during a run.

    The document requires, in 2.12, "a full audit log of every grade published
    and by whom approved". Lab 2 builds the mechanism; Lab 6 makes it durable
    on disk. Keeping the in-memory ledger here means the audit trail is
    testable from Lab 2 onwards.

    Attributes:
        entries: Calls in issue order.
        run_id: The run this ledger belongs to.
    """

    run_id: str
    entries: list[ToolCall] = field(default_factory=list)

    def record(self, call: ToolCall) -> None:
        """Append one call. Never mutates an earlier entry."""
        self.entries.append(call)

    # -- queries -----------------------------------------------------------

    def names(self) -> list[str]:
        """Every tool name invoked, in order, including repeats."""
        return [entry.tool for entry in self.entries]

    def called(self, name: str) -> bool:
        """Was this tool invoked at least once?"""
        return any(entry.tool == name for entry in self.entries)

    def successful(self, name: str) -> bool:
        """Was this tool invoked at least once *without* failing?"""
        return any(entry.tool == name and entry.ok for entry in self.entries)

    def failures(self) -> list[ToolCall]:
        """Every failed call, for diagnosis."""
        return [entry for entry in self.entries if not entry.ok]

    def total_time_ms(self) -> float:
        """Summed handler time across all calls.

        Nested calls are included, so a composite tool's total is the sum of
        itself and everything it invoked.
        """
        return round(sum(entry.elapsed_ms for entry in self.entries), 3)

    def report(self) -> str:
        """A readable audit table."""
        if not self.entries:
            return "tool ledger is empty - no tool was called"
        header = f"{'#':>3}  {'tool':<28}{'depth':>5}{'ms':>9}  status"
        lines = [header, "-" * len(header)]
        for i, entry in enumerate(self.entries, 1):
            status = "ok" if entry.ok else f"FAIL: {entry.error}"
            lines.append(
                f"{i:>3}  {entry.tool:<28}{entry.depth:>5}{entry.elapsed_ms:>9.2f}  {status}"
            )
        lines.append("-" * len(header))
        lines.append(
            f"{len(self.entries):>3}  {'TOTAL':<28}{'':>5}{self.total_time_ms():>9.2f}  "
            f"{len(self.failures())} failed"
        )
        return "\n".join(lines)

    def __len__(self) -> int:
        """Number of recorded calls."""
        return len(self.entries)

    def __iter__(self) -> Iterator[ToolCall]:
        """Iterate calls in order."""
        return iter(self.entries)


# ==========================================================================
# Context
# ==========================================================================


@dataclass
class ToolContext:
    """What a tool handler is allowed to see.

    Deliberately does **not** expose a filesystem path, a `Settings` object or
    an LLM client. A handler can reach data only by calling another tool on the
    registry, which keeps nested calls on the same ledger.

    Attributes:
        registry: Used by a composite tool to call its dependencies, so those
            calls are recorded too.
        ledger: The append-only audit record.
        run_id: Correlates with the logging run id.
        _depth: Internal nesting counter.
    """

    registry: "ToolRegistry"
    ledger: ToolLedger
    run_id: str
    _depth: int = 0

    def child(self) -> "ToolContext":
        """A context for a nested call, one level deeper."""
        return ToolContext(
            registry=self.registry,
            ledger=self.ledger,
            run_id=self.run_id,
            _depth=self._depth + 1,
        )

    @property
    def depth(self) -> int:
        """Current nesting depth."""
        return self._depth

    def call(self, name: str, payload: BaseModel | Mapping[str, Any]) -> "ToolResult[Any]":
        """Invoke another tool on this run's ledger.

        Args:
            name: Registered tool name.
            payload: A validated input model, or a mapping validated into it.

        Returns:
            The nested `ToolResult`.

        Raises:
            ToolError: If the nested call fails. A composite tool that cannot
                complete is failing itself, so this propagates.
        """
        result = self.registry.invoke(name, payload, context=self.child())
        if not result.ok:
            raise ToolError(
                f"nested tool {name!r} failed during a composite tool: {result.error}"
            )
        return result


# ==========================================================================
# Result
# ==========================================================================


@dataclass(frozen=True)
class ToolResult(Generic[TOut]):
    """The outcome of one tool invocation.

    Attributes:
        tool: Tool name.
        value: The validated output model, present only when ``ok``.
        call: The ledger entry this result produced.
        ok: Whether the handler succeeded.
        error: Failure reason when ``ok`` is False.
    """

    tool: str
    call: ToolCall
    ok: bool
    value: Optional[TOut] = None
    error: Optional[str] = None

    def unwrap(self) -> TOut:
        """Return the output value, or raise if the call failed.

        Raises:
            ToolError: When ``ok`` is False.
        """
        if not self.ok or self.value is None:
            raise ToolError(f"{self.tool} failed: {self.error}")
        return self.value


# ==========================================================================
# Spec
# ==========================================================================


Handler = Callable[[Any, ToolContext], BaseModel]


@dataclass(frozen=True)
class ToolSpec(Generic[TIn, TOut]):
    """A typed tool declaration.

    Attributes:
        name: Unique registered name. The doc's connector reads use names that
            read like the document's own, e.g. ``read_notes_store``.
        summary: One-line description. This is what an LLM planner would read
            when deciding whether to call the tool, so it says when to call it,
            not just what it does.
        input_model: Pydantic model for the arguments.
        output_model: Pydantic model for the result.
        handler: ``(input_model, context) -> output_model``.
        doc_ref: The document requirement this tool serves, for traceability.
        read_only: True when the tool cannot mutate the store. Lab 6's audit
            and Lab 5's connector both key off this.
    """

    name: str
    summary: str
    input_model: type[TIn]
    output_model: type[TOut]
    handler: Handler
    doc_ref: str
    read_only: bool = True

    def describe(self) -> str:
        """A human-readable declaration, for the notebook and for reports."""
        inputs = ", ".join(
            f"{name}: {field.annotation}"
            for name, field in self.input_model.model_fields.items()
        )
        return (
            f"{self.name}({inputs}) -> {self.output_model.__name__}\n"
            f"    {self.summary}\n"
            f"    doc: {self.doc_ref} | read_only={self.read_only}"
        )


# ==========================================================================
# Registry
# ==========================================================================


class ToolRegistry:
    """Holds the available tools and executes calls against one ledger.

    This is the *only* way to reach data in this worklet. Agents are given a
    registry, never a store, which is what makes Lab 2's acceptance criterion
    checkable rather than aspirational.
    """

    def __init__(self, *, run_id: str) -> None:
        """Create an empty registry bound to a run id."""
        self._tools: dict[str, ToolSpec[Any, Any]] = {}
        self.ledger = ToolLedger(run_id=run_id)
        self.run_id = run_id

    # -- registration ------------------------------------------------------

    def register(self, spec: ToolSpec[TIn, TOut]) -> ToolSpec[TIn, TOut]:
        """Add a tool.

        Args:
            spec: The declaration to add.

        Returns:
            The same spec, so registration can decorate a definition inline.

        Raises:
            ToolError: If the name is already taken. Silent replacement would
                let a later lab quietly shadow an earlier tool.
        """
        if spec.name in self._tools:
            raise ToolError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        log_event(
            logger,
            "tool_registered",
            tool=spec.name,
            doc_ref=spec.doc_ref,
            read_only=spec.read_only,
        )
        return spec

    def tool(
        self,
        name: str,
        *,
        summary: str,
        input_model: type[TIn],
        output_model: type[TOut],
        doc_ref: str,
        read_only: bool = True,
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register`.

        Usage::

            @registry.tool(
                "read_notes_store",
                summary="Read the notes/materials store.",
                input_model=NotesQuery,
                output_model=NotesReadResult,
                doc_ref="2.10 Lab 2",
            )
            def read_notes_store(q: NotesQuery, ctx: ToolContext) -> NotesReadResult:
                ...

        Args:
            name: Unique tool name.
            summary: When to call this tool, in one line.
            input_model: Pydantic input model.
            output_model: Pydantic output model.
            doc_ref: Requirement this serves.
            read_only: Whether the tool can mutate the store.

        Returns:
            A decorator returning the handler unchanged.
        """

        def decorate(handler: Handler) -> Handler:
            self.register(
                ToolSpec(
                    name=name,
                    summary=summary,
                    input_model=input_model,
                    output_model=output_model,
                    handler=handler,
                    doc_ref=doc_ref,
                    read_only=read_only,
                )
            )
            return handler

        return decorate

    # -- lookup ------------------------------------------------------------

    def names(self) -> list[str]:
        """Every registered tool name, sorted."""
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec[Any, Any]:
        """Look up one spec.

        Raises:
            ToolError: If unknown, listing what is available.
        """
        if name not in self._tools:
            raise ToolError(f"unknown tool {name!r}; available: {self.names()}")
        return self._tools[name]

    def describe_all(self) -> str:
        """Every spec's declaration, as a readable block."""
        return "\n".join(self._tools[name].describe() for name in self.names())

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        name: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        context: Optional[ToolContext] = None,
    ) -> ToolResult[Any]:
        """Execute a tool and record the call.

        Input is validated against the tool's declared input model *before*
        the handler runs, so a handler never sees a malformed argument. A
        handler that raises produces a failed `ToolResult` rather than
        propagating, because a failed lookup is a runtime fact to audit.

        Args:
            name: Registered tool name.
            payload: A validated input model, or a mapping to validate.
            context: Parent context for nested calls. Omit for a top-level call.

        Returns:
            A `ToolResult`.

        Raises:
            ToolError: If the tool name is unknown, or the payload does not
                satisfy the declared input model.
        """
        spec = self.get(name)
        ctx = context or ToolContext(registry=self, ledger=self.ledger, run_id=self.run_id)

        if isinstance(payload, BaseModel):
            validated = spec.input_model.model_validate(payload.model_dump())
        else:
            try:
                validated = spec.input_model.model_validate(dict(payload))
            except ValidationError as exc:
                raise ToolError(
                    f"invalid input for tool {name!r}: {exc.error_count()} problem(s). "
                    f"First: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"
                ) from exc

        call_id = uuid.uuid4().hex[:12]
        input_payload = validated.model_dump(mode="json")
        started = time.perf_counter()
        ok = True
        error: Optional[str] = None
        value: Optional[BaseModel] = None

        try:
            value = spec.handler(validated, ctx)
            if not isinstance(value, spec.output_model):
                ok = False
                error = (
                    f"handler returned {type(value).__name__}, "
                    f"expected {spec.output_model.__name__}"
                )
        except Exception as exc:  # noqa: BLE001 - recorded, then reported
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("Tool %s failed: %s", name, error)

        elapsed_ms = (time.perf_counter() - started) * 1000
        call = ToolCall(
            call_id=call_id,
            tool=name,
            run_id=self.run_id,
            input_payload=input_payload,
            ok=ok,
            error=error,
            elapsed_ms=round(elapsed_ms, 4),
            at=datetime.now(tz=timezone.utc),
            depth=ctx.depth,
        )
        self.ledger.record(call)
        log_event(
            logger,
            "tool_call",
            tool=name,
            call_id=call_id,
            ok=ok,
            elapsed_ms=round(elapsed_ms, 2),
            depth=ctx.depth,
        )
        return ToolResult(tool=name, call=call, ok=ok, value=value, error=error)

    # -- interoperability --------------------------------------------------

    def as_langchain_tools(self) -> list[Any]:
        """Expose the registry as LangChain `StructuredTool` objects.

        Lab 7 binds these to graph nodes. Each tool keeps its typed schemas, so
        a node receives a validated Pydantic model rather than a string blob.

        Returns:
            One `StructuredTool` per registered tool.

        Raises:
            ImportError: If langchain-core is not installed.
        """
        try:
            from langchain_core.tools import StructuredTool
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ImportError(
                "langchain-core is required for LangGraph interop. "
                "Install it with: pip install langchain-core==1.6.6"
            ) from exc

        tools = []
        for name in self.names():
            spec = self._tools[name]

            def make_handler(spec: ToolSpec[Any, Any]) -> Callable[..., str]:
                """Build the callable LangChain will invoke.

                Returns the output as JSON so the text interface stays
                lossless; the typed path is `registry.invoke` directly.
                """

                def handler(**kwargs: Any) -> str:
                    """Invoke this tool and return its output as JSON."""
                    result = self.invoke(spec.name, kwargs)
                    if not result.ok:
                        return f'{{"ok": false, "error": {result.error!r}}}'
                    return result.value.model_dump_json()  # type: ignore[union-attr]

                handler.__name__ = spec.name
                handler.__doc__ = spec.summary
                return handler

            tools.append(
                StructuredTool.from_function(
                    func=make_handler(spec),
                    name=spec.name,
                    description=spec.summary,
                    args_schema=spec.input_model,
                )
            )
        return tools


__all__ = [
    "ToolCall",
    "ToolContext",
    "ToolError",
    "ToolLedger",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]