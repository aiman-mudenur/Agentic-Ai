"""The connector as a real MCP server, speaking stdio.

    python -m course_organizer.connector.mcp_server

This module is the ``connector server`` in doc 2.10 Lab 5's deliverable --
*"A connector server with typed endpoints and auth; agents call only through
it."* It is not a mock of MCP and not a facade over one: it builds a genuine
``mcp.server.fastmcp.FastMCP`` server, registers each connector endpoint as an
MCP tool, and serves the Model Context Protocol over stdio. A client process
(:class:`~course_organizer.connector.client.McpConnectorClient`) spawns this
module as a subprocess and calls it.

What the server adds beyond the in-process registry
----------------------------------------------------

* **A process boundary.** The connector's authority -- its tokens, its
  adapter, its ledger -- lives in *this* process. A caller's memory cannot be
  used to escalate, because the caller has no handle on any of it.
* **A published contract.** ``tools/list`` advertises every endpoint with a
  JSON Schema derived from its Pydantic input model, so a client can discover
  the surface instead of being told it.
* **The credential stops being a parameter.** The bearer token is read once,
  from the server process's environment, at startup. No tool takes a token
  argument, so there is nothing for a model to fill in.

Why the tool wrappers are written out by hand
---------------------------------------------

``FastMCP`` derives a tool's input schema from a function's type hints and
offers no way to inject a schema directly (``mcp.server.fastmcp.tools.Tool
.from_function`` calls ``func_metadata(fn)`` and nothing else). Rather than
generating wrapper functions at import time, the four are written out
explicitly. That costs thirty lines and buys three things: the wire schema is
visible in the source, a reader can see exactly what crosses the process
boundary, and there is no metaprogramming between the declared Pydantic model
and the advertised one for a bug to hide in. When the input models change, the
signatures here are what a test fails on.

Run it
------

.. code-block:: console

   $ set MCP_CONNECTOR_TOKEN=<a shared secret>
   $ python -m course_organizer.connector.mcp_server --source local
   $ python -m course_organizer.connector.mcp_server --describe
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Optional

from course_organizer.connector.auth import (
    CONNECTOR_ROLES,
    ROLE_SCOPES,
    ensure_token,
)
from course_organizer.connector.core import EndpointRegistry
from course_organizer.connector.errors import (
    ConnectorError,
    ConnectorPolicyError,
    SourceUnavailableError,
    tag_for_wire,
)

#: The role the presented token belongs to when nothing says otherwise.
#: Faculty, because that is the role doc 2.3 puts in charge of the reads that
#: feed a judgement, and because it is the only role holding every scope -- so
#: the default can read the lab's data rather than refusing it.
ROLE_FACULTY = "course_faculty"

__all__ = ["build_server", "main", "serve"]

logger = logging.getLogger(__name__)

#: Set by :func:`build_server` so the module-level wrappers can reach the
#: registry without threading it through every call. A module global is the
#: right shape here precisely because there is exactly one registry per
#: server process, and the alternative -- closures per tool -- would put the
#: FastMCP registration inside a builder function and make the tool
#: declarations impossible to read.
_REGISTRY: Optional[EndpointRegistry] = None


def build_server(
    *,
    source: Optional[str] = None,
    token: Optional[str] = None,
    role: Optional[str] = None,
    seed_dir: Optional[str] = None,
) -> Any:
    """Build a ``FastMCP`` server exposing the connector's endpoints.

    Args:
        source: Which source adapter the server should use. Defaults to the
            server's ``SOURCE_ADAPTER`` environment variable, then to
            ``Settings.source_adapter``.
        token: The bearer token the server will accept. Defaults to the
            process's ``MCP_CONNECTOR_TOKEN``. When neither is set, an
            **ephemeral** token is generated and a warning is written to
            stderr: the server still starts, so ``tools/list`` works, but
            every call from a client holding a different token is refused.
            That is the policy working, not a bug, and the warning says so.
        role: Which :class:`~course_organizer.connector.auth.ConnectorRole` the
            presented token belongs to. Defaults to ``MCP_CONNECTOR_ROLE``,
            then to ``course_faculty``. See the note below on why this is
            needed and why it is not an escalation path.
        seed_dir: Overrides the seed root.

    Returns:
        A ``FastMCP`` instance with one tool per connector endpoint, and the
        registry attached as ``.course_organizer_registry`` for a test that
        wants to inspect the server's own ledger.

    Raises:
        SourceUnavailableError: If the requested source cannot be built.
        ConnectorPolicyError: If ``role`` is not a known role. Refusing to
            start is the point: a typo in a role name must not silently
            become a more permissive principal than the operator intended.
    """
    from mcp.server.fastmcp import FastMCP

    # Imported here, not at module scope, because connector/__init__ imports
    # the server's siblings and a module-level import would close the cycle.
    from course_organizer.connector import (
        build_default_connector,
        build_default_principals,
    )

    global _REGISTRY

    if token is None:
        token = os.environ.get("MCP_CONNECTOR_TOKEN")
    token, generated = ensure_token(token)
    if generated:
        print(
            "[course_organizer] MCP_CONNECTOR_TOKEN is not set. This server "
            "generated an EPHEMERAL token, so it will refuse calls from a "
            "client holding any other token -- including a client that "
            "generated its own. Set MCP_CONNECTOR_TOKEN in both processes.",
            file=sys.stderr,
            flush=True,
        )
    # _call() reads the environment first and falls back to this, so the two
    # entry points -- main() and a direct build_server() call from a test --
    # end up with the same credential.
    _GENERATED_TOKEN[0] = token

    if role is None:
        role = os.environ.get("MCP_CONNECTOR_ROLE") or None
    if role is None:
        role = ROLE_FACULTY
    if role not in CONNECTOR_ROLES:
        raise ConnectorPolicyError(
            f"cannot serve as {role!r}: it is not a known role. Known roles "
            f"are {sorted(CONNECTOR_ROLES)}. Refusing to start rather than "
            f"defaulting, because a misspelled role must never widen access."
        )
    # Exactly one principal is reachable over the wire: the one holding the
    # presented token, and its role is fixed here at startup. The role is not a
    # wire argument and not a tool parameter, so a model cannot select its own
    # privileges; the deployment chooses them out of band, the same way it
    # chooses the token.
    if role != ROLE_FACULTY:
        print(
            f"[course_organizer] this server will authorise the presented "
            f"token as role={role!r}, which does not hold every scope. "
            f"Refusals are expected and are not errors.",
            file=sys.stderr,
            flush=True,
        )

    if source is None:
        source = os.environ.get("SOURCE_ADAPTER") or None

    registry = build_default_connector(
        source=source,
        seed_dir=seed_dir,
        principals=build_default_principals(token=token, role=role),
        connector_transport="mcp_stdio",
    )
    _REGISTRY = registry

    server = FastMCP(
        "course-organizer-connector",
        instructions=(
            "One governed connector for the course worklet's three data "
            "sources, per doc 2.7:\n"
            "  read('the notes/materials store')\n"
            "  read('assignment & rubric definitions')\n"
            "  read('submission records')\n"
            "Every call is authorised against the caller's role. A refusal "
            "means the role does not hold the required scope, not that the "
            "data is missing."
        ),
    )
    server.course_organizer_registry = registry  # type: ignore[attr-defined]

    for spec in registry:
        _register(server, registry, spec.name)

    return server


def _register(server: Any, registry: EndpointRegistry, endpoint: str) -> None:
    """Register one connector endpoint as an MCP tool.

    The four wrappers are declared at module level so their signatures -- and
    therefore the wire schemas -- can be read directly. This function attaches
    each one to ``server`` under its connector name.

    Args:
        server: The ``FastMCP`` instance.
        registry: The connector, for the explicit wrappers.
        endpoint: Which endpoint this tool serves.
    """
    if endpoint == "list_courses":
        server.add_tool(_list_courses, name="list_courses")
    elif endpoint == "read_notes_store":
        server.add_tool(_read_notes_store, name="read_notes_store")
    elif endpoint == "read_assignment_rubrics":
        server.add_tool(
            _read_assignment_rubrics, name="read_assignment_rubrics"
        )
    elif endpoint == "read_submission_records":
        server.add_tool(
            _read_submission_records, name="read_submission_records"
        )
    else:
        # A fifth endpoint needs a fifth wrapper. Failing loudly here is the
        # point: an endpoint that reaches the wire without a written signature
        # has an untyped schema, which is the exact defect this lab exists to
        # prevent.
        raise ValueError(
            f"endpoint {endpoint!r} has no MCP wrapper. FastMCP derives a "
            f"tool's input schema from the function's type hints, so every "
            f"endpoint needs an explicitly typed wrapper in "
            f"{__name__}. Add one and list it in _register()."
        )
    # `registry` is threaded in for the wrappers' benefit even though they
    # read the module global; keeping the parameter means a future wrapper
    # can close over a specific registry in a test.
    del registry


# ---------------------------------------------------------------------------
# The four tools. Signatures here ARE the wire contract.
# ---------------------------------------------------------------------------
def _call(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a connector endpoint and return a JSON-safe result.

    Args:
        endpoint: The endpoint name.
        payload: Arguments, already keyword-matched to the wrapper's
            signature.

    Returns:
        The output model dumped with ``mode="json"``, so every value is a
        plain JSON type -- no ``datetime``, no ``Decimal``, nothing that
        ``json.dumps`` would refuse. That is what lets the tool return the
        model unchanged instead of hand-assembling a dict.

    Raises:
        ConnectorError: Any refusal, re-raised as a
            :class:`_WireError` carrying the bracketed type tag. The tag is
            what makes the *type* survive, not just the message: see
            :func:`course_organizer.connector.errors.tag_for_wire` for why
            FastMCP's own `ToolError` formatting cannot do this on its own.
    """
    if _REGISTRY is None:  # pragma: no cover - defensive
        raise ConnectorError(
            "the MCP connector server was asked to serve a call before its "
            "registry was built; this is a bug in the server, not a policy "
            "refusal"
        )
    token = os.environ.get("MCP_CONNECTOR_TOKEN")
    if not token:
        # build_server() replaced an unset token with a generated one; keep
        # that generated value in the environment for the life of the process
        # so the two agree.
        token = _GENERATED_TOKEN[0]
    try:
        result = _REGISTRY.invoke(endpoint, payload, token=token)
    except ConnectorError as exc:
        # Re-raise as a plain exception whose *message* is tagged. The class is
        # deliberately not ConnectorError any more: nothing in this process
        # catches it, and FastMCP turns anything raised here into
        # `ToolError(f"Error executing tool {name}: {e}")`, which is exactly
        # the wrapper that would otherwise flatten the type.
        raise _WireError(tag_for_wire(exc)) from exc
    return result.unwrap().model_dump(mode="json")


class _WireError(RuntimeError):
    """A connector refusal, serialised so its type survives the wire.

    Carries no behaviour. Its whole job is to have a ``str()`` that begins with
    ``[ConnectorErrorSubclass]``, because
    ``mcp.server.fastmcp.tools.base`` formats a tool failure as
    ``f"Error executing tool {name}: {e}"`` and nothing else -- no type, no
    structured payload. The client re-reads the tag with
    :func:`course_organizer.connector.errors.untag_from_wire`.

    Intentionally **not** a :class:`ConnectorError` subclass: if it were, some
    future ``except ConnectorError`` in the server would swallow it and turn a
    policy refusal into a successful-looking result. Being a stranger here is
    the safer default.
    """


#: Holds the token :func:`build_server` settled on, so :func:`_call` can reuse
#: it when the environment was not set. A one-element list rather than a
#: module global string so it can be rebound from inside a function.
_GENERATED_TOKEN: list[str] = [""]


def _list_courses() -> dict[str, Any]:
    """List the course codes this connector's source can serve.

    Use this first when you do not know a valid ``course_code``: the other
    three endpoints all require one, and guessing produces a refusal.

    Returns:
        ``{"provenance": ..., "course_codes": [...], "source_count": n}``.
    """
    return _call("list_courses", {})


def _read_notes_store(course_code: str, unit: str | None = None) -> dict[str, Any]:
    """Read 'the notes/materials store' for one course.

    Args:
        course_code: The course to read, e.g. ``CS201``.
        unit: Optional unit filter. Must be an exact unit id; an unknown one
            is refused with the real list rather than returning an empty
            result.

    Returns:
        The course's notes, their units and their version history, plus the
        provenance of the read.
    """
    return _call(
        "read_notes_store",
        {"course_code": course_code, "unit": unit},
    )


def _read_assignment_rubrics(course_code: str) -> dict[str, Any]:
    """Read 'assignment & rubric definitions' for one course.

    Args:
        course_code: The course to read, e.g. ``CS201``.

    Returns:
        The course's assignment briefs with their due dates, marks and rubric
        criteria, plus the provenance of the read.
    """
    return _call("read_assignment_rubrics", {"course_code": course_code})


def _read_submission_records(
    course_code: str, assignment_id: str | None = None
) -> dict[str, Any]:
    """Read 'submission records' for one course.

    Args:
        course_code: The course to read, e.g. ``CS201``.
        assignment_id: Optional single-assignment filter. Must be an exact id;
            an unknown one is refused with the real list rather than returning
            an empty result.

    Returns:
        The cohort's submission history, plus the provenance of the read.
    """
    return _call(
        "read_submission_records",
        {"course_code": course_code, "assignment_id": assignment_id},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve(
    *,
    source: Optional[str] = None,
    token: Optional[str] = None,
    role: Optional[str] = None,
    seed_dir: Optional[str] = None,
) -> None:
    """Build the server and run it over stdio. Blocks until the pipe closes.

    Args:
        source: Which source adapter to serve.
        token: The bearer token to accept.
        role: Which role that token belongs to. Defaults to
            ``course_faculty``.
        seed_dir: Overrides the seed root.
    """
    server = build_server(
        source=source, token=token, role=role, seed_dir=seed_dir
    )
    registry = server.course_organizer_registry  # type: ignore[attr-defined]
    print(
        f"[course_organizer] serving {registry.descriptor.describe()} with "
        f"{len(registry)} endpoint(s) over stdio",
        file=sys.stderr,
        flush=True,
    )
    server.run(transport="stdio")


def _describe(source: Optional[str], seed_dir: Optional[str]) -> int:
    """Print the connector's contract and exit, without serving.

    Useful for evidence and for checking a configuration change without a
    client. Reads the source but makes no calls, so it needs no token.

    Args:
        source: Which source adapter to describe.
        seed_dir: Overrides the seed root.

    Returns:
        A process exit code: 0 if the source is healthy, 1 if not.
    """
    from course_organizer.connector import build_default_connector

    try:
        registry = build_default_connector(source=source, seed_dir=seed_dir)
    except SourceUnavailableError as exc:
        print(f"connector unavailable: {exc}", file=sys.stderr)
        return 1

    print(registry.describe_all())
    print()
    print(registry.descriptor.describe())
    health = registry.adapter.health()
    print(f"source health: ok={health.ok} {health.reason}")
    if health.detail:
        print(f"  {health.detail}")
    print()
    print("schema keys per endpoint:")
    for name, schema in registry.schemas().items():
        required = schema["input"].get("required", [])
        print(f"  {name:<24} input required={required}")
    return 0 if health.ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    """Command-line entry point.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code. ``--describe`` returns 0/1; serving returns 0 when
        the pipe closes cleanly and 1 on a configuration error.
    """
    parser = argparse.ArgumentParser(
        prog="python -m course_organizer.connector.mcp_server",
        description=(
            "Serve the course organizer's connector over the Model Context "
            "Protocol on stdio. Doc 2.10 Lab 5: 'A connector server with "
            "typed endpoints and auth; agents call only through it.'"
        ),
    )
    parser.add_argument(
        "--source",
        default=None,
        help=(
            "Source adapter to serve: local (default), in_memory or google. "
            "Overrides the SOURCE_ADAPTER environment variable."
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Bearer token to accept. Prefer the MCP_CONNECTOR_TOKEN "
            "environment variable so the token does not appear in the process "
            "list."
        ),
    )
    parser.add_argument(
        "--role",
        default=None,
        help=(
            f"Role the accepted token belongs to; one of "
            f"{', '.join(sorted(ROLE_SCOPES))}. Prefer MCP_CONNECTOR_ROLE. "
            f"Defaults to {ROLE_FACULTY}. This decides which scopes the "
            f"presented credential holds, so choosing it is an act of "
            f"deployment, not of calling."
        ),
    )
    parser.add_argument(
        "--seed-dir",
        default=None,
        help="Override the seed corpus root.",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help=(
            "Print the endpoint contract, the source descriptor and the "
            "schemas, then exit. Makes no calls and needs no token."
        ),
    )
    args = parser.parse_args(argv)

    # MCP logs every request at INFO to stderr. The protocol itself is on
    # stdout, so this is not a correctness problem, but a client that captures
    # stderr for evidence gets hundreds of lines that say only
    # "Processing request of type CallToolRequest". Raise the level and keep
    # the noise out of the evidence files.
    for noisy in ("mcp", "mcp.server", "mcp.server.fastmcp", "mcp.server.lowlevel"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.describe:
        return _describe(args.source, args.seed_dir)

    token = args.token or os.environ.get("MCP_CONNECTOR_TOKEN")
    token, generated = ensure_token(token)
    if generated:
        _GENERATED_TOKEN[0] = token
    if args.token:
        # An explicit --token is the one case where the environment must be
        # told, because _call() reads the environment.
        os.environ["MCP_CONNECTOR_TOKEN"] = token

    try:
        serve(source=args.source, token=token, role=args.role, seed_dir=args.seed_dir)
    except SourceUnavailableError as exc:
        print(f"connector unavailable: {exc}", file=sys.stderr)
        return 1
    except ConnectorError as exc:
        print(f"connector error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
