"""The governed surface: a registry of typed endpoints, with auth and a ledger.

Doc 2.10 Lab 5 asks for two things this module supplies:

    *"A connector server with typed endpoints and auth; agents call only
    through it."*

**Typed** means every endpoint declares a Pydantic input model *and* a Pydantic
output model, and the registry validates both directions on every call. It is
not documentation-as-types: an unvalidated payload raises
:class:`~course_organizer.connector.errors.ConnectorContractError` before an
agent can hold a reference to it.

**Auth** means no call reaches the adapter until a bearer token has been
resolved to a :class:`~course_organizer.connector.auth.Principal` and that
principal's role has been shown to grant the endpoint's scope. The order is
fixed and it matters: *authenticate, then authorise, then validate arguments,
then read, then validate the payload, then record*. Each of the first five
steps has its own failure mode, and collapsing them makes an incident
unreadable.

This registry deliberately mirrors the shape of Lab 2's
:class:`~course_organizer.tools.base.ToolRegistry`, Lab 3's
``SkillRegistry`` and Lab 4's
:class:`~course_organizer.memory.base.MemoryRegistry` -- same
``register``/``names``/``get``/``describe_all``/``invoke`` vocabulary, same
ledger alongside. Lab 6 wraps all four in one runtime, and a wrapper that has
to special-case one of them is a wrapper nobody trusts.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Iterator, Mapping, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from course_organizer.connector.auth import Principal, PrincipalRegistry
from course_organizer.connector.errors import (
    ConnectorContractError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
)
from course_organizer.connector.port import SourceAdapter, SourceDescriptor
from course_organizer.logging_setup import log_event

__all__ = [
    "ConnectorCall",
    "ConnectorLedger",
    "ConnectorResult",
    "EndpointHandler",
    "EndpointRegistry",
    "EndpointSpec",
    "Outcomes",
]

logger = logging.getLogger(__name__)

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)

#: The three things that can happen to a call. Refused and failed are kept
#: apart on purpose: *refused* is the policy working, *failed* is a problem.
Outcomes = ("allowed", "refused", "failed")

#: Document 2.10 Lab 5's three reads, verbatim. The decision about whether an
#: endpoint is required is made by matching :attr:`EndpointSpec.doc_read`
#: against these strings, and *only* against these strings. No guesswork, and no
#: conflation between "implemented in Lab 5" and "required by the document".
DOCUMENT_READ_PHRASES: tuple[str, ...] = (
    "read('the notes/materials store')",
    "read('assignment & rubric definitions')",
    "read('submission records')",
)


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Never naive local time, anywhere in this package."""
    return datetime.now(tz=timezone.utc)


class ConnectorCall(BaseModel):
    """One row of the connector's audit log.

    Doc 2.3 names the requirement: *"Governance layer: access control per role,
    version history, audit log of grades published."* Lab 5 implements the
    access-control half and the read-side log that Lab 6 will extend to every
    action. A row exists for **every** call, including the ones that were
    refused -- a log that only records successes cannot answer "what was
    attempted".

    Attributes:
        index: Monotonic position in this connector's run, starting at 1.
        endpoint: Which endpoint was called.
        principal: The authenticated caller's subject. A person, not a role.
        role: The role that decided authorisation.
        token_fingerprint: 16 hex characters of SHA-256 over the token. Present
            so an auditor can tell "which credential" without the credential
            being recoverable from the log.
        scope: The scope the endpoint required.
        outcome: One of :data:`Outcomes`.
        error: The refusal or failure message. Empty on success.
        source: The adapter's name, so a log can be compared across a swap.
        transport: ``"in_process"`` or ``"mcp_stdio"``. A call that arrived
            over the wire is evidence of a second process, which is exactly
            what an auditor wants to distinguish.
        records: How many domain records the call returned. Zero for a
            refusal, which is what makes a policy denial visible in a count.
        duration_ms: Wall-clock for the call, including adapter time.
        at: When the call finished.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    index: int = Field(ge=1)
    endpoint: str
    principal: str
    role: str
    token_fingerprint: str
    scope: str
    outcome: str
    error: str = ""
    source: str
    transport: str
    records: int = Field(ge=0)
    duration_ms: float = Field(ge=0.0)
    at: datetime

    @property
    def allowed(self) -> bool:
        """True when the call was authorised and served."""
        return self.outcome == "allowed"

    def summary(self) -> str:
        """One line for the lab report and the notebook.

        Never prints a credential -- only the fingerprint.
        """
        head = (
            f"{self.index:>3}. {self.endpoint:<24} {self.outcome:<8} "
            f"{self.principal} ({self.role})"
        )
        tail = (
            f"scope={self.scope} source={self.source} transport={self.transport} "
            f"records={self.records} {self.duration_ms:.1f}ms"
        )
        if self.error:
            return f"{head} {tail} error={self.error}"
        return f"{head} {tail}"


class ConnectorLedger:
    """Every call, allowed or not. The connector's memory of what happened.

    In-process and append-only, which is what makes the Lab 5 pitfall
    demonstrable: an agent that bypasses the connector does not merely get
    data faster, it produces a log with a **hole** in it. See
    :meth:`fingerprint`, which is the shape a hole is detected with.
    """

    def __init__(self) -> None:
        self._calls: list[ConnectorCall] = []

    def record(self, call: ConnectorCall) -> ConnectorCall:
        """Append one row and return it, so callers can log the same object."""
        self._calls.append(call)
        return call

    def __len__(self) -> int:
        """How many calls have been recorded."""
        return len(self._calls)

    def __iter__(self) -> Iterator[ConnectorCall]:
        """Iterate the rows in call order."""
        return iter(self._calls)

    def names(self) -> list[str]:
        """Every endpoint that has been called at least once, sorted."""
        return sorted({c.endpoint for c in self._calls})

    def called(self, name: str) -> bool:
        """Was this endpoint called at all?"""
        return any(c.endpoint == name for c in self._calls)

    def allowed(self, name: str) -> bool:
        """Was this endpoint called and served at least once?"""
        return any(c.endpoint == name and c.allowed for c in self._calls)

    def refusals(self) -> list[ConnectorCall]:
        """Every row the policy turned away."""
        return [c for c in self._calls if c.outcome == "refused"]

    def failures(self) -> list[ConnectorCall]:
        """Every row that errored for a non-policy reason."""
        return [c for c in self._calls if c.outcome == "failed"]

    def principals(self) -> list[str]:
        """Every subject that called, sorted."""
        return sorted({c.principal for c in self._calls})

    def last(self) -> Optional[ConnectorCall]:
        """The most recent row, or None if nothing has been called.

        Convenient for a caller that has just made one call and wants to
        assert on what was recorded, without re-deriving which row it was.
        """
        return self._calls[-1] if self._calls else None

    def for_endpoint(self, name: str) -> list[ConnectorCall]:
        """Every row for one endpoint, in call order.

        Args:
            name: The endpoint name.

        Returns:
            The matching rows. Empty if the endpoint was never called.
        """
        return [c for c in self._calls if c.endpoint == name]

    def total_time_ms(self) -> float:
        """Sum of every call's duration."""
        return round(sum(c.duration_ms for c in self._calls), 3)

    def total_records(self) -> int:
        """Total domain records served. Zero if every call was refused."""
        return sum(c.records for c in self._calls)

    def fingerprint(self) -> str:
        """A 16-hex digest of the whole log, for evidence.

        The digest covers outcome, endpoint, principal and record count, so a
        refused call changes it too. Two runs of the same scenario against the
        same source should produce the same fingerprint, which makes "the run
        is reproducible" a one-line check instead of a diff.

        Returns:
            16 hex characters of SHA-256.
        """
        import hashlib

        parts = "|".join(
            f"{c.endpoint}:{c.outcome}:{c.principal}:{c.role}:{c.records}"
            for c in self._calls
        )
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]

    def report(self) -> str:
        """The whole log, one line per call, plus a totals line."""
        if not self._calls:
            return "connector ledger: empty (no call has been made)"
        lines = [c.summary() for c in self._calls]
        lines.append(
            f"-- {len(self._calls)} call(s), {len(self.refusals())} refused, "
            f"{len(self.failures())} failed, {self.total_records()} record(s), "
            f"{self.total_time_ms():.1f}ms, fingerprint {self.fingerprint()}"
        )
        return "\n".join(lines)


EndpointHandler = Callable[[TIn, Principal, SourceDescriptor], TOut]
"""What an endpoint's implementation looks like.

Takes the validated input, the authenticated principal and the source's own
descriptor, and returns the output model. It is never handed a token, a
settings object or a filesystem path, so it cannot authenticate itself, cannot
reconfigure the connector and cannot reach around the adapter.
"""


class EndpointSpec(Generic[TIn, TOut]):
    """A typed endpoint declaration.

    Attributes:
        name: The endpoint name. The document's three reads keep the names
            Lab 2's tools already use -- ``read_notes_store``,
            ``read_assignment_rubrics``, ``read_submission_records`` -- so
            adding the connector does not rename anything an agent or a test
            already depends on. That is deliberate: a rename would break
            Labs 2-4 for no benefit.
        summary: What it does and when to call it.
        scope: The scope the endpoint requires. Checked against the caller's
            role before the handler runs.
        input_model: Pydantic model for the arguments.
        output_model: Pydantic model for the result.
        handler: The implementation.
        doc_ref: The document requirement this serves, for traceability.
        doc_read: The document's own phrase, e.g.
            ``read('the notes/materials store')``. Kept verbatim so a
            reviewer can match the code to the pseudocode by eye.
        added_by_lab: Which lab *implemented* this endpoint. ``"Lab 5"`` for
            all four, because Lab 5 wrote all four -- which is precisely why
            this field cannot answer whether the document asked for it. Use
            :attr:`documented` for that; the two questions are different, and
            conflating them is how a project's own addition ends up presented
            as a requirement.
        count_field: The name of the output field holding the domain records.
            Named explicitly because the output models also carry list fields
            that are *not* records -- ``declared_units``, ``course_codes`` --
            and inferring "the first list field" made the ledger report 5 notes
            for CS201 when the true count was 8. The ledger is audit evidence,
            so it does not get to be approximately right. When omitted it
            falls back to a field literally named ``notes``, ``assignments``,
            ``submissions`` or ``course_codes``, and to 0 otherwise.
    """

    __slots__ = (
        "name",
        "summary",
        "scope",
        "input_model",
        "output_model",
        "handler",
        "doc_ref",
        "doc_read",
        "added_by_lab",
        "count_field",
    )

    def __init__(
        self,
        *,
        name: str,
        summary: str,
        scope: str,
        input_model: type[TIn],
        output_model: type[TOut],
        handler: EndpointHandler[TIn, TOut],
        doc_ref: str,
        doc_read: str,
        added_by_lab: str = "Lab 5",
        count_field: Optional[str] = None,
    ) -> None:
        self.name = name
        self.summary = summary
        self.scope = scope
        self.input_model = input_model
        self.output_model = output_model
        self.handler = handler
        self.doc_ref = doc_ref
        self.doc_read = doc_read
        self.added_by_lab = added_by_lab
        self.count_field = count_field or _default_count_field(output_model)

    def input_schema(self) -> dict[str, Any]:
        """The JSON Schema of the arguments, for the wire and for the docs."""
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        """The JSON Schema of the result."""
        return self.output_model.model_json_schema()

    @property
    def documented(self) -> bool:
        """Does the document require this endpoint?

        Decided by matching :attr:`doc_read` against the three phrases in the
        document's own pseudocode, and *only* by that. An earlier version of
        this class inferred it from :attr:`added_by_lab`, which was wrong in a
        way worth recording: Lab 5 implemented all four endpoints, so every one
        of them carried ``added_by_lab="Lab 5"`` and the field could not tell
        the three the document asks for from the one this project added. Every
        endpoint then reported itself as an addition, and the traceability
        matrix -- whose entire job is to say what is required -- inverted.

        Matching on the document's verbatim text also means an endpoint cannot
        become "documented" by being *named* as such. The phrase either appears
        in doc 2.10's pseudocode or it does not.
        """
        return self.doc_read in DOCUMENT_READ_PHRASES

    @property
    def origin(self) -> str:
        """One line saying where this endpoint's requirement comes from."""
        if self.documented:
            return f"documented: {self.doc_read}  [{self.doc_ref}]"
        return f"added by {self.added_by_lab}, not a documented requirement"

    def describe(self) -> str:
        """A human-readable declaration for the notebook and the report."""
        inputs = ", ".join(
            f"{n}" for n in self.input_model.model_fields
        )
        return (
            f"{self.name}({inputs}) -> {self.output_model.__name__}\n"
            f"    scope    : {self.scope}\n"
            f"    origin   : {self.origin}\n"
            f"    summary  : {self.summary}"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EndpointSpec {self.name} scope={self.scope}>"


class ConnectorResult(Generic[TOut]):
    """A validated endpoint result, tagged with who asked and over what.

    The models come back typed, and the envelope says where the data came
    from and who got it. Agents receive this object; they do not receive a
    bare list of records with no way to tell which source answered.
    """

    __slots__ = ("value", "principal", "descriptor", "endpoint", "call")

    def __init__(
        self,
        value: TOut,
        *,
        principal: Principal,
        descriptor: SourceDescriptor,
        endpoint: str,
        call: ConnectorCall,
    ) -> None:
        self.value = value
        self.principal = principal
        self.descriptor = descriptor
        self.endpoint = endpoint
        self.call = call

    def unwrap(self) -> TOut:
        """The validated value. Mirrors ``ToolResult.unwrap`` from Lab 2."""
        return self.value

    def provenance(self) -> str:
        """One line: endpoint, source, transport, who asked."""
        return (
            f"{self.endpoint} <- {self.descriptor.name} "
            f"({self.descriptor.provenance}) via {self.call.transport}, "
            f"read by {self.principal.subject} ({self.principal.role})"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ConnectorResult {self.endpoint} {type(self.value).__name__}>"


class EndpointRegistry:
    """The connector: typed endpoints, one source adapter, per-role auth, a log.

    Args:
        adapter: The swappable source. Exactly one, chosen by
            :func:`~course_organizer.connector.adapters.build_source_adapter`.
        principals: Token-to-principal registry. The connector's auth root.
        ledger: An existing ledger to append to, for tests that need to
            inspect a shared log across two connectors.
        transport: How callers are reaching this registry, recorded on every
            row. ``"in_process"`` for a direct call, ``"mcp_stdio"`` for a
            call that arrived over a spawned MCP server.

    Example:
        >>> from course_organizer.connector import build_default_connector
        >>> connector = build_default_connector()
        >>> connector.names()
        ['list_courses', 'read_assignment_rubrics', 'read_notes_store', 'read_submission_records']
    """

    def __init__(
        self,
        *,
        adapter: SourceAdapter,
        principals: PrincipalRegistry,
        ledger: Optional[ConnectorLedger] = None,
        transport: str = "in_process",
    ) -> None:
        self._adapter = adapter
        self._principals = principals
        self._ledger = ledger if ledger is not None else ConnectorLedger()
        self._transport = transport
        self._endpoints: dict[str, EndpointSpec[Any, Any]] = {}
        self._calls = 0

    # -- declaration -----------------------------------------------------
    def register(self, spec: EndpointSpec[TIn, TOut]) -> EndpointSpec[TIn, TOut]:
        """Add an endpoint.

        Args:
            spec: The declaration.

        Returns:
            The same spec, for chaining.

        Raises:
            ValueError: If the name is taken. A silent overwrite would make a
                typo in one module change another module's endpoint.
        """
        if spec.name in self._endpoints:
            raise ValueError(
                f"endpoint {spec.name!r} is already registered; endpoint names "
                f"are the connector's public contract and are not shadowable"
            )
        self._endpoints[spec.name] = spec
        log_event(
            logger,
            "connector.endpoint.registered",
            endpoint=spec.name,
            scope=spec.scope,
            source=self._adapter.descriptor.name,
        )
        return spec

    def endpoint(
        self,
        name: str,
        *,
        summary: str,
        scope: str,
        input_model: type[TIn],
        output_model: type[TOut],
        doc_ref: str,
        doc_read: str,
        added_by_lab: str = "Lab 5",
        count_field: Optional[str] = None,
    ) -> Callable[[EndpointHandler[TIn, TOut]], EndpointHandler[TIn, TOut]]:
        """Decorator form of :meth:`register`.

        Args:
            name: The endpoint name.
            summary: What it does and when to call it.
            scope: The scope required.
            input_model: Pydantic model for the arguments.
            output_model: Pydantic model for the result.
            doc_ref: The document requirement served.
            doc_read: The document's own phrase for this read.
            added_by_lab: Which lab introduced it, if not the document's own.
            count_field: Which output field holds the domain records, for the
                ledger. Inferred by name when omitted.

        Returns:
            A decorator that registers the handler and returns it unchanged.
        """

        def decorate(handler: EndpointHandler[TIn, TOut]) -> EndpointHandler[TIn, TOut]:
            self.register(
                EndpointSpec(
                    name=name,
                    summary=summary,
                    scope=scope,
                    input_model=input_model,
                    output_model=output_model,
                    handler=handler,
                    doc_ref=doc_ref,
                    doc_read=doc_read,
                    added_by_lab=added_by_lab,
                    count_field=count_field,
                )
            )
            return handler

        return decorate

    # -- introspection ---------------------------------------------------
    @property
    def adapter(self) -> SourceAdapter:
        """The source adapter. Only this package should need it."""
        return self._adapter

    @property
    def descriptor(self) -> SourceDescriptor:
        """How the current source describes itself."""
        return self._adapter.descriptor

    @property
    def ledger(self) -> ConnectorLedger:
        """The audit log."""
        return self._ledger

    @property
    def principal_registry(self) -> PrincipalRegistry:
        """The token-to-principal registry."""
        return self._principals

    @property
    def transport(self) -> str:
        """How callers reach this registry. Recorded on every row."""
        return self._transport

    def __len__(self) -> int:
        """How many endpoints are registered."""
        return len(self._endpoints)

    def __iter__(self) -> Iterator[EndpointSpec[Any, Any]]:
        """Iterate the endpoint declarations, sorted by name."""
        return iter(
            [self._endpoints[n] for n in sorted(self._endpoints)]
        )

    def names(self) -> list[str]:
        """Every registered endpoint name, sorted."""
        return sorted(self._endpoints)

    def get(self, name: str) -> EndpointSpec[Any, Any]:
        """Look up one endpoint.

        Args:
            name: The endpoint name.

        Returns:
            Its declaration.

        Raises:
            ConnectorNotFoundError: If no such endpoint. Listing the real
                names in the message turns a typo into a one-glance fix.
        """
        if name not in self._endpoints:
            raise ConnectorNotFoundError(
                f"no connector endpoint named {name!r}; available: "
                f"{self.names()}"
            )
        return self._endpoints[name]

    def describe_all(self) -> str:
        """Every endpoint's declaration, one block each."""
        blocks = [
            f"[{index}] {spec.describe()}"
            for index, spec in enumerate(self, start=1)
        ]
        blocks.append(
            f"-- {len(self._endpoints)} endpoint(s) over source "
            f"{self._adapter.descriptor.describe()}"
        )
        return "\n".join(blocks)

    def schemas(self) -> dict[str, dict[str, Any]]:
        """Every endpoint's input and output JSON Schema.

        This is what the MCP server publishes, and what
        ``tests/test_lab05_mcp_connector.py`` diffs against the live
        ``tools/list`` response to prove the wire contract matches the code.
        """
        return {
            name: {
                "input": self._endpoints[name].input_schema(),
                "output": self._endpoints[name].output_schema(),
                "scope": self._endpoints[name].scope,
                "doc_read": self._endpoints[name].doc_read,
                "doc_ref": self._endpoints[name].doc_ref,
            }
            for name in self.names()
        }

    # -- the call path ---------------------------------------------------
    def invoke(
        self,
        name: str,
        payload: BaseModel | Mapping[str, Any] | None = None,
        *,
        token: Optional[str] = None,
    ) -> ConnectorResult[Any]:
        """Call one endpoint. The connector's only entry point.

        The order of the six steps below is the contract, not an
        implementation detail:

        1. **Resolve the endpoint.** A name that is not registered is a
           not-found, before any credential is examined.
        2. **Authenticate.** No token, or an unknown one, is an auth error.
        3. **Authorise.** The principal's role must grant the endpoint's scope.
        4. **Validate the arguments** against the declared input model.
        5. **Read** through the adapter.
        6. **Validate the payload** against the declared output model, then
           record the row.

        Steps 1-4 are all refusals that never touch the source, and all of them
        write a row. Step 6's validation is the one that makes the wire
        trustworthy: MCP carries results as JSON inside a text block, so a
        payload arriving from another process has been through ``json.loads``
        and could be anything. A response that does not match its declared
        output model is refused rather than handed over.

        Args:
            name: The endpoint name.
            payload: A validated model, or a mapping to validate.
            token: The bearer credential. Never sourced from a model.

        Returns:
            A :class:`ConnectorResult` wrapping the validated output.

        Raises:
            ConnectorNotFoundError: Unknown endpoint, or a domain "no such
                course" from the adapter.
            ConnectorAuthError: No or unrecognised token.
            ConnectorPolicyError: Role does not grant the scope.
            ConnectorContractError: Arguments or payload failed validation.
            SourceUnavailableError: The adapter itself is unusable.
        """
        spec = self.get(name)
        principal: Optional[Principal] = None
        scope = spec.scope
        outcome = "failed"
        error = ""
        records = 0
        started = time.perf_counter()

        try:
            # 1. endpoint
            # (done above, via self.get)
            # 2. authenticate
            principal = self._principals.resolve(token)
            # 3. authorise
            principal.require(scope, endpoint=name)
            # 4. validate arguments
            inputs = self._coerce_in(spec, payload)
            # 5. read
            raw = spec.handler(inputs, principal, self._adapter.descriptor)
            # 6. validate the payload
            value = self._coerce_out(spec, raw)
            records = self._count_records(spec, value)
            outcome = "allowed"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            outcome = "refused" if _is_refusal(exc) else "failed"
            self._log(
                spec=spec,
                principal=principal,
                scope=scope,
                outcome=outcome,
                error=error,
                records=0,
                started=started,
            )
            raise

        call = self._log(
            spec=spec,
            principal=principal,
            scope=scope,
            outcome=outcome,
            error=error,
            records=records,
            started=started,
        )
        assert principal is not None  # narrowed: outcome == "allowed" implies it
        return ConnectorResult(
            value,
            principal=principal,
            descriptor=self._adapter.descriptor,
            endpoint=name,
            call=call,
        )

    # -- internals -------------------------------------------------------
    def _coerce_in(
        self, spec: EndpointSpec[Any, Any], payload: BaseModel | Mapping[str, Any] | None
    ) -> BaseModel:
        """Validate arguments against the declared input model."""
        if payload is None:
            data: dict[str, Any] = {}
        elif isinstance(payload, BaseModel):
            if type(payload) is not spec.input_model:
                raise ConnectorContractError(
                    f"endpoint {spec.name!r} takes {spec.input_model.__name__}, "
                    f"but was given a {type(payload).__name__}. Re-validate the "
                    f"arguments through the declared model rather than passing "
                    f"an arbitrary object across the boundary."
                )
            return payload
        elif isinstance(payload, Mapping):
            data = dict(payload)
        else:
            raise ConnectorContractError(
                f"endpoint {spec.name!r} takes {spec.input_model.__name__} or a "
                f"mapping; got {type(payload).__name__}"
            )
        try:
            return spec.input_model.model_validate(data)
        except ValidationError as exc:
            raise ConnectorContractError(
                f"arguments for {spec.name!r} do not match "
                f"{spec.input_model.__name__}: {exc.error_count()} problem(s); "
                f"first: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"
            ) from exc

    def _coerce_out(self, spec: EndpointSpec[Any, Any], raw: Any) -> BaseModel:
        """Validate a result against the declared output model."""
        if isinstance(raw, BaseModel) and type(raw) is spec.output_model:
            return raw
        try:
            if isinstance(raw, BaseModel):
                return spec.output_model.model_validate(
                    raw.model_dump(mode="json")
                )
            if isinstance(raw, Mapping):
                return spec.output_model.model_validate(dict(raw))
            return spec.output_model.model_validate(raw, from_attributes=True)
        except ValidationError as exc:
            raise ConnectorContractError(
                f"endpoint {spec.name!r} returned a payload that does not match "
                f"its declared {spec.output_model.__name__}: {exc.error_count()} "
                f"problem(s); first: {exc.errors()[0]['loc']} "
                f"{exc.errors()[0]['msg']}. The contract is the endpoint's, so "
                f"a payload that fails it is refused rather than passed on."
            ) from exc

    @staticmethod
    def _count_records(spec: EndpointSpec[Any, Any], value: BaseModel) -> int:
        """How many domain records a result carries, for the ledger.

        Uses the spec's declared ``count_field`` and nothing else. An earlier
        version inferred "the first list-valued field", which on
        ``NotesResult`` found ``declared_units`` (5 entries) instead of
        ``notes`` (8 entries) -- an audit log that undercounts the data it
        served is worse than no audit log, because it looks correct.

        Args:
            spec: The endpoint that produced the value.
            value: Its validated output model.

        Returns:
            The length of the declared field, or 0 if it is absent or empty.
        """
        field = spec.count_field
        if not field or field not in type(value).model_fields:
            return 0
        try:
            return len(getattr(value, field))
        except TypeError:  # pragma: no cover - defensive
            return 0

    def _log(
        self,
        *,
        spec: EndpointSpec[Any, Any],
        principal: Optional[Principal],
        scope: str,
        outcome: str,
        error: str,
        records: int,
        started: float,
    ) -> ConnectorCall:
        """Write one row, whatever happened."""
        self._calls += 1
        call = ConnectorCall(
            index=self._calls,
            endpoint=spec.name,
            principal=principal.subject if principal else "<unauthenticated>",
            role=principal.role if principal else "-",
            token_fingerprint=principal.fingerprint() if principal else "-",
            scope=scope,
            outcome=outcome,
            error=error,
            source=self._adapter.descriptor.name,
            transport=self._transport,
            records=records,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            at=_utcnow(),
        )
        self._ledger.record(call)
        log_event(
            logger,
            "connector.call",
            endpoint=call.endpoint,
            outcome=call.outcome,
            principal=call.principal,
            role=call.role,
            source=call.source,
            transport=call.transport,
            records=call.records,
            duration_ms=call.duration_ms,
            error=error or None,
        )
        return call


def _default_count_field(output_model: type[BaseModel]) -> Optional[str]:
    """Pick the records field of an output model by name.

    A named lookup rather than a structural guess: every endpoint result model
    in this package names its records field one of these four things, and a
    new endpoint that does not is a new endpoint that has to say so via
    ``count_field`` explicitly.

    Args:
        output_model: The endpoint's declared output model.

    Returns:
        The field name, or None if the model has no recognisable records
        field -- in which case the ledger records 0 and the endpoint is
        expected to pass ``count_field`` explicitly.
    """
    fields = output_model.model_fields
    for candidate in ("notes", "assignments", "submissions", "course_codes"):
        if candidate in fields:
            return candidate
    return None


def _is_refusal(exc: BaseException) -> bool:
    """Is this exception a deliberate refusal rather than a fault?

    Refusals mean the governance worked: no credential, the wrong role, bad
    arguments, a payload that broke its contract, a course that does not
    exist. They are recorded as ``refused`` so a run that was correctly
    blocked and a run that crashed can be told apart at a glance -- which is
    the difference between "the policy did its job" and "something is broken".
    """
    from course_organizer.connector.errors import (
        ConnectorAuthError,
        ConnectorContractError,
        ConnectorPolicyError,
    )

    return isinstance(
        exc,
        (
            ConnectorAuthError,
            ConnectorPolicyError,
            ConnectorContractError,
            ConnectorNotFoundError,
        ),
    )
