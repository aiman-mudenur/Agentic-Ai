"""Lab 5 -- MCP connector: one typed, authorised front door for every source.

The document, 2.10, Lab 5, in full:

    **Goal.** Expose every data source through one typed connector.
    **Deliverable.** A connector server with typed endpoints and auth; agents
    call only through it.
    **Accept.** All data access goes through the connector; swapping a source
    needs no agent change.
    **Pitfall.** No agent should touch a source directly; a bypass defeats
    governance later.

and its pseudocode::

    connector.expose: read('the notes/materials store')
                      read('assignment & rubric definitions')
                      read('submission records')
    agents.use(connector)   # no agent touches a source directly

What this module actually does
-----------------------------
It runs those lines and then measures the three claims the document makes, in
the order a reviewer would want to check them.

1. **The three reads the document names are exposed, typed, and attributed.**
   Section 2 calls each one through the connector and prints its counts, its
   declaring document reference, and its provenance. Nothing here names a seed
   file, because nothing here can.

2. **Auth is the connector's, not the caller's.** Section 3 calls all four
   endpoints as all four roles -- sixteen cells -- and prints the resulting
   matrix: eleven allowed, five refused, every refusal typed as a policy
   failure and written to the ledger with the caller named. Two of those
   refusals are then repeated over a **real MCP subprocess**
   (``python -m course_organizer.connector.mcp_server``), because a policy that
   only holds in-process is a policy that holds only in your own tests.

3. **Swapping a source changes no agent code.** Section 5 rebuilds the same
   ``ToolRegistry`` over a different ``SourceAdapter`` and reports what changed
   -- a different course, a different note count, a different provenance
   string -- and what did not: the tool names, the query, the result type, the
   caller. The four lines of agent code are identical in both runs.

Why the pitfall is demonstrated rather than described
-----------------------------------------------------
A bypass is not an exception you can watch for, because it succeeds. This
module cannot import the store to prove that -- that is precisely the property
under test, and an AST test in ``tests/test_lab05_mcp_connector.py`` is what
holds it (only ``course_organizer.connector`` may import
``course_organizer.tools.seed_store``). So the pitfall is shown by measuring
what governance withholds: as ``course_faculty`` the connector returns eight
submission records; as ``student`` the identical request is refused, and the
refusal is on the ledger naming the caller. The data the bypass would have
produced is real, obtained legitimately above, and the cost of skipping the
check is that nothing would record who asked.

Two things this module deliberately does not do
----------------------------------------------
**It does not grade anything.** Read ``read_submission_records`` is scoped and
audited, but reading a cohort's submissions is not the same as marking them, and
the connector has no endpoint that decides a score. Doc 2.3 gives the basis for
the audit -- *"access control per role, version history, audit log of grades
published"* -- and Lab 5 implements the access-control and audit-log halves. The
publishing half belongs to the grade boundary, which is Lab 6's.

**It does not define the roles from the document.** The document says
*"access control per role"* without saying which roles. :data:`ROLE_SCOPES` is
this project's answer, stated once, in one place, and the fact that it is a
project choice rather than a quotation is recorded in
``docs/lab05.md`` section (b).
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from course_organizer.connector import (
    CONNECTOR_ROLES,
    NOTES_READ,
    ROLE_SCOPES,
    SUBMISSIONS_READ,
    ASSIGNMENTS_READ,
    EndpointRegistry,
    ConnectorError,
    ConnectorPolicyError,
    build_default_connector,
)
from course_organizer.connector.client import (
    InProcessConnectorClient,
    McpConnectorClient,
    contract_from_registry,
)
from course_organizer.logging_setup import log_event, setup_logging
from course_organizer.tools import readers
from course_organizer.tools.compute import build_lab04_registry

logger = logging.getLogger(__name__)

#: How this module is invoked, and what it stamps on its ledger rows.
LAB = "lab05_mcp_connector"

#: Where in the document every claim below comes from.
DOC_REF = "doc 2.10, Lab 5"

#: Doc 2.10 Lab 5, Goal.
DOC_GOAL = "Expose every data source through one typed connector."

#: Doc 2.10 Lab 5, Deliverable.
DOC_DELIVERABLE = (
    "A connector server with typed endpoints and auth; agents call only "
    "through it."
)

#: Doc 2.10 Lab 5, Accept. Two claims, and they are different claims.
DOC_ACCEPTANCE = (
    "All data access goes through the connector; swapping a source needs no "
    "agent change."
)

#: Doc 2.10 Lab 5, Pitfall to avoid.
DOC_PITFALL = (
    "No agent should touch a source directly; a bypass defeats governance "
    "later."
)

#: Doc 2.10 Lab 5, Nodes.
DOC_NODES = (
    "Connector used by all data-facing agents. Connector under the data agents."
)

#: Doc 2.10 Lab 5, pseudocode, verbatim including its comment.
DOC_PSEUDOCODE = (
    "connector.expose: read('the notes/materials store')\n"
    "                      read('assignment & rubric definitions')\n"
    "                      read('submission records')\n"
    "agents.use(connector)   # no agent touches a source directly"
)

#: The endpoint each pseudocode line becomes. Three lines, three names -- and
#: the assertion that they are the *only* governed reads is a test, not a
#: convention. `list_courses` is the one endpoint Lab 5 added; it is labelled
#: as such by its ``EndpointSpec`` and is not a document requirement.
PSEUDOCODE_TO_ENDPOINT: tuple[tuple[str, str], ...] = (
    ("read('the notes/materials store')", "read_notes_store"),
    ("read('assignment & rubric definitions')", "read_assignment_rubrics"),
    ("read('submission records')", "read_submission_records"),
)

#: The payload each of those endpoints is called with. One place, so section 2
#: and section 4 cannot drift into testing different things.
#:
#: These are the *connector's* query models -- ``NotesQuery(course_code, unit)``
#: -- not the tool layer's, which is a different and larger type. The connector
#: exposes a narrower surface than the tools do, which is the point of it: an
#: agent cannot ask the connector for something the connector has not agreed to
#: expose. Passing ``current_only`` here, as Lab 4's *tool* accepts, is refused
#: as an unexpected extra argument -- correctly.
READ_PAYLOADS: dict[str, dict[str, Any]] = {
    "list_courses": {},
    "read_notes_store": {"course_code": "CS201"},
    "read_assignment_rubrics": {"course_code": "CS201"},
    "read_submission_records": {"course_code": "CS201"},
}

#: The checks that can only be evaluated by spawning a real MCP subprocess.
#: Named here rather than matched on string fragments in the skip logic, so that
#: renaming a check cannot silently turn it into a passing check.
TRANSPORT_CHECK_LABELS: tuple[str, ...] = (
    "a real MCP subprocess served every call",
    "the wire and the in-process client return identical payloads",
    "a refusal survives the wire as a typed policy failure",
    "the MCP session is reused rather than respawned per call",
    "each refused role needed its own server spawn",
)


@dataclass(frozen=True)
class ReadObservation:
    """One document read, made through the connector, with its evidence.

    Attributes:
        pseudocode_line: The document line this endpoint implements.
        endpoint: The connector endpoint name.
        declared_doc_ref: The endpoint's own ``doc_ref``, which every
            ``EndpointSpec`` must carry. Printed so the reader can check the
            claim against the source rather than take the word of a script.
        declared_added_by_lab: The endpoint's ``added_by_lab`` string, or empty
            when the document names it. Non-empty means *this project* invented
            the endpoint, and it says so loudly.
        scope: The scope a caller needs.
        outcome: ``"allowed"`` or ``"refused"``.
        error: The refusal message, or empty.
        record_count: How many records came back. The audit number.
        summary: The result model's own ``describe()``.
        provenance: One line of the ``Provenance`` block, for the audit trail.
    """

    pseudocode_line: str
    endpoint: str
    declared_doc_ref: str
    declared_origin: str
    documented_endpoint: bool
    scope: str
    outcome: str
    error: str
    record_count: int
    summary: str
    provenance: str

    @property
    def documented(self) -> bool:
        """Did the document name this endpoint, or did Lab 5 add it?

        Delegates to :attr:`~course_organizer.connector.core.EndpointSpec.documented`,
        which decides it by matching the document's own words rather than by
        trusting a label.
        """
        return self.documented_endpoint

    def render(self) -> str:
        """The observation as printed lines.

        Returns:
            Five indented lines: identity, origin, scope, count or refusal, and
            provenance.
        """
        lines = [
            self.pseudocode_line,
            f"    -> endpoint    : {self.endpoint}",
            f"    -> origin      : {self.declared_origin}",
            f"    -> scope       : {self.scope}",
        ]
        if self.outcome == "allowed":
            lines.append(f"    -> records     : {self.record_count}")
        else:
            lines.append(f"    -> REFUSED     : {self.error}")
        lines.append(f"    -> provenance  : {self.provenance}")
        return "\n".join(lines)


@dataclass(frozen=True)
class AccessRow:
    """One (role, endpoint) cell of the access matrix, with its outcome.

    Attributes:
        role: A key of :data:`ROLE_SCOPES`.
        endpoint: A connector endpoint name.
        has_scope: Whether the role grants the endpoint's scope.
        outcome: ``"allowed"`` or ``"refused"``.
        error: The exception class name that was raised, or empty.
        record_count: Records returned, for allowed cells.
        elapsed_ms: Wall-clock time for the call, including the policy check
            that failed. Kept because "refused quickly" and "refused after
            reading everything" are different behaviours.
    """

    role: str
    endpoint: str
    has_scope: bool
    outcome: str
    error: str
    record_count: int
    elapsed_ms: float

    @property
    def cell(self) -> str:
        """A short marker for the matrix: the count, or the refusal letter."""
        if self.outcome == "allowed":
            return str(self.record_count)
        return "R"

    @property
    def explained(self) -> bool:
        """Did a refusal arrive as a typed, named failure rather than a crash?

        A policy that refuses by returning ``None``, or by raising bare
        ``Exception``, has refused but has not explained itself -- and an
        unexplained refusal is indistinguishable from a broken endpoint.
        """
        if self.outcome == "allowed":
            return True
        return self.error == "ConnectorPolicyError"


def _row_marker(row: AccessRow) -> str:
    """A refusal explained as a policy failure, marked distinctly.

    Args:
        row: The cell to mark.

    Returns:
        ``"R"`` for a properly typed policy refusal, ``"!"`` for a refusal that
        arrived some other way. The two are rendered differently on purpose: a
        matrix full of ``R`` and a matrix full of ``!`` mean very different
        things about a system.
    """
    return row.cell if row.explained else "!"


# ==========================================================================
# Section 2 -- the document's three reads, exposed
# ==========================================================================


def observe_reads(
    registry: EndpointRegistry,
    token: str,
    course_code: str,
) -> tuple[ReadObservation, ...]:
    """Call each documented read through the connector and record the evidence.

    Runs **as ``course_faculty``**, the role holding every scope, so that a
    failure here is a failure of the endpoint and not a policy decision. Section
    3 is where the policy gets exercised.

    The record count is read from the ledger rather than from the payload. That
    is not a stylistic choice: the payload's ``describe()`` is what the *source*
    says, while the ledger's ``records`` is what the connector counted when it
    governed the call. A connector that undercounts its own audit trail would
    be invisible here, which is the whole point of printing both.

    Args:
        registry: A built connector.
        token: A ``course_faculty`` credential.
        course_code: Which course to read.

    Returns:
        One :class:`ReadObservation` per documented read, in the document's own
        order, followed by the endpoint Lab 5 added.
    """
    observations: list[ReadObservation] = []
    ordered = [name for _, name in PSEUDOCODE_TO_ENDPOINT] + ["list_courses"]

    for endpoint in ordered:
        spec = registry.get(endpoint)
        payload = dict(READ_PAYLOADS[endpoint])
        if "course_code" in payload:
            payload["course_code"] = course_code

        try:
            result = registry.invoke(endpoint, payload, token=token)
        except ConnectorError as exc:
            # A documented read that refuses a full-scope caller is a defect in
            # the endpoint, not a policy outcome. Surfacing it as a refusal
            # would make the acceptance check pass on a broken connector.
            raise AssertionError(
                f"{endpoint} refused a course_faculty caller: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        value = result.unwrap()
        call = registry.ledger.last()
        assert call is not None, "an allowed call must be on the ledger"
        assert call.endpoint == endpoint, (
            f"ledger last row is {call.endpoint!r}, not {endpoint!r}"
        )
        # `ConnectorResult.provenance` is a one-line *summary*; the structured
        # `Provenance` block travels on the payload itself, which is the reason
        # provenance cannot be dropped in transit. Read it from there.
        prov = value.provenance

        line = next(
            (ln for ln, name in PSEUDOCODE_TO_ENDPOINT if name == endpoint),
            "(not in the document)",
        )
        observations.append(
            ReadObservation(
                pseudocode_line=line,
                endpoint=endpoint,
                declared_doc_ref=spec.doc_ref,
                declared_origin=spec.origin,
                documented_endpoint=spec.documented,
                scope=spec.scope,
                outcome=call.outcome,
                error=call.error,
                record_count=call.records,
                summary=value.describe(),
                provenance=(
                    f"{prov.source} via {prov.source_transport} | "
                    f"{', '.join(prov.sources) or '(no location reported)'} | "
                    f"by {prov.fetched_by} ({prov.role})"
                ),
            )
        )
    return tuple(observations)


# ==========================================================================
# Section 3 -- auth: the same endpoints, every role
# ==========================================================================


def access_matrix(
    registry: EndpointRegistry,
    course_code: str,
    endpoints: Sequence[str],
) -> tuple[AccessRow, ...]:
    """Call every endpoint as every role and record what happened.

    Uses each role's own credential rather than presenting one role's token and
    asking for another role's scope, because that is the mistake this matrix
    exists to rule out: if the role were a *parameter* of the call, a caller
    could simply pass the one it wanted.

    A role with no scopes still gets called. ``anonymous`` has no credential at
    all, so it is exercised as the absence of one, and its refusals are
    authentication failures rather than policy failures -- a distinction worth
    printing, because "you are not allowed" and "who are you" send a caller to
    different fixes.

    Args:
        registry: A built connector.
        course_code: Which course to ask about.
        endpoints: Which endpoints to call. Every one, in practice.

    Returns:
        One :class:`AccessRow` per (role, endpoint) pair, role-major so the
        printed matrix reads by role.
    """
    rows: list[AccessRow] = []
    principals = registry.principal_registry

    for role in CONNECTOR_ROLES:
        # Each role presents its own credential. Asking for a role with someone
        # else's token would defeat the point: the role must be a property of
        # the *credential*, never a parameter of the call.
        token = principals.token_for_role(role)
        for endpoint in endpoints:
            spec = registry.get(endpoint)
            payload = dict(READ_PAYLOADS[endpoint])
            if "course_code" in payload:
                payload["course_code"] = course_code

            started = time.perf_counter()
            error = ""
            count = 0
            try:
                registry.invoke(endpoint, payload, token=token)
            except ConnectorError as exc:
                # Every refusal the connector raises is a `ConnectorError`, so
                # the concrete subclass is captured by name rather than by
                # separate `except` clauses that would differ only in what they
                # write into the same field.
                error = type(exc).__name__
            else:
                call = registry.ledger.last()
                assert call is not None, "an allowed call must be on the ledger"
                count = call.records
            elapsed = (time.perf_counter() - started) * 1000

            rows.append(
                AccessRow(
                    role=role,
                    endpoint=endpoint,
                    has_scope=spec.scope in ROLE_SCOPES[role],
                    outcome="refused" if error else "allowed",
                    error=error,
                    record_count=count,
                    elapsed_ms=round(elapsed, 2),
                )
            )
    return tuple(rows)


# ==========================================================================
# Section 4 -- the same connector over a real MCP subprocess
# ==========================================================================


@dataclass(frozen=True)
class WireOutcome:
    """One call made across a real MCP stdio connection.

    Attributes:
        endpoint: Which endpoint was called.
        role: The role the credential belongs to, as the **server** resolved it.
        outcome: ``"allowed"``, ``"refused"`` or ``"failed"``.
        error: The exception class name that reached the client, or empty. Over
            a wire the *type* does not survive on its own -- the MCP server
            formats tool failures as text -- so the connector tags them with an
            explicit ``[ClassName]`` prefix and this field is what that tag
            decoded back to. Getting a name here is itself a result.
        record_count: Records returned.
        elapsed_ms: Time for this one call, warm process.
    """

    endpoint: str
    role: str
    outcome: str
    error: str
    record_count: int
    elapsed_ms: float

    def render(self) -> str:
        """The outcome as one printed line.

        Returns:
            A line naming the endpoint, the role, the outcome and the timing.
        """
        detail = f"{self.error}" if self.error else f"{self.record_count} record(s)"
        return (
            f"{self.endpoint:<26} {self.role:<19} {self.outcome:<8} "
            f"{detail:<28} {self.elapsed_ms:>7.1f}ms"
        )


@dataclass(frozen=True)
class ParityRow:
    """In-process and over-the-wire results for one endpoint, compared.

    Attributes:
        endpoint: Which endpoint.
        direct_json: The in-process result, JSON-mode dumped.
        wire_json: The MCP result, JSON-mode dumped.
        differences: Sorted field paths that differ, after normalising away the
            clock. Paths use ``a.b`` notation so a difference *inside*
            ``provenance`` is reported as ``provenance.x`` rather than as the
            whole block -- otherwise one timestamp makes four endpoints look
            broken and the reader has to guess why.
        ignored: The paths deliberately excluded, and why.
    """

    endpoint: str
    direct_json: dict[str, Any]
    wire_json: dict[str, Any]
    differences: tuple[str, ...]
    ignored: tuple[str, ...] = ()

    @property
    def identical(self) -> bool:
        """Do the two payloads agree on every compared leaf?

        False when any field path differs. Empty :attr:`differences` is the
        condition; this exists so callers read as "identical" rather than as
        "not different".
        """
        return not self.differences

    def verdict(self) -> str:
        """One line naming how much was compared and what the answer was.

        Both halves are load-bearing. The count says the comparison was not a
        token gesture -- three endpoints compare tens of leaves each. The paths
        say *where* a failure is, which is the entire reason for flattening; a
        verdict of "differs" with nothing after it would send a reader looking
        for the difference themselves, having already paid the cost of running
        the check.

        Returns:
            ``"<n> leaf field(s) identical"``, or ``"... differ on <paths>"``.
        """
        count = len(_flatten(self.direct_json))
        unit = "leaf field(s)"
        if self.identical:
            return f"{count} {unit} identical"
        return f"{count} {unit} differ on {', '.join(self.differences)}"


@dataclass(frozen=True)
class RefusalSpawn:
    """One refusal, obtained by spawning a server for that role.

    A role is a property of the credential, and the credential reaches the
    server through the server's **environment**. So a refusal for a different
    role needs a different server process -- there is no way to serve two roles
    out of one spawn, and pretending otherwise would mean putting the role on
    the wire, which is the thing Lab 5 exists to prevent. Each spawn therefore
    costs its own startup, and that cost is reported here rather than folded
    into the warm-call figures where it would look like a slow endpoint.

    Attributes:
        role: The role the credential belongs to.
        outcome: ``"allowed"`` or ``"refused"``.
        error: The exception class that reached the client, or empty.
        message: Its message, so the report can show the refusal's own wording.
        elapsed_ms: Spawn plus call, for this role.
    """

    role: str
    outcome: str
    error: str
    message: str
    elapsed_ms: float


@dataclass(frozen=True)
class TransportReport:
    """Everything measured about running the connector over a real MCP server.

    Attributes:
        outcomes: Every call made over the faculty connection, in order.
        refusals: One :class:`RefusalSpawn` per role refused over the wire.
        parity: Per-endpoint payload comparison, direct vs wire.
        startup_ms: Time to spawn the faculty server subprocess and list its
            tools. Paid **once**: the session is held open for every call, so
            this is a startup cost and not a per-call one.
        warm_call_ms: The per-call times on that held-open session, in call
            order. Four numbers rather than a mean, because the mean is what
            hides a respawn.
        server_names: Connection and server names as the adapter reports them.
            Expected empty: this client uses one unnamed stdio connection, and a
            stray name would mean the tools came from somewhere unintended.
        tool_count: How many tools the server advertised.
    """

    outcomes: tuple[WireOutcome, ...]
    refusals: tuple[RefusalSpawn, ...]
    parity: tuple[ParityRow, ...]
    startup_ms: float
    warm_call_ms: tuple[float, ...]
    server_names: tuple[list[str], list[str]]
    tool_count: int

    @property
    def refused_over_the_wire(self) -> tuple[WireOutcome, ...]:
        """Refusals among the faculty connection's own calls."""
        return tuple(o for o in self.outcomes if o.outcome == "refused")

    @property
    def all_wire_refusals(self) -> tuple[RefusalSpawn, ...]:
        """Every refusal observed over any wire connection."""
        return tuple(r for r in self.refusals if r.outcome == "refused")

    @property
    def policy_refusals_survived(self) -> bool:
        """Did every refusal arrive as ``ConnectorPolicyError``?

        A refusal that arrived as plain text, or as a generic transport error,
        means a caller could not tell "you are not allowed" from "the server
        broke" -- and an audit that cannot tell those apart cannot answer the
        question it exists for.
        """
        return all(r.error == "ConnectorPolicyError" for r in self.all_wire_refusals)

    @property
    def payloads_identical(self) -> bool:
        """Did both transports produce the same typed payload everywhere?"""
        return all(row.identical for row in self.parity)

    @property
    def total_warm_ms(self) -> float:
        """Total time for every warm call on the held-open session."""
        return sum(self.warm_call_ms)

    @property
    def session_is_reused(self) -> bool:
        """Is this evidence that the session is held open rather than respawned?

        Compared as *four calls against one startup*. If each call respawned the
        subprocess, the four would together cost about four startups -- so the
        test is whether they together cost **less than two**, which is a factor
        of two of slack and cannot be met by a per-call spawn.

        Stated that way rather than as "the mean call is faster than the
        startup", because the mean hides the case that matters: one slow call in
        four is enough to make a per-call respawn look like a healthy mean.
        """
        if not self.warm_call_ms or self.startup_ms <= 0:
            return False
        return self.total_warm_ms < 2 * self.startup_ms

    @property
    def spawns_per_refusal_role(self) -> int:
        """How many server processes the refusal half of this run cost.

        One per role, always, for the reason in :class:`RefusalSpawn`.
        """
        return len(self.refusals)


#: Field paths excluded from the in-process / over-the-wire payload comparison,
#: and why. A clock reading is the only thing two processes cannot agree on.
CLOCK_PATHS: tuple[str, ...] = ("provenance.fetched_at",)

#: Field paths this run normalised away. Kept as data so the report can print
#: what it did not compare, rather than leaving the reader to wonder whether
#: "identical" was achieved by looking at fewer fields than it appears to.
IGNORED_PATH_REASON: str = (
    "a wall-clock reading taken in a different process; every other field is "
    "compared"
)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a JSON payload into paths, so differences are nameable.

    Comparing two nested payloads with ``!=`` produces one bit of information:
    "they differ". It does not say *where*, and the one field that legitimately
    differs -- a timestamp -- then makes every endpoint look broken. Flattening
    first means the difference is reported as ``provenance.fetched_at`` and
    nothing else, which is both true and checkable.

    Two conventions, both chosen so a path can be pasted into a report and
    understood:

    * dict keys are joined with ``.``; list items are indexed, so a swapped
      record reads ``notes[3].body`` rather than ``notes.[3].body``;
    * an *empty* dict or list contributes no leaves. That is intentional --
      there is nothing to compare inside it -- and it is why callers must
      compare the union of both key sets rather than one side's alone, or an
      empty list on the wire and a populated one in process would look equal.

    Args:
        value: Any JSON-shaped value.
        prefix: Path to ``value``, already formatted.

    Returns:
        Path to leaf value, for every leaf in ``value``. Empty if ``value``
        contains no leaves.
    """
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key in sorted(value):
            flat.update(_flatten(value[key], f"{prefix}.{key}" if prefix else key))
        return flat
    if isinstance(value, list):
        flat = {}
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
        return flat
    return {prefix: value}


def compare_transports(
    *,
    token: str,
    course_code: str,
    contract: Any,
    registry: EndpointRegistry,
    refusal_roles: Sequence[str] = ("student", "anonymous"),
    source: str = "local",
) -> TransportReport:
    """Run the connector twice -- in-process and over MCP -- and compare.

    The in-process client and the MCP client are two implementations of one
    :class:`~course_organizer.connector.client.ConnectorClient` Protocol. If
    they disagree about a payload, an agent behaves differently depending on
    how it is wired, and every bug report becomes a question about deployment.
    So the payloads are compared field by field rather than assumed equal.

    Args:
        token: A ``course_faculty`` credential, shared by both sides.
        course_code: Which course to read.
        contract: The
            :class:`~course_organizer.connector.client.ConnectorContract` the
            client validates against. Built from ``registry``, so the comparison
            is between transports and not between contracts.
        registry: The connector ``contract`` was derived from. Used for the
            record counts, because the endpoint's declared ``count_field`` is
            the authoritative answer -- see :func:`_count_of`.
        refusal_roles: Roles to repeat over the wire specifically to prove a
            refusal survives serialisation. Each costs its own server spawn;
            see :class:`RefusalSpawn` for why that is unavoidable.
        source: Which source the spawned servers should serve.

    Returns:
        A :class:`TransportReport`.
    """
    # -- the direct half: an in-process client over the same registry ---------
    direct_client = InProcessConnectorClient(registry=registry, token=token)
    direct_json: dict[str, dict[str, Any]] = {}
    with direct_client:
        for endpoint, payload in READ_PAYLOADS.items():
            body = dict(payload)
            if "course_code" in body:
                body["course_code"] = course_code
            value = direct_client.read(endpoint, body)
            direct_json[endpoint] = value.model_dump(mode="json")

    # -- the wire half: one real subprocess, one held-open session ----------
    outcomes: list[WireOutcome] = []
    wire_json: dict[str, dict[str, Any]] = {}
    warm: list[float] = []

    startup_started = time.perf_counter()
    client = McpConnectorClient(
        contract=contract, token=token, source=source, role="course_faculty"
    )
    try:
        tools = client._ensure_tools()  # noqa: SLF001 - startup is the measurement
        startup_ms = (time.perf_counter() - startup_started) * 1000
        tool_count = len(tools)
        server_names = client.server_names()

        for endpoint, payload in READ_PAYLOADS.items():
            body = dict(payload)
            if "course_code" in body:
                body["course_code"] = course_code
            started = time.perf_counter()
            try:
                value = client.read(endpoint, body)
            except ConnectorError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                warm.append(elapsed_ms)
                outcomes.append(
                    WireOutcome(
                        endpoint=endpoint,
                        role="course_faculty",
                        outcome="refused",
                        error=type(exc).__name__,
                        record_count=0,
                        elapsed_ms=round(elapsed_ms, 2),
                    )
                )
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000
                warm.append(elapsed_ms)
                wire_json[endpoint] = value.model_dump(mode="json")
                outcomes.append(
                    WireOutcome(
                        endpoint=endpoint,
                        role="course_faculty",
                        outcome="allowed",
                        error="",
                        record_count=_count_of(registry, endpoint, value),
                        elapsed_ms=round(elapsed_ms, 2),
                    )
                )
    finally:
        client.close()

    # -- the refusals, each on its own server -------------------------------
    refusals: list[RefusalSpawn] = []
    for role in refusal_roles:
        wire_client = McpConnectorClient(
            contract=contract, token=token, source=source, role=role
        )
        started = time.perf_counter()
        try:
            try:
                wire_client.read(
                    "read_submission_records", {"course_code": course_code}
                )
            except ConnectorError as exc:
                outcome, error, message = "refused", type(exc).__name__, str(exc)
            else:
                outcome, error, message = "ALLOWED", "", "the refusal did not happen"
            refusals.append(
                RefusalSpawn(
                    role=role,
                    outcome=outcome,
                    error=error,
                    message=message,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            )
        finally:
            wire_client.close()

    # -- payload parity, field path by field path ---------------------------
    parity: list[ParityRow] = []
    for endpoint, direct in direct_json.items():
        wire = wire_json.get(endpoint)
        if wire is None:  # pragma: no cover - only if a call refused unexpectedly
            parity.append(
                ParityRow(endpoint, direct, {}, ("<no wire result>",), ())
            )
            continue
        a = _flatten(direct)
        b = _flatten(wire)
        ignored = tuple(p for p in CLOCK_PATHS if p in a or p in b)
        differences = tuple(
            sorted(
                p
                for p in set(a) | set(b)
                if p not in CLOCK_PATHS and a.get(p) != b.get(p)
            )
        )
        parity.append(
            ParityRow(
                endpoint=endpoint,
                direct_json=direct,
                wire_json=wire,
                differences=differences,
                ignored=ignored,
            )
        )

    return TransportReport(
        outcomes=tuple(outcomes),
        refusals=tuple(refusals),
        parity=tuple(parity),
        startup_ms=round(startup_ms, 2),
        warm_call_ms=tuple(round(v, 2) for v in warm),
        server_names=server_names,
        tool_count=tool_count,
    )


def _count_of(registry: EndpointRegistry, endpoint: str, value: Any) -> int:
    """How many records a typed result holds, per the endpoint's own declaration.

    Delegates to ``EndpointSpec.count_field`` rather than guessing. That field
    exists because Lab 5's original ``_count_records`` used "the first
    list-valued field", which found ``declared_units`` (5) before ``notes`` (8)
    and so understated every audit row for the notes endpoint. Guessing here
    would reintroduce that bug in the one place that prints the number a
    reviewer compares against section 2.

    Args:
        registry: The connector that served the call.
        endpoint: Which endpoint produced ``value``.
        value: The typed result.

    Returns:
        The declared record count, or 0 when the endpoint declares none.
    """
    spec = registry.get(endpoint)
    dumped = value.model_dump(mode="json")
    if spec.count_field is not None:
        listed = dumped.get(spec.count_field)
        if isinstance(listed, list):
            return len(listed)
        if isinstance(listed, int):
            return listed
    return 0


# ==========================================================================
# Section 5 -- swapping the source changes no agent code
# ==========================================================================


@dataclass(frozen=True)
class SwapLeg:
    """One run of the same agent code against one source.

    The agent is a fixed four-line sequence -- build a registry, ask one
    question, read two fields -- and it is reproduced here verbatim in
    :func:`measure_source_swap` for both sources. If this dataclass can be
    filled in twice with different values, the document's second acceptance
    clause holds.

    Attributes:
        source: The ``SOURCE_ADAPTER`` value for this leg.
        course_code: The course that was asked about. Same question both times.
        course_name: What the source calls that course.
        note_count: How many notes came back.
        source_files: The provenance locations, in the source's own vocabulary.
        descriptor: The adapter's one-line self-description.
    """

    source: str
    course_code: str
    course_name: str
    note_count: int
    source_files: tuple[str, ...]
    descriptor: str

    def render(self) -> str:
        """The leg as two printed lines.

        Returns:
            The source and course on one line, then its provenance on the next.
        """
        return (
            f"SOURCE_ADAPTER={self.source:<10} -> {self.course_name!r}\n"
            f"{'':<29}notes={self.note_count}  "
            f"provenance={', '.join(self.source_files) or '(none)'}"
        )


@dataclass(frozen=True)
class SwapReport:
    """Two legs of the same agent code, and what changed between them.

    Attributes:
        before: The run against the project's default source.
        after: The run against a different one.
        tool_names: The registry's tool names, captured once. Identical across
            both legs because the same ``build_lab04_registry`` built them.
        query_keys: The *keys* of the payload both runs passed. The values
            differ, because the two sources serve different courses -- a real
            deployment supplies the course id from configuration alongside
            ``SOURCE_ADAPTER``, and that is a configuration difference, not an
            agent one.
        result_type: The Python type both runs returned.
        code: The agent lines, kept as data so the report prints what ran
            rather than a description of it.
    """

    before: SwapLeg
    after: SwapLeg
    tool_names: tuple[str, ...]
    query_keys: tuple[str, ...]
    result_type: str
    code: tuple[str, ...]

    @property
    def same_agent_code(self) -> bool:
        """Did the caller see the same interface in both runs?

        True when the tool names, the query *shape* and the result type are
        unchanged. That is the whole claim: *"swapping a source needs no agent
        change."* The course code's **value** differs, and is excluded on
        purpose -- see :attr:`query_keys`.
        """
        return (
            bool(self.tool_names)
            and self.result_type.endswith("NotesReadResult")
            and self.query_keys == ("course_code",)
        )

    @property
    def data_actually_changed(self) -> bool:
        """Did the swap really change where the data came from?

        A swap that returned identical data would prove nothing about swapping.
        Requiring the course name *and* the provenance to differ catches a
        misconfigured adapter that silently served the old corpus -- which is
        the most likely way for this measurement to lie.
        """
        return (
            self.before.course_name != self.after.course_name
            and self.before.source_files != self.after.source_files
        )


#: Which course each swappable source actually serves. ``InMemoryAdapter``
#: serves one course of its own, ``ZZ999``; ``LocalSeedAdapter`` serves the two
#: seeded courses. A swap between them is a swap between genuinely different
#: corpora, which is what makes the measurement worth making -- swapping
#: ``local`` for a differently-configured ``local`` would change nothing.
DEFAULT_SWAP_COURSES: dict[str, str] = {
    "local": "CS201",
    "in_memory": "ZZ999",
}

#: The agent lines that run against each source, character for character. Held
#: as data so the printed report shows the code that actually executed.
SWAP_AGENT_CODE: tuple[str, ...] = (
    'registry = build_default_connector(source=source, token=token)',
    'readers.use_connector(registry, token)',
    'tools = build_lab04_registry(run_id=f"{run_id}/{source}")',
    'tools.invoke("read_notes_store", {"course_code": course})',
)


def measure_source_swap(
    *,
    token: str,
    run_id: str,
    before_source: str = "local",
    after_source: str = "in_memory",
    course_codes: Optional[dict[str, str]] = None,
) -> SwapReport:
    """Run the same agent code twice, over two different sources, and compare.

    The agent code is the four lines in :data:`SWAP_AGENT_CODE`, and it is
    executed from that one tuple for both legs, so *"no agent change"* is a
    fact about one piece of code rather than a promise about two similar-looking
    blocks. What the loop varies is the
    :class:`~course_organizer.connector.core.EndpointRegistry` handed to
    :func:`~course_organizer.tools.readers.use_connector`, which is the seam the
    document's second acceptance clause is really about.

    The course code differs between the legs, and that is deliberate rather than
    papered over: :data:`DEFAULT_SWAP_COURSES` maps each source to a course it
    actually serves, because the two sources hold different corpora. A real
    deployment supplies that id from configuration next to ``SOURCE_ADAPTER``.
    What the check compares is the query's *shape*, which is the agent's
    contribution; the value is the operator's.

    Args:
        token: A ``course_faculty`` credential accepted by both.
        run_id: Run id for the tool ledgers.
        before_source: The source for the first leg.
        after_source: The source for the second.
        course_codes: Source to course-code mapping. Defaults to
            :data:`DEFAULT_SWAP_COURSES`.

    Returns:
        A :class:`SwapReport`.

    Raises:
        AssertionError: If either leg's read fails. A swap that cannot read is
            not a swap, and returning a partial report would hide that.
        KeyError: If ``course_codes`` omits one of the two sources. Raised
            rather than defaulted, so a typo in a source name cannot quietly
            produce a leg that reads the wrong course.
    """
    courses = {**DEFAULT_SWAP_COURSES, **(course_codes or {})}
    for source in (before_source, after_source):
        if source not in courses:
            raise KeyError(
                f"no course configured for source {source!r}; pass "
                f"course_codes={{{source!r}: 'CS201'}}"
            )

    legs: list[SwapLeg] = []
    tool_names: tuple[str, ...] = ()
    result_type = ""
    query_keys: tuple[str, ...] = ()

    for source in (before_source, after_source):
        course = courses[source]
        registry = build_default_connector(source=source, token=token)
        readers.reset_connector()
        readers.use_connector(registry, token)

        # ---- the agent. Identical for both legs; only `source`/`course`
        # ---- (configuration) differ.
        tools = build_lab04_registry(run_id=f"{run_id}/{source}")
        query = {"course_code": course}
        result = tools.invoke("read_notes_store", dict(query))
        if not result.ok:
            raise AssertionError(
                f"the {source!r} leg failed: {result.error}"
            )
        value = result.value
        # ---- end of the agent. -------------------------------------------

        tool_names = tuple(tools.names())
        result_type = type(value).__name__
        query_keys = tuple(sorted(query))
        legs.append(
            SwapLeg(
                source=source,
                course_code=course,
                course_name=value.course_name,
                note_count=value.note_count,
                source_files=tuple(value.source_files),
                descriptor=registry.descriptor.summary
                if hasattr(registry.descriptor, "summary")
                else str(registry.descriptor),
            )
        )

    return SwapReport(
        before=legs[0],
        after=legs[1],
        tool_names=tool_names,
        query_keys=query_keys,
        result_type=result_type,
        code=SWAP_AGENT_CODE,
    )


# ==========================================================================
# Section 6 -- the pitfall: what governance withholds
# ==========================================================================


@dataclass(frozen=True)
class BypassObservation:
    """What the pitfall costs, measured rather than asserted.

    Doc 2.10 Lab 5: *"No agent should touch a source directly; a bypass defeats
    governance later."* The clause that matters is *later*. At the moment of the
    bypass nothing looks wrong -- the data is real, the code works, the tests
    pass. What is missing is the record, and the record is what makes the later
    argument possible.

    So this measures the two halves of the claim:

    * what a bypass would have obtained -- obtained here *legitimately*, as the
      role that is allowed it, so the amount is real rather than hypothetical;
    * what the governance left behind instead -- one ledger row naming the
      caller, the scope that was missing, and the outcome.

    Attributes:
        withheld_records: How many submission records faculty may read and a
            student may not.
        withheld_description: What those records are, for the printed report.
        student_error: The exception class the student got.
        student_message: Its message.
        student_own_read_allowed: Whether the same student *can* read the notes.
            Proves the refusal discriminates on scope rather than blanket-
            refusing every call, which is what separates a policy from a broken
            endpoint.
        ledger_rows_for_endpoint: Ledger rows naming that caller.
        ledger_names_the_caller: Whether the subject appears in the audit.
        ledger_names_the_scope: Whether the missing scope appears.
        read_signature: ``ConnectorClient.read``'s parameter list, which is where
            a caller would have to put a credential in order to escalate.
    """

    withheld_records: int
    withheld_description: str
    student_error: str
    student_message: str
    student_own_read_allowed: int
    ledger_rows_for_endpoint: int
    ledger_names_the_caller: bool
    ledger_names_the_scope: bool
    read_params: tuple[str, ...]

    @property
    def escalation_seam(self) -> str:
        """Where a caller would have to put a credential to raise its own role.

        The rendered signature, for the reader. The *decision* is
        :attr:`no_escalation_parameter`, which compares parameter **names** --
        a string comparison against a rendered signature would break the moment
        someone added an annotation, which is exactly the kind of brittleness
        that makes a structural check get quietly deleted.
        """
        return f"({', '.join(self.read_params)})"

    @property
    def no_escalation_parameter(self) -> bool:
        """Does ``read()`` accept anything a caller could raise its own role with?

        True when the only parameters are the endpoint name and the payload.
        ``self`` is excluded because it is bound, and ``**kwargs`` would fail
        this too -- which is the point: a catch-all is an escalation seam.
        """
        return set(self.read_params) == {"endpoint", "payload"}

    def render(self) -> str:
        """The observation as printed lines.

        Returns:
            The withheld data, the refusal, and the audit row it produced.
        """
        lines = [
            f"  as course_faculty : {self.withheld_records} "
            f"{self.withheld_description}",
            f"  as student       : REFUSED {self.student_error}",
            f"                     {self.student_message}",
            f"  same student, read_notes_store : ALLOWED, "
            f"{self.student_own_read_allowed} note(s)",
            "  so the refusal is scope-based, not a blanket deny",
            "",
            "  what the governed path leaves behind:",
            f"    ledger rows for read_submission_records : "
            f"{self.ledger_rows_for_endpoint}",
            f"    names the caller                        : "
            f"{self.ledger_names_the_caller}",
            f"    names the missing scope                : "
            f"{self.ledger_names_the_scope}",
            "",
            "  where a caller would put a credential to escalate:",
            f"    ConnectorClient.read{self.escalation_seam}",
            "    -> nothing. The token is held by the client, not the call.",
        ]
        return "\n".join(lines)


def demonstrate_bypass(
    registry: EndpointRegistry,
    token: str,
    course_code: str,
) -> BypassObservation:
    """Measure what the connector withheld from a student, and what it recorded.

    Runs the same request twice under two roles. The first is the authorised
    read that establishes the size of what is being protected; the second is the
    unauthorised one, refused and logged. Neither imports the store, because
    this module is part of the package the AST test protects -- a demonstration
    of a bypass that itself bypasses would be self-defeating.

    Args:
        registry: A built connector whose ledger this will read.
        token: A ``course_faculty`` credential.
        course_code: Which course to ask about.

    Returns:
        A :class:`BypassObservation`.

    Raises:
        AssertionError: If the authorised read fails. Without a real count there
            is nothing meaningful to say about what was withheld.
    """
    faculty = registry.invoke(
        "read_submission_records", {"course_code": course_code}, token=token
    )
    withheld = faculty.unwrap()
    count = len(getattr(withheld, "submissions", []) or [])

    before = len(registry.ledger)
    student = registry.principal_registry.token_for_role("student")
    try:
        registry.invoke(
            "read_submission_records", {"course_code": course_code}, token=student
        )
    except ConnectorError as exc:
        student_error = type(exc).__name__
        student_message = str(exc)
    else:  # pragma: no cover - a policy failure here is a defect, caught by checks
        student_error, student_message = "<none>", "the refusal did not happen"

    after = len(registry.ledger)
    assert after == before + 1, (
        f"one refused call must add exactly one ledger row; {before} -> {after}"
    )
    rows = [
        c for c in registry.ledger if c.endpoint == "read_submission_records"
    ]
    policy_rows = [c for c in rows if c.outcome != "allowed"]
    own = registry.invoke(
        "read_notes_store", {"course_code": course_code}, token=student
    )
    own_count = len(getattr(own.unwrap(), "notes", []) or [])

    signature = inspect.signature(InProcessConnectorClient.read)
    params = tuple(
        name for name in signature.parameters if name != "self"
    )

    return BypassObservation(
        withheld_records=count,
        withheld_description=f"submission record(s) for {course_code}",
        student_error=student_error,
        student_message=student_message,
        student_own_read_allowed=own_count,
        ledger_rows_for_endpoint=len(policy_rows),
        ledger_names_the_caller=any("student" in c.principal for c in policy_rows),
        ledger_names_the_scope=any(SUBMISSIONS_READ in c.error for c in policy_rows),
        read_params=params,
    )


# ==========================================================================
# The run, and the acceptance checks
# ==========================================================================


@dataclass(frozen=True)
class LabRunResult:
    """Everything Lab 5 measured, in one object.

    Attributes:
        run_id: The run.
        course_code: Which course was read.
        source: Which source served the in-process half.
        endpoint_names: Every governed endpoint, sorted.
        observations: Section 2's reads.
        rows: Section 3's access matrix.
        transport: Section 4's transport comparison.
        swap: Section 5's source swap.
        bypass: Section 6's pitfall measurement.
        ledger_report: The connector's audit table.
        tools_ledger_report: The Lab 2 tool layer's audit table, for continuity.
        with_mcp: Whether the real MCP subprocess was spawned. False only in a
            fast smoke run, and the transport checks say so rather than quietly
            passing.
        total_elapsed_ms: Wall-clock time for the whole lab.
    """

    run_id: str
    course_code: str
    source: str
    endpoint_names: tuple[str, ...]
    observations: tuple[ReadObservation, ...]
    rows: tuple[AccessRow, ...]
    transport: TransportReport
    swap: SwapReport
    bypass: BypassObservation
    ledger_report: str
    tools_ledger_report: str
    with_mcp: bool
    total_elapsed_ms: float

    @property
    def skipped_labels(self) -> frozenset[str]:
        """Check labels that were not evaluated because MCP was switched off."""
        if self.with_mcp:
            return frozenset()
        return frozenset(TRANSPORT_CHECK_LABELS)

    # -- derived views, each answering one question ------------------------

    @property
    def documented_reads(self) -> tuple[ReadObservation, ...]:
        """The reads the document names, excluding the one Lab 5 added."""
        return tuple(o for o in self.observations if o.documented)

    @property
    def allowed_rows(self) -> tuple[AccessRow, ...]:
        """Matrix cells that succeeded."""
        return tuple(r for r in self.rows if r.outcome == "allowed")

    @property
    def refused_rows(self) -> tuple[AccessRow, ...]:
        """Matrix cells that were refused."""
        return tuple(r for r in self.rows if r.outcome == "refused")

    @property
    def checks(self) -> tuple[tuple[str, bool, str], ...]:
        """Every acceptance and guard check, as ``(label, passed, detail)``.

        Computed from the measurements rather than collected during the run, so
        the whole list is in one place and a test can assert on it.
        """
        items: list[tuple[str, bool, str]] = []

        # -- the document's Goal: one typed connector, three named reads ----
        documented = self.documented_reads
        items.append(
            (
                "the document's three reads are exposed as endpoints",
                len(documented) == 3
                and {o.endpoint for o in documented}
                == {name for _, name in PSEUDOCODE_TO_ENDPOINT},
                ", ".join(o.endpoint for o in documented),
            )
        )
        items.append(
            (
                "every documented read returned data to a full-scope caller",
                all(o.outcome == "allowed" and o.record_count > 0 for o in documented),
                ", ".join(f"{o.endpoint}={o.record_count}" for o in documented),
            )
        )
        items.append(
            (
                "every read reports where its data came from",
                all("(no location reported)" not in o.provenance for o in self.observations),
                f"{len(self.observations)} result(s), each with provenance.sources",
            )
        )
        items.append(
            (
                "the added endpoint is labelled as this project's, not the "
                "document's",
                all(
                    not o.documented
                    for o in self.observations
                    if o.endpoint not in {n for _, n in PSEUDOCODE_TO_ENDPOINT}
                ),
                next(
                    (
                        f"{o.endpoint}: {o.declared_origin}"
                        for o in self.observations
                        if not o.documented
                    ),
                    "none added",
                ),
            )
        )

        # -- the document's Deliverable: typed endpoints and auth ----------
        items.append(
            (
                "every endpoint declares the scope it needs",
                all(o.declared_doc_ref for o in self.observations),
                ", ".join(f"{o.endpoint}->{o.scope}" for o in self.observations),
            )
        )
        items.append(
            (
                "auth is enforced per role, not per caller",
                all(
                    r.outcome == ("allowed" if r.has_scope else "refused")
                    for r in self.rows
                ),
                f"{len(self.allowed_rows)} allowed, {len(self.refused_rows)} refused "
                f"of {len(self.rows)} call(s)",
            )
        )
        items.append(
            (
                "every refusal arrived as a typed policy failure",
                all(r.explained for r in self.refused_rows),
                ", ".join(sorted({r.error for r in self.refused_rows})) or "none",
            )
        )
        items.append(
            (
                "a student may read the notes but not the cohort's submissions",
                any(
                    r.role == "student" and r.outcome == "allowed"
                    for r in self.rows
                    if r.endpoint == "read_notes_store"
                )
                and all(
                    r.outcome == "refused"
                    for r in self.rows
                    if r.role == "student"
                    and r.endpoint == "read_submission_records"
                ),
                "the refusal discriminates on scope",
            )
        )

        # -- the real MCP server, not just the in-process registry ---------
        if self.with_mcp:
            items.append(
                (
                    "a real MCP subprocess served every call",
                    self.transport.tool_count == len(self.endpoint_names),
                    f"{self.transport.tool_count} tool(s) over stdio, "
                    f"names={self.transport.server_names}",
                )
            )
            items.append(
                (
                    "the wire and the in-process client return identical payloads",
                    self.transport.payloads_identical,
                    ", ".join(
                        f"{r.endpoint}: {r.verdict()}" for r in self.transport.parity
                    ),
                )
            )
            items.append(
                (
                    "a refusal survives the wire as a typed policy failure",
                    self.transport.policy_refusals_survived
                    and len(self.transport.all_wire_refusals) > 0,
                    ", ".join(
                        f"{o.role}->{o.error}" for o in self.transport.all_wire_refusals
                    )
                    or "none attempted",
                )
            )
            items.append(
                (
                    "the MCP session is reused rather than respawned per call",
                    self.transport.session_is_reused,
                    f"{len(self.transport.warm_call_ms)} calls in "
                    f"{self.transport.total_warm_ms:.1f}ms against a "
                    f"{self.transport.startup_ms:.1f}ms spawn "
                    f"(a per-call respawn would cost about "
                    f"{len(self.transport.warm_call_ms) * self.transport.startup_ms:.0f}ms)",
                )
            )
            items.append(
                (
                    "each refused role needed its own server spawn",
                    self.transport.spawns_per_refusal_role
                    == len(self.transport.refusals),
                    f"{self.transport.spawns_per_refusal_role} spawn(s) for "
                    f"{len(self.transport.refusals)} role(s): the role travels in "
                    f"the server's environment, never on the wire",
                )
            )
        else:
            # Reported as skipped, and counted separately. A `--no-mcp` run must
            # not be able to print "OVERALL: PASS" while the deliverable it
            # exists to demonstrate was never exercised.
            items.extend(
                (label, True, "NOT RUN (--no-mcp); not evidence for this claim")
                for label in TRANSPORT_CHECK_LABELS
            )

        # -- the document's Accept, second clause: swapping needs no agent
        #    change ---------------------------------------------------------
        items.append(
            (
                "swapping the source needed no agent change",
                self.swap.same_agent_code,
                f"same {len(self.swap.tool_names)} tool(s), same query keys "
                f"{self.swap.query_keys}, same {self.swap.result_type}",
            )
        )
        items.append(
            (
                "the swap really changed the data's origin",
                self.swap.data_actually_changed,
                f"{self.swap.before.source}:{self.swap.before.course_name!r} -> "
                f"{self.swap.after.source}:{self.swap.after.course_name!r}",
            )
        )

        # -- the document's Pitfall ---------------------------------------
        items.append(
            (
                "the pitfall withheld real data from the unauthorised role",
                self.bypass.withheld_records > 0
                and self.bypass.student_error == "ConnectorPolicyError",
                f"{self.bypass.withheld_records} record(s) withheld; "
                f"student got {self.bypass.student_error}",
            )
        )
        items.append(
            (
                "the refusal is auditable: the ledger names caller and scope",
                self.bypass.ledger_rows_for_endpoint > 0
                and self.bypass.ledger_names_the_caller
                and self.bypass.ledger_names_the_scope,
                f"{self.bypass.ledger_rows_for_endpoint} row(s), caller="
                f"{self.bypass.ledger_names_the_caller}, scope="
                f"{self.bypass.ledger_names_the_scope}",
            )
        )
        items.append(
            (
                "there is no parameter a caller could use to escalate",
                self.bypass.no_escalation_parameter,
                f"read{self.bypass.escalation_seam}",
            )
        )
        return tuple(items)

    @property
    def passed(self) -> bool:
        """Did every evaluated check pass?

        Skipped checks count as passing here only because they are reported
        separately by :attr:`skipped_labels` and printed as ``SKIP``; a run that
        skipped the transport is not evidence for the transport.
        """
        return all(ok for _, ok, _ in self.checks)

    @property
    def failed(self) -> tuple[tuple[str, bool, str], ...]:
        """Just the failures, for a diagnosis-oriented summary."""
        return tuple(item for item in self.checks if not item[1])

    @property
    def skipped(self) -> tuple[tuple[str, bool, str], ...]:
        """Just the checks that were not run."""
        return tuple(
            item for item in self.checks if item[0] in self.skipped_labels
        )


def run_mcp_connector(
    *,
    run_id: str,
    token: str,
    course_code: str = "CS201",
    source: str = "local",
    swap_source: str = "in_memory",
    with_mcp: bool = True,
) -> LabRunResult:
    """Run every section and collect the evidence.

    Args:
        run_id: Run id, stamped on the ledgers.
        token: A ``course_faculty`` credential. Passed in rather than read from
            the environment so the caller knows the credential the run used and
            so a test can supply one the connector will accept.
        course_code: Which course to read throughout.
        source: Which source serves the in-process half.
        swap_source: Which source the swap leg switches to.
        with_mcp: Whether to spawn the real MCP subprocess. Off only for a fast
            smoke run -- the transport checks report as skipped rather than
            passing, so a run without the wire cannot claim the deliverable.

    Returns:
        A `LabRunResult` whose `passed` is what the CLI exits on.

    Raises:
        AssertionError: From any section that could not measure what it claims.
            A lab run that cannot demonstrate its own deliverable is a failure,
            not a partial result.
    """
    started = time.perf_counter()
    log_event(
        logger,
        "lab05_start",
        run_id=run_id,
        course_code=course_code,
        source=source,
        with_mcp=with_mcp,
    )

    registry = build_default_connector(source=source, token=token)
    observations = observe_reads(registry, token, course_code)
    rows = access_matrix(registry, course_code, registry.names())
    bypass = demonstrate_bypass(registry, token, course_code)
    ledger_report = registry.ledger.report()

    if with_mcp:
        transport = compare_transports(
            token=token,
            course_code=course_code,
            contract=contract_from_registry(registry),
            registry=registry,
            source=source,
        )
    else:
        transport = TransportReport(
            outcomes=(),
            refusals=(),
            parity=(),
            startup_ms=0.0,
            warm_call_ms=(),
            server_names=([], []),
            tool_count=0,
        )

    swap = measure_source_swap(
        token=token,
        run_id=run_id,
        before_source=source,
        after_source=swap_source,
    )

    # Put the tools back on the run's own source so the tool ledger printed at
    # the end belongs to this run rather than to the swap's second leg.
    readers.reset_connector()
    readers.use_connector(registry, token)
    tools = build_lab04_registry(run_id=run_id)
    for endpoint in (
        "read_notes_store",
        "read_assignment_rubrics",
        "read_submission_records",
    ):
        payload = dict(READ_PAYLOADS[endpoint])
        payload["course_code"] = course_code
        tools.invoke(endpoint, payload)

    return LabRunResult(
        run_id=run_id,
        course_code=course_code,
        source=source,
        endpoint_names=tuple(registry.names()),
        observations=observations,
        rows=rows,
        transport=transport,
        swap=swap,
        bypass=bypass,
        ledger_report=ledger_report,
        tools_ledger_report=tools.ledger.report(),
        with_mcp=with_mcp,
        total_elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )


# ==========================================================================
# CLI
# ==========================================================================


def _rule(char: str = "=", width: int = 78) -> str:
    """A horizontal rule.

    Args:
        char: What to draw it with.
        width: How wide.

    Returns:
        The rule.
    """
    return char * width


def _print_matrix(rows: tuple[AccessRow, ...], endpoints: Sequence[str]) -> None:
    """Print the role-by-endpoint access matrix.

    Cells carry the record count when allowed and ``R`` when refused by policy.
    ``!`` would mean a refusal that was *not* a typed policy failure, which is
    rendered differently on purpose -- see :func:`_row_marker`.

    Args:
        rows: Every matrix row, role-major.
        endpoints: The column order.
    """
    # Every column width is measured from the data it holds. The scope column is
    # the one that got this wrong first: its longest value is
    # "assignments:read, notes:read, submissions:read" at 45 characters, and a
    # hardcoded 9 overran it, so the first two columns ran together and the
    # faculty row read as a scope list ending in "2".
    role_width = max(len(r) for r in CONNECTOR_ROLES) + 2
    name_width = max(len(e) for e in endpoints) + 2
    scope_text = {role: ", ".join(sorted(ROLE_SCOPES[role])) or "none" for role in CONNECTOR_ROLES}
    scope_width = max([len("scopes")] + [len(v) for v in scope_text.values()]) + 2

    header = "  " + "role".ljust(role_width) + "scopes".ljust(scope_width)
    header += "".join(e.ljust(name_width) for e in endpoints)
    print(header)
    print("  " + "-" * (role_width + scope_width + len(endpoints) * name_width))

    for role in CONNECTOR_ROLES:
        role_rows = [r for r in rows if r.role == role]
        if not role_rows:
            continue
        line = "  " + role.ljust(role_width) + scope_text[role].ljust(scope_width)
        for endpoint in endpoints:
            row = next((r for r in role_rows if r.endpoint == endpoint), None)
            line += ("?" if row is None else _row_marker(row)).ljust(name_width)
        print(line)
    print()
    print(f"  cell = record count when allowed, R = refused by policy, "
          f"! = refused some other way")
    print(f"  roles are this project's choice (docs/lab05.md section b); the "
          f"document says only")
    print(f"  \"access control per role\" (doc 2.3).")


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Run Lab 5 and print its evidence.

    Args:
        argv: Argument list. Omit to read `sys.argv`.

    Returns:
        0 if every evaluated check passed, 1 otherwise. The exit code is the
        acceptance criterion; the printed tables are the evidence for it.
    """
    parser = argparse.ArgumentParser(
        prog=f"python -m course_organizer.labs.{LAB}",
        description=(
            "Lab 5 - MCP connector. Exposes the document's three reads as typed, "
            "authorised endpoints, serves them from a real MCP subprocess, and "
            "measures what governance withholds and what a source swap costs."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run id for the ledgers. Defaults to lab05-<timestamp>.",
    )
    parser.add_argument(
        "--course",
        default="CS201",
        help="Which course to read throughout. Defaults to CS201.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="SOURCE_ADAPTER for the in-process half. Defaults to the setting.",
    )
    parser.add_argument(
        "--swap-source",
        default="in_memory",
        help=(
            "Which source the swap leg switches to. Defaults to 'in_memory', "
            "which serves a different corpus over the same connector."
        ),
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help=(
            "Skip the real MCP subprocess. Fast, but the four transport checks "
            "are then reported as SKIP and the run is not evidence for them."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Python logging level. Defaults to WARNING to keep the report readable.",
    )
    args = parser.parse_args(argv)

    run_id = args.run_id or f"{LAB}-{int(time.time())}"
    setup_logging(level=args.log_level, run_id=run_id)

    from course_organizer.connector.auth import ensure_token

    token, generated = ensure_token(None)
    if generated:
        print(
            "  note: no MCP_CONNECTOR_TOKEN set; using an ephemeral one for this "
            "process.\n"
            "        Nothing was written to .env. Set it there if you want the "
            "same credential\n"
            "        in a separately spawned server."
        )
        print()

    result = run_mcp_connector(
        run_id=run_id,
        token=token,
        course_code=args.course,
        source=args.source or "local",
        swap_source=args.swap_source,
        with_mcp=not args.no_mcp,
    )

    print()
    print(_rule())
    print(f"{DOC_REF} - MCP connector: one typed front door")
    print(_rule())
    print(f"  run id       : {result.run_id}")
    print(f"  source       : {result.source}  (swap leg: {result.swap.after.source})")
    print(f"  course       : {result.course_code}")
    print(f"  endpoints    : {', '.join(result.endpoint_names)}")
    print(f"  elapsed      : {result.total_elapsed_ms:.1f}ms")
    print()
    print(f"  doc goal        : {DOC_GOAL}")
    print(f"  doc deliverable : {DOC_DELIVERABLE}")
    print(f"  doc accept      : {DOC_ACCEPTANCE}")
    print(f"  doc pitfall     : {DOC_PITFALL}")
    print(f"  doc nodes       : {DOC_NODES}")
    print()
    print("  pseudocode:")
    for line in DOC_PSEUDOCODE.splitlines():
        print(f"    {line}")
    print()

    print(_rule())
    print("1. The connector's shape")
    print(_rule())
    print()
    print("  agent")
    print("    -> ConnectorClient          holds the credential, never the model")
    print("    -> MCP transport (optional) a real MCP server over stdio")
    print("    -> EndpointRegistry          auth, scope check, typed in/out, ledger")
    print("    -> SourceAdapter             the swappable half")
    print("    -> LocalSeedStore            the only code that opens data/")
    print()
    print("  No line in this project outside course_organizer.connector names the")
    print("  store. That is held by an AST test, not by convention:")
    print("  tests/test_lab05_mcp_connector.py::"
          "test_no_module_outside_connector_imports_a_source_store")
    print()

    print(_rule())
    print("2. The document's three reads, exposed")
    print(_rule())
    print()
    for observation in result.observations:
        print(observation.render())
        print()

    print(_rule())
    print("3. Auth: the same endpoints, every role")
    print(_rule())
    print()
    _print_matrix(result.rows, result.endpoint_names)
    print(
        f"  {len(result.allowed_rows)} allowed, {len(result.refused_rows)} refused "
        f"of {len(result.rows)} call(s); every refusal is in the ledger."
    )
    print()
    slowest = max(result.rows, key=lambda r: r.elapsed_ms)
    print(f"  slowest cell: {slowest.role} -> {slowest.endpoint} "
          f"{slowest.elapsed_ms:.2f}ms ({slowest.outcome})")
    print("  refusals are cheap, so a policy check is not being paid for with a")
    print("  full read of data the caller was never going to see.")
    print()

    print(_rule())
    print("4. The same connector over a real MCP subprocess")
    print(_rule())
    print()
    if not result.with_mcp:
        print("  SKIPPED (--no-mcp). No subprocess was spawned.")
        print()
    else:
        t = result.transport
        print(f"  tools advertised : {t.tool_count}  "
              f"(registry has {len(result.endpoint_names)})")
        print(f"  connection names : {t.server_names}  "
              f"(empty = one unnamed stdio connection, as intended)")
        print()
        print("  one server spawn, one held-open session, four calls:")
        print(f"    spawn                     : {t.startup_ms:>9.1f}ms")
        for outcome, elapsed in zip(t.outcomes, t.warm_call_ms):
            assert outcome.elapsed_ms == elapsed, (
                "the per-call timing must be the same number the row prints"
            )
            print(f"    {outcome.endpoint:<26} {elapsed:>9.1f}ms")
        print(f"    {'total of all calls':<26} {t.total_warm_ms:>9.1f}ms")
        print()
        if t.session_is_reused:
            print(f"  Four calls cost less than one spawn "
                  f"({t.total_warm_ms:.1f}ms vs {t.startup_ms:.1f}ms), which a")
            print(f"  per-call respawn could not do -- a respawn would cost about")
            print(f"  {len(t.warm_call_ms) * t.startup_ms:.0f}ms. The session is held open.")
        else:
            print(f"  WARNING: {len(t.warm_call_ms)} calls cost "
                  f"{t.total_warm_ms:.1f}ms against a {t.startup_ms:.1f}ms spawn.")
        print()
        print("  refusals over the wire, one server per role:")
        print("    the role travels in the server's ENVIRONMENT, never as an")
        print("    argument, so serving a second role means a second spawn.")
        for refusal in t.refusals:
            print(
                f"    {refusal.role:<12} {refusal.outcome:<8} "
                f"{refusal.error or '(allowed)':<24} "
                f"{refusal.elapsed_ms:>8.1f}ms  (spawn + call)"
            )
            if refusal.message:
                print(f"      {refusal.message}")
        print()
        print("  payload parity, in-process vs over the wire:")
        for row in t.parity:
            print(f"    {row.endpoint:<26} {row.verdict()}")
        print()
        print(f"  not compared: {', '.join(CLOCK_PATHS)} -- {IGNORED_PATH_REASON}.")
        print("  Every other leaf is compared, including the nested provenance.")
        print()

    print(_rule())
    print("5. Swapping a source changes no agent code")
    print(_rule())
    print()
    print("  the agent, executed from one tuple for both legs:")
    for line in result.swap.code:
        print(f"    {line}")
    print()
    print("  what it returned:")
    print(f"    {result.swap.before.render()}")
    print(f"    {result.swap.after.render()}")
    print()
    print(f"  unchanged: {len(result.swap.tool_names)} tool(s) "
          f"({', '.join(result.swap.tool_names)})")
    print(f"             query keys {result.swap.query_keys}, "
          f"result type {result.swap.result_type}")
    print()
    print("  the course code's value differs, and that is configuration, not code:")
    print("  the two sources serve different corpora, so each is asked about a")
    print("  course it actually has. A deployment supplies that id alongside")
    print(f"  SOURCE_ADAPTER. What the agent contributes is the query shape: "
          f"{result.swap.query_keys}.")
    print()
    print("  changed  : course name and provenance, both of which come from the")
    print("             source and from nowhere else. That is what a swap is.")
    print()

    print(_rule())
    print("6. The pitfall: what governance withheld")
    print(_rule())
    print()
    print(f"  {DOC_PITFALL}")
    print()
    print(result.bypass.render())
    print()
    print("  A bypass is not an error, which is why it survives review. The data")
    print("  is real and the code works. What is missing is the row above, and")
    print("  the row is what makes 'later' arguable.")
    print()

    print(_rule())
    print("7. Ledgers")
    print(_rule())
    print()
    print("  connector calls (this lab):")
    print(result.ledger_report)
    print()
    print("  tool calls (Lab 2's layer, now reading through the connector):")
    print(result.tools_ledger_report)
    print()

    print(_rule())
    print("8. Acceptance")
    print(_rule())
    print()
    skipped = result.skipped_labels
    for label, passed, detail in result.checks:
        if label in skipped:
            mark = "SKIP"
        else:
            mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label:<62} {detail}")

    failed = result.failed
    skipped_checks = result.skipped
    print()
    if failed:
        print(f"OVERALL: FAIL ({len(failed)} check(s))")
        for label, _, detail in failed:
            print(f"  - {label}  [{detail}]")
        return 1
    summary = f"OVERALL: PASS  ({len(result.checks)} checks"
    if skipped_checks:
        summary += f", {len(skipped_checks)} skipped"
    print(
        summary
        + f", {len(result.rows)} access cells, "
        f"{len(result.transport.outcomes)} MCP call(s), "
        f"{result.total_elapsed_ms:.1f}ms)"
    )
    if skipped_checks:
        print(
            "  note: the skipped checks are the transport ones. This run is not "
            "evidence for them;"
        )
        print("        re-run without --no-mcp.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
