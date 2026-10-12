"""Lab 5's connector: one typed, authorised front door for every data source.

Doc 2.10 Lab 5, in full:

    **Goal.** Expose every data source through one typed connector.

    **Deliverable.** A connector server with typed endpoints and auth; agents
    call only through it.

    **Accept.** All data access goes through the connector; swapping a source
    needs no agent change.

    **Pitfall.** No agent should touch a source directly; a bypass defeats
    governance later.

The layering this package enforces::

    agent
      -> ConnectorClient            <- holds the credential, never the model
      -> MCP transport (optional)   <- a real MCP server over stdio
      -> EndpointRegistry            <- auth, scope check, typed in/out, ledger
      -> SourceAdapter               <- the swappable half
      -> LocalSeedStore              <- the only code that opens data/

Three claims in that diagram are checkable rather than aspirational, and each
has a test:

*No agent touches a source directly* -- the AST test in
``tests/test_lab05_mcp_connector.py`` asserts that only this package imports
``course_organizer.tools.seed_store``. A bypass would have to be written here.

*Every call is typed* -- :meth:`EndpointRegistry.invoke` validates arguments
and results against declared Pydantic models. A result that fails its contract
raises :class:`~course_organizer.connector.errors.ConnectorContractError`
instead of reaching an agent.

*Auth is not the model's to choose* -- no endpoint has a credential parameter,
the client attaches the token itself, and
``tests/test_lab05_mcp_connector.py`` asserts no LLM-visible text in the whole
package contains a token.

Typical use::

    from course_organizer.connector import build_default_connector

    connector = build_default_connector()
    token = connector.principal_registry.token_for("course_faculty")
    result = connector.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=token
    )
    print(result.unwrap().describe())
    print(connector.ledger.report())
"""

from __future__ import annotations

from typing import Any, Optional

from course_organizer.connector.adapters import (
    GOOGLE_READ_SCOPES,
    GoogleWorkspaceAdapter,
    GoogleWorkspaceTransport,
    InMemoryAdapter,
    LocalSeedAdapter,
    RestGoogleWorkspaceTransport,
    build_source_adapter,
)
from course_organizer.connector.auth import (
    ANONYMOUS_SCOPES,
    ASSIGNMENTS_READ,
    CONNECTOR_ROLES,
    KNOWN_SCOPES,
    NOTES_READ,
    ROLE_SCOPES,
    SUBMISSIONS_READ,
    Principal,
    PrincipalRegistry,
    default_principals,
    ensure_token,
)
from course_organizer.connector.core import (
    ConnectorCall,
    ConnectorLedger,
    ConnectorResult,
    EndpointRegistry,
    EndpointSpec,
    Outcomes,
)
from course_organizer.connector.endpoints import (
    AssignmentQuery,
    AssignmentResult,
    CourseListResult,
    NotesQuery,
    NotesResult,
    Provenance,
    SubmissionQuery,
    SubmissionResult,
    register_default_endpoints,
)
from course_organizer.connector.errors import (
    ConnectorAuthError,
    ConnectorContractError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
    ConnectorTransportError,
    SourceUnavailableError,
)
from course_organizer.connector.port import (
    SourceAdapter,
    SourceDescriptor,
    SourceHealth,
)

__all__ = [
    # -- the document's own three reads, plus the one Lab 5 added ------------
    "AssignmentQuery",
    "AssignmentResult",
    "CourseListResult",
    "NotesQuery",
    "NotesResult",
    "SubmissionQuery",
    "SubmissionResult",
    # -- provenance ----------------------------------------------------------
    "Provenance",
    # -- the swappable layer --------------------------------------------------
    "GOOGLE_READ_SCOPES",
    "GoogleWorkspaceAdapter",
    "GoogleWorkspaceTransport",
    "InMemoryAdapter",
    "LocalSeedAdapter",
    "RestGoogleWorkspaceTransport",
    "SourceAdapter",
    "SourceDescriptor",
    "SourceHealth",
    "build_source_adapter",
    # -- auth ----------------------------------------------------------------
    "ANONYMOUS_SCOPES",
    "ASSIGNMENTS_READ",
    "CONNECTOR_ROLES",
    "KNOWN_SCOPES",
    "NOTES_READ",
    "Principal",
    "PrincipalRegistry",
    "ROLE_SCOPES",
    "SUBMISSIONS_READ",
    "default_principals",
    "ensure_token",
    # -- the governed surface -------------------------------------------------
    "ConnectorCall",
    "ConnectorLedger",
    "ConnectorResult",
    "EndpointRegistry",
    "EndpointSpec",
    "Outcomes",
    "register_default_endpoints",
    # -- errors ---------------------------------------------------------------
    "ConnectorAuthError",
    "ConnectorContractError",
    "ConnectorError",
    "ConnectorNotFoundError",
    "ConnectorPolicyError",
    "ConnectorTransportError",
    "SourceUnavailableError",
    # -- the factory ----------------------------------------------------------
    "DEFAULT_ENDPOINTS",
    "build_default_connector",
    "build_default_principals",
]

#: The endpoint names the document's three reads produce, plus the one Lab 5
#: added. Asserted by the tests, so adding an endpoint without deciding
#: whether it is documented is a test failure rather than a silent expansion
#: of the governed surface.
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "list_courses",
    "read_assignment_rubrics",
    "read_notes_store",
    "read_submission_records",
)


def build_default_principals(
    *,
    token: Optional[str] = None,
    role: Optional[str] = None,
    settings: Any = None,
) -> PrincipalRegistry:
    """Build the development principal registry.

    Args:
        token: If given, this becomes the presented principal's token and is
            ignored for every other role. Used so the MCP server process and
            the client process can share one known credential; see
            :func:`course_organizer.connector.auth.default_principals`.
        role: Which role the presented ``token`` belongs to. Defaults to
            ``course_faculty``, the only role holding every scope. This is the
            MCP server's ``--role``: the role of a credential is decided by
            whoever issues it, not by the caller presenting it.
        settings: Read for ``mcp_connector_token`` when ``token`` is not
            given.

    Returns:
        A registry with one principal per role in :data:`CONNECTOR_ROLES`.

    Raises:
        ConnectorPolicyError: If ``role`` is not a known role. Validated here
            rather than trusted, so an unknown role fails before a registry
            exists rather than at the first call.
    """
    if token is None:
        if settings is None:
            from course_organizer.config import get_settings

            settings = get_settings()
        token, _generated = ensure_token(
            getattr(settings, "mcp_connector_token", None)
        )
    if role is None and settings is not None:
        role = getattr(settings, "mcp_connector_role", None)
    return PrincipalRegistry(default_principals(token, role=role))


def build_default_connector(
    *,
    source: Optional[str] = None,
    seed_dir: Optional[str] = None,
    token: Optional[str] = None,
    settings: Any = None,
    transport: Optional[GoogleWorkspaceTransport] = None,
    principals: Optional[PrincipalRegistry] = None,
    ledger: Optional[ConnectorLedger] = None,
    connector_transport: str = "in_process",
) -> EndpointRegistry:
    """Build a fully-wired connector: source, principals, endpoints, ledger.

    This is the single entry point for every Lab 5 demonstration and every
    test. Nothing else in the project assembles a connector, which is what
    makes "agents call only through it" a property of the code rather than a
    habit.

    Args:
        source: ``"local"``, ``"in_memory"`` or ``"google"``. Defaults to
            ``Settings.source_adapter``, which is ``"local"``.
        seed_dir: Overrides the seed root, for tests.
        token: The bearer token to seed the principal registry with, when this
            connector is not given a ``principals`` registry of its own. A test
            that builds a connector over a temporary corpus and then calls it
            needs to know the credential it will be handed; making it choose one
            is more honest than having it read ``.env`` and hope the two agree.
        settings: A :class:`course_organizer.config.Settings`. Read when
            ``source`` is not given and when the Google adapter needs it.
        transport: An injected Google transport, for testing that adapter
            without a tenant.
        principals: An existing principal registry, to share one log across
            two connectors.
        ledger: An existing ledger, to share one log across two connectors.
        connector_transport: Recorded on every ledger row, so a call that
            arrived over a spawned MCP process is distinguishable from a
            direct in-process call.

    Returns:
        A registry with :data:`DEFAULT_ENDPOINTS` registered.

    Raises:
        SourceUnavailableError: If the selected source cannot be built. The
            message names the setting to change.
    """
    if settings is None and (source is None or source == "google"):
        from course_organizer.config import get_settings

        settings = get_settings()
    if source is None:
        source = str(getattr(settings, "source_adapter", "local"))

    adapter = build_source_adapter(
        source, settings=settings, seed_dir=seed_dir, transport=transport
    )
    if principals is None:
        principals = build_default_principals(token=token, settings=settings)

    registry = EndpointRegistry(
        adapter=adapter,
        principals=principals,
        ledger=ledger,
        transport=connector_transport,
    )
    return register_default_endpoints(registry)
