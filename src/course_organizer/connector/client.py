"""What an agent actually holds: a client, never a source, never a credential.

The connector's contract with its callers is this sentence:

    an agent holds a :class:`ConnectorClient`; the client holds the credential;
    the model never sees either.

That is enforced by the shape of :meth:`ConnectorClient.read` -- it takes an
endpoint name and arguments, and there is **no parameter** through which a
caller could supply a token. This is the whole point of putting auth in the
connector rather than in the agent, and it is a structural property rather
than a discipline: an LLM cannot be talked into escalating, because the tool
it is given has nowhere to put a credential. Doc 2.10 Lab 5 asks for *"typed
endpoints and auth"* and this is what makes the second half of that phrase
mean something at run time.

Two clients implement it, and the comparison between them is Lab 5's proof
that there is one governed surface rather than two:

:class:`InProcessConnectorClient`
    Calls an :class:`~course_organizer.connector.core.EndpointRegistry`
    directly, in one process.

:class:`McpConnectorClient`
    Spawns a **real MCP server** -- ``python -m
    course_organizer.connector.mcp_server`` -- over stdio, and talks to it
    through ``langchain-mcp-adapters``. A separate OS process, a real
    protocol, and a result that arrives as untyped JSON inside a text block.

Both return the same validated Pydantic model for the same call, which is the
testable form of *"a connector server with typed endpoints"*.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ValidationError

from course_organizer.connector.core import EndpointRegistry
from course_organizer.connector.errors import (
    ConnectorAuthError,
    ConnectorContractError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
    ConnectorTransportError,
    SourceUnavailableError,
    untag_from_wire,
)
from course_organizer.connector.port import SourceDescriptor
from course_organizer.logging_setup import log_event

__all__ = [
    "ConnectorClient",
    "ConnectorContract",
    "InProcessConnectorClient",
    "McpConnectorClient",
    "contract_from_registry",
    "run_coroutine",
    "server_command",
]

logger = logging.getLogger(__name__)

#: How long to wait for a spawned server to advertise its tools. Generous
#: because the first call on a cold machine includes spawning a Python
#: process, importing ``mcp`` and ``langchain-mcp-adapters`` and loading the
#: seed corpus. Measured at roughly two seconds on the development machine;
#: thirty leaves room for a slow disk without turning a hang into a timeout
#: that looks like a policy problem.
_STARTUP_TIMEOUT = 30.0


class _LoopThread:
    """A private event loop on a background thread, alive for a client's life.

    Why this exists, because it is not incidental plumbing
    ------------------------------------------------------

    ``langchain-mcp-adapters`` 0.3.2 has no client-lifetime session. The two
    obvious ways to get tools both open a session only long enough to list
    them, and then each tool call opens its own:

    * ``async with MultiServerMCPClient({...}) as client:`` raises
      ``NotImplementedError`` outright -- removed in 0.1.0
      (``client.py:355``). Confirmed by reading the source and by running it.
    * ``await client.get_tools()`` looks persistent and is not. It opens a
      session, lists, and closes it (``tools.py:585-596``). The returned tools
      were built with ``session=None``, so each ``call_tool`` re-enters
      ``create_session`` (``tools.py:460-471``) and spawns a new server
      subprocess. The method's own docstring warns: *"A new session will be
      created for each tool call."*

    The first version of this client used :func:`run_coroutine` per call -- one
    ``asyncio.run`` each -- and measured a fresh ``[course_organizer] serving
    ...`` banner plus a ~2 s pause on *every single call*. Two consequences,
    both disqualifying:

    * the MCP client cost ~2 s per call against ~1 ms in process, so any
      wall-clock comparison between the two transports was measuring process
      startup rather than the thing being compared; and
    * Lab 8's parallel-versus-sequential measurement would have been swamped
      by connector startup noise.

    The fix is ``async with client.session(name) as live:`` plus
    ``load_mcp_tools(live, ...)``, which closes the live session into every
    tool it returns, so all calls share one subprocess. Holding that session
    open needs a coroutine that parks indefinitely, which needs a loop that
    outlives any single call -- hence this thread, and hence
    :meth:`McpConnectorClient.close` being part of the interface rather than
    an afterthought: a client that never stops leaks one thread and one
    subprocess.
    """

    def __init__(self, name: str = "connector-mcp") -> None:
        self._name = name
        self._loop: Any = None
        self._thread: Any = None
        self._ready = threading.Event()

    def start(self) -> Any:
        """Start the thread and return its loop. Idempotent."""
        if self._loop is not None:
            return self._loop
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):  # pragma: no cover - defensive
            raise ConnectorTransportError(
                f"the connector client's event loop thread ({self._name}) did "
                f"not come up within 30s"
            )
        return self._loop

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def submit(self, coro: Any, timeout: float = 120.0) -> Any:
        """Run a coroutine on the private loop and return its result.

        **Propagates the coroutine's own exception unchanged.** That is a
        deliberate design choice and it was learned the hard way: an earlier
        version wrapped anything that was not a
        :class:`~course_organizer.connector.errors.ConnectorError` into a
        ``ConnectorTransportError`` with the message appended. But a policy
        refusal arrives from the server as a ``ToolError``, not a connector
        error, so the wrapper fired first and buried the wire tag that
        :func:`_translate_mcp_error` needs to re-type it -- the caller got
        "the connector client's event loop failed" for what was actually "you
        are not allowed to do that".

        This method therefore only classifies failures that are genuinely its
        own: a timeout, or a loop that is no longer running. Everything else
        belongs to the caller, which is the layer that knows what the wire
        means.

        Args:
            coro: The coroutine.
            timeout: Seconds to wait. The default is generous because the very
                first submit also starts the server subprocess, which on a cold
                filesystem is not instant.

        Returns:
            The coroutine's result.

        Raises:
            ConnectorTransportError: On timeout, or if the private loop is gone.
        """
        future = self.spawn(coro)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ConnectorTransportError(
                f"an MCP call did not complete within {timeout}s; the server "
                f"process may be wedged"
            ) from exc
        except BaseException as exc:
            loop = self._loop
            if loop is None or loop.is_closed():
                # The loop died under us, so the failure is ours to report.
                raise ConnectorTransportError(
                    f"the connector client's event loop "
                    f"({self._name}) is no longer running: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            # The loop is healthy; the coroutine simply raised. Hand the
            # original exception up untouched.
            raise

    def spawn(self, coro: Any) -> Any:
        """Schedule a coroutine on the private loop and return its future.

        The non-blocking counterpart of :meth:`submit`, for work that is meant
        to stay running. The MCP session coroutine parks inside
        ``async with client.session(...)`` for the client's whole life, so
        waiting on it would deadlock the caller.

        Args:
            coro: The coroutine.

        Returns:
            A ``concurrent.futures.Future``.
        """
        loop = self.start()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the loop and join the thread. Idempotent."""
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)



def run_coroutine(coro: Any) -> Any:
    """Run one coroutine to completion, on any thread.

    The labs and the CLI are synchronous; the MCP client is asynchronous. This
    is the seam between them.

    Uses :func:`asyncio.run` when no loop is running, and a dedicated worker
    thread when one already is -- because calling ``asyncio.run`` from inside
    a running loop raises, and a notebook or a LangGraph node is a perfectly
    ordinary place for one already to be running.

    Args:
        coro: The coroutine to run.

    Returns:
        Its result.

    Raises:
        ConnectorTransportError: If the coroutine raised, with the original
            exception chained so nothing is lost.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorTransportError(
                f"the connector client failed: {type(exc).__name__}: {exc}"
            ) from exc

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(asyncio.run, coro).result()
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorTransportError(
                f"the connector client failed: {type(exc).__name__}: {exc}"
            ) from exc


@runtime_checkable
class ConnectorClient(Protocol):
    """The only thing an agent is given in place of a data source.

    Note the absence of a credential parameter, and of any method that returns
    a source adapter, a filesystem path or a settings object. A caller that
    implements this protocol has exactly one way to get data, and that way goes
    through the connector.
    """

    def read(
        self, endpoint: str, payload: Optional[Mapping[str, Any]] = None
    ) -> BaseModel:
        """Call one endpoint and return its validated output model.

        Args:
            endpoint: The endpoint name, e.g. ``read_notes_store``.
            payload: Arguments, which must match the endpoint's declared input
                model.

        Returns:
            The endpoint's declared output model, validated.

        Raises:
            ConnectorError: Any refusal, contract failure or transport problem.
                The subclasses are documented on
                :mod:`course_organizer.connector.errors`.
        """
        ...

    def names(self) -> list[str]:
        """Every endpoint this client can reach, sorted."""
        ...

    def descriptor(self) -> SourceDescriptor:
        """How the source behind this client describes itself."""
        ...

    def describe(self) -> str:
        """A human-readable summary for the notebook and the report."""
        ...

    def close(self) -> None:
        """Release any transport resources. Safe to call more than once."""
        ...


class ConnectorContract:
    """The endpoints and models a client expects a server to implement.

    Separated from both the client and the server so that "the client checks
    the server" is a thing that can be *said*, *tested* and *violated on
    purpose*. An MCP result arrives as a JSON string in a text block; nothing
    in the protocol guarantees it matches what the server promised. This class
    is the client's copy of the promise, and it re-validates every response
    against it.

    Args:
        outputs: Endpoint name to its declared output model.

    Example:
        >>> from course_organizer.connector import build_default_connector
        >>> contract = contract_from_registry(build_default_connector())
        >>> sorted(contract.outputs)
        ['list_courses', 'read_assignment_rubrics', 'read_notes_store', 'read_submission_records']
    """

    def __init__(self, outputs: Mapping[str, type[BaseModel]]) -> None:
        self._outputs = dict(outputs)

    @property
    def outputs(self) -> dict[str, type[BaseModel]]:
        """A copy of the endpoint-to-model mapping."""
        return dict(self._outputs)

    def names(self) -> list[str]:
        """Every expected endpoint name, sorted."""
        return sorted(self._outputs)

    def model_for(self, endpoint: str) -> type[BaseModel]:
        """The output model an endpoint must return.

        Args:
            endpoint: The endpoint name.

        Returns:
            Its declared output model.

        Raises:
            ConnectorNotFoundError: If the endpoint is not in the contract.
        """
        if endpoint not in self._outputs:
            raise ConnectorNotFoundError(
                f"no connector endpoint named {endpoint!r}; the contract "
                f"covers {self.names()}"
            )
        return self._outputs[endpoint]

    def validate(self, endpoint: str, payload: Any) -> BaseModel:
        """Check a decoded payload against the endpoint's declared model.

        The one place the "typed" in "typed endpoints" is actually enforced
        across a process boundary.

        Args:
            endpoint: Which endpoint answered.
            payload: The decoded JSON, expected to be a mapping.

        Returns:
            The validated output model.

        Raises:
            ConnectorContractError: If the payload is not a mapping, or does
                not validate. The message names the endpoint, the model and the
                first problem, because "invalid response" with no detail is the
                least debuggable error a client can raise.
        """
        model = self.model_for(endpoint)
        if not isinstance(payload, Mapping):
            raise ConnectorContractError(
                f"endpoint {endpoint!r} returned {type(payload).__name__}, but "
                f"its declared {model.__name__} is an object. A connector that "
                f"returns an untyped payload is not a typed endpoint."
            )
        try:
            return model.model_validate(dict(payload))
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ConnectorContractError(
                f"endpoint {endpoint!r} returned a payload that does not match "
                f"its declared {model.__name__}: {exc.error_count()} "
                f"problem(s); first: {first['loc']} {first['msg']}"
            ) from exc


def contract_from_registry(registry: EndpointRegistry) -> ConnectorContract:
    """Build a contract from a live registry's declarations.

    Args:
        registry: Any wired connector. The adapter is never called.

    Returns:
        The matching contract.
    """
    return ConnectorContract(
        {name: registry.get(name).output_model for name in registry.names()}
    )


class InProcessConnectorClient:
    """:class:`ConnectorClient` backed by a registry in this same process.

    The default client. It is what the labs, the tests and the notebook use,
    because it needs no subprocess and no protocol, and a reader should not
    have to understand a transport to understand the governance.

    Args:
        registry: The connector to call.
        token: The bearer credential this client presents on every call. Held
            here and never exposed. Prefer a
            :meth:`PrincipalRegistry.token_for_role` lookup over a literal.
    """

    def __init__(self, *, registry: EndpointRegistry, token: str) -> None:
        self._registry = registry
        self._token = token
        self._closed = False

    @property
    def registry(self) -> EndpointRegistry:
        """The registry behind this client. For evidence, not for agents."""
        return self._registry

    @property
    def token_fingerprint(self) -> str:
        """16 hex characters identifying the held credential.

        Lets a test or a report prove *which* credential was used without
        printing it. Computed the same way the ledger computes it, so the two
        always agree.
        """
        import hashlib

        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()[:16]

    def read(
        self, endpoint: str, payload: Optional[Mapping[str, Any]] = None
    ) -> BaseModel:
        """Call one endpoint. The token is attached here and nowhere else."""
        if self._closed:
            raise ConnectorTransportError(
                "this connector client has been closed"
            )
        return self._registry.invoke(endpoint, payload, token=self._token).unwrap()

    def names(self) -> list[str]:
        """Every endpoint this client can reach, sorted."""
        return self._registry.names()

    def descriptor(self) -> SourceDescriptor:
        """How the source behind this client describes itself."""
        return self._registry.descriptor

    def describe(self) -> str:
        """A human-readable summary for the notebook and the report."""
        return (
            f"InProcessConnectorClient -> "
            f"{self._registry.descriptor.name} via in_process, "
            f"{len(self._registry)} endpoint(s), "
            f"credential #{self.token_fingerprint}"
        )

    def close(self) -> None:
        """Nothing to release. Idempotent by design."""
        self._closed = True

    def __enter__(self) -> "InProcessConnectorClient":
        """No setup needed, so this is a no-op kept for symmetry.

        Lets a caller write ``with build_client(...) as client:`` without
        caring which transport it got, which is the same property the
        acceptance criterion is about.
        """
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Close on the way out, including on an exception."""
        self.close()


#: Connector error class names, mapped to the class itself, so a refusal that
#: crossed the process boundary can be re-raised as the *same type* an
#: in-process caller would have seen. Callers should not have to write two
#: ``except`` clauses depending on which transport they happened to use.
_ERROR_CLASSES: dict[str, type[ConnectorError]] = {
    cls.__name__: cls
    for cls in (
        ConnectorAuthError,
        ConnectorPolicyError,
        ConnectorContractError,
        ConnectorNotFoundError,
        SourceUnavailableError,
        ConnectorTransportError,
    )
}


def _real_stderr() -> Any:
    """The lowest-level stream on standard error, or ``None`` if there is none.

    ``sys.__stderr__`` is the interpreter's *original* ``sys.stderr``, kept
    separately from ``sys.stderr`` precisely so that code which replaces the
    latter can still reach the former. That matters here because replacing
    ``sys.stderr`` is what a Jupyter kernel does.

    Args:
        None.

    Returns:
        A stream with a working ``fileno()``, or ``None`` when the process has no
        usable descriptor 2 at all (some embedded or daemonised hosts).
    """
    for candidate in (getattr(sys, "__stderr__", None),):
        if candidate is None:
            continue
        try:
            candidate.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            continue
        return candidate
    return None


def _stdio_errlog_target() -> Any:
    """The function object whose ``__defaults__`` holds MCP's bound stderr.

    ``mcp.client.stdio.stdio_client`` is wrapped by ``@asynccontextmanager``, and
    ``functools.wraps`` copies ``__name__``, ``__doc__`` and ``__dict__`` but
    **not** ``__defaults__``. So the wrapper's own ``__defaults__`` is ``None`` and
    the bound ``sys.stderr`` lives one level down, on ``__wrapped__``. Patching
    the wrapper therefore looks like it succeeded and changes nothing.

    Args:
        None.

    Returns:
        The function to rebind, or ``None`` if ``mcp`` cannot be imported.
    """
    try:
        from mcp.client.stdio import stdio_client
    except ImportError:  # pragma: no cover - mcp is a hard dependency
        return None
    inner = getattr(stdio_client, "__wrapped__", None)
    return inner if inner is not None else stdio_client


@asynccontextmanager
async def _fd_backed_stderr() -> Any:
    """Make MCP's stdio transport work from inside a Jupyter kernel.

    **Why this exists.** ``mcp.client.stdio.stdio_client`` is declared as
    ``async def stdio_client(server, errlog: TextIO = sys.stderr)`` -- the default
    is evaluated when the module is *imported* -- and it hands that stream to
    ``subprocess.Popen(stderr=errlog)``, which needs a real file descriptor.
    ``langchain_mcp_adapters.sessions`` calls it with one argument, so the only
    seam is the default itself (see :func:`_stdio_errlog_target` for why that is
    not as obvious as it looks).

    Under ipykernel it is the wrong stream: ``ipykernel/kernelapp.py`` replaces
    ``sys.stderr`` with an ``OutStream`` that forwards to the kernel's iopub
    channel and has no OS-level descriptor. A spawn from a notebook cell
    therefore dies with ``io.UnsupportedOperation: fileno``, before the server
    has run a line.

    **What this does.** If the bound default has no usable descriptor, point it
    at the interpreter's original stderr for the duration of the block, then put
    it back. A no-op in every ordinary process, which is the point: the CLI, the
    tests, and any server-side use keep the library's own default and are
    untouched by code that exists only for the kernel case.

    **What it does not claim.** This cannot fix a host with no descriptor 2 at
    all; :func:`_real_stderr` returns ``None`` there and the block does nothing,
    so the original ``fileno`` error surfaces unchanged and is reported as a
    transport failure. It is not hidden, and it is not called a policy problem.

    Yields:
        The replacement stream, or ``None`` when no substitution was made. Nothing
        here is useful to the caller either way.
    """
    target = _stdio_errlog_target()
    if target is None:  # pragma: no cover - mcp is a hard dependency
        yield None
        return

    defaults = target.__defaults__
    if defaults is None or not defaults:
        yield None
        return

    bound = defaults[0]
    try:
        bound.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        pass
    else:
        # The library's own default is already usable. Nothing to do, which is
        # the case for every process that is not a Jupyter kernel.
        yield None
        return

    replacement = _real_stderr()
    if replacement is None:  # pragma: no cover - no descriptor 2 anywhere
        yield None
        return

    log_event(
        logger,
        "connector.mcp.stderr_redirected",
        target=getattr(target, "__name__", "?"),
        bound_type=type(bound).__name__,
        replacement_type=type(replacement).__name__,
    )
    target.__defaults__ = (replacement,) + tuple(defaults[1:])
    try:
        yield replacement
    finally:
        target.__defaults__ = defaults


def _translate_mcp_error(endpoint: str, exc: BaseException) -> ConnectorError:
    """Turn a wire-level failure into the connector error it stands for.

    Why this is needed
    ------------------

    ``MultiServerMCPClient`` defaults to ``handle_tool_errors=True``, which
    catches a tool's exception and returns its **message as a text block**
    instead of raising. For a governance system that default is exactly wrong:
    a policy refusal would arrive as an ordinary-looking result, the client's
    JSON decode would then fail on non-JSON text, and the caller would be told
    "the endpoint returned a text block that is not JSON" -- a contract error
    -- when the truth was "you are not allowed to do that". The client is
    therefore constructed with ``handle_tool_errors=False``, so the failure
    surfaces as a ``ToolError`` that this function can read.

    **The type does not come for free, and this is the part worth knowing.**
    ``mcp.server.fastmcp.tools.base:117`` formats a tool failure as
    ``f"Error executing tool {name}: {e}"`` and the protocol carries it as an
    ``isError`` result with that text. Only the message crosses; the exception
    *class* is discarded by the MCP layer. So the server writes an explicit
    ``[ConnectorPolicyError]`` tag in front of the message (see
    :func:`course_organizer.connector.errors.tag_for_wire`) and this function
    reads that tag. An earlier version searched the message for a bare class
    name instead, which was wrong in both directions: it missed every error
    whose message did not name its own class -- all of them, as it turned out
    -- and it would have mis-typed any error that merely *mentioned* a class
    name in prose.

    Args:
        endpoint: The endpoint that was called, for the fallback message.
        exc: The exception the transport raised.

    Returns:
        A connector error, re-typed when the wire tag named a known class.
    """
    text = str(exc)
    name, body = untag_from_wire(text)
    cls = _ERROR_CLASSES.get(name or "")
    if cls is not None:
        return cls(body or text)
    return ConnectorTransportError(
        f"MCP call to {endpoint!r} failed on the server with an error that is "
        f"not one of the connector's own types: {type(exc).__name__}: {text}"
    )


def server_command(
    *, source: Optional[str] = None, seed_dir: Optional[str] = None
) -> tuple[str, list[str], dict[str, str]]:
    """The command that launches the connector server, and its environment.

    Kept in one place so the client and the tests agree on how the server is
    started, and so a change to the entry point is a one-line edit.

    Two deliberate properties:

    * **No ``--token`` argument.** The server accepts one, but a command line
      is visible to every process on the machine, whereas an environment
      variable set for the child alone is not. :class:`McpConnectorClient`
      passes the credential only through the environment, which is why the
      token never appears in ``server_names()``, in any endpoint's arguments,
      or in anything a model can see.
    * **The same interpreter that is running the client.** ``sys.executable``
      rather than the bare string ``python``, so a virtualenv client does not
      spawn a system Python that lacks ``mcp``.

    Args:
        source: Optional ``--source`` value.
        seed_dir: Optional ``--seed-dir`` value.

    Returns:
        ``(executable, args, env_additions)``. ``env_additions`` holds only
        what the caller should layer *on top of* its own environment; it is
        not a complete environment.
    """
    args = ["-m", "course_organizer.connector.mcp_server"]
    if source:
        args += ["--source", source]
    if seed_dir:
        args += ["--seed-dir", str(seed_dir)]

    # Without src/ on the path the child cannot import the package. The parent
    # has already resolved it, so reuse the parent's sys.path entries rather
    # than assuming this is an installed (pip -e) layout.
    child_env: dict[str, str] = {}
    # client.py lives at <root>/src/course_organizer/connector/client.py, so
    # parents[2] is <root>/src -- the directory that must be importable for
    # `python -m course_organizer.connector.mcp_server` to resolve. (parents[1]
    # is the package directory itself, which cannot import `course_organizer`.)
    package_root = str(Path(__file__).resolve().parents[2])
    existing = os.environ.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if package_root not in parts:
        parts.insert(0, package_root)
    child_env["PYTHONPATH"] = os.pathsep.join(parts)
    return sys.executable, args, child_env


def _project_root() -> str:
    """The repository root, derived from the installed package's location.

    ``course_organizer/__init__.py`` -> parents[0] is the package,
    parents[1] is ``src``, parents[2] is the root. Used only as the
    subprocess's working directory so a relative ``--seed-dir`` would resolve
    the same way it does in-process.
    """
    import pathlib

    import course_organizer

    return str(pathlib.Path(course_organizer.__file__).resolve().parents[2])


class McpConnectorClient:
    """:class:`ConnectorClient` backed by a real MCP server over stdio.

    This is the ``connector server`` in doc 2.10 Lab 5's deliverable, and it is
    a real one: a separate process, spawned by
    ``langchain-mcp-adapters``, speaking the Model Context Protocol over
    stdio. Nothing here is a simulation of MCP.

    **What the wire actually does, measured rather than assumed.** A tool call
    over this transport returns a list of content blocks -- in
    ``langchain-mcp-adapters`` 0.3.2, ``[{"type": "text", "text": "<json
    string>", "id": ...}]``. The typed payload has been through
    ``json.dumps`` and back, so on the far side of the boundary it is a plain
    ``dict``. That is the real reason a *client* contract is necessary and not
    a belt-and-braces flourish: a correct server and a wrong one are
    indistinguishable until something validates against a declared model.
    :meth:`read` does that, and
    ``tests/test_lab05_mcp_connector.py`` proves it by pointing this client at
    a server that returns a deliberately mis-typed payload.

    Args:
        contract: The endpoints and output models this client requires.
        token: The bearer credential, passed to the server process through its
            environment as ``MCP_CONNECTOR_TOKEN``. Never an argument of any
            endpoint, so a model cannot influence it.
        source: Which source the spawned server should use, passed the same
            way. Defaults to the server's own configured default.
        role: Which role the presented credential belongs to, passed the same
            way. Defaults to ``course_faculty``. Over the wire exactly one
            principal is reachable, so this is how Lab 5 demonstrates a role
            *refusal* rather than only a success -- a server started with
            ``role="student"`` will refuse ``read_submission_records`` with a
            :class:`~course_organizer.connector.errors.ConnectorPolicyError`
            that arrives on this side with its type intact.
        command: Override the server command. For the misbehaving-server test.
        extra_env: Extra environment for the server process.
        connection_name: The key this connection is registered under.

    Example:
        >>> client = McpConnectorClient(  # doctest: +SKIP
        ...     contract=contract_from_registry(build_default_connector()),
        ...     token=token,
        ... )
        >>> client.read("list_courses").describe()  # doctest: +SKIP
        '2 course(s) from local_seed: CS201, DBMS402'
    """

    def __init__(
        self,
        *,
        contract: ConnectorContract,
        token: str,
        source: Optional[str] = None,
        role: Optional[str] = None,
        command: Optional[Sequence[str]] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        connection_name: str = "course_organizer",
    ) -> None:
        self._contract = contract
        self._token = token
        self._source = source
        self._role = role
        self._command = list(command) if command else None
        self._extra_env = dict(extra_env or {})
        self._connection_name = connection_name
        self._tools: dict[str, Any] = {}
        self._loop_thread: Optional[_LoopThread] = None
        self._release: Optional[threading.Event] = None
        self._tools_ready: Optional[threading.Event] = None
        self._startup_error: Optional[BaseException] = None
        self._descriptor: Optional[SourceDescriptor] = None
        self._closed = False
        self._calls = 0

    # -- transport -------------------------------------------------------
    def _connection(self) -> dict[str, Any]:
        """The stdio connection spec handed to ``MultiServerMCPClient``."""
        if self._command is not None:
            command, args = self._command[0], list(self._command[1:])
            child_env: dict[str, str] = {}
        else:
            base_command, base_args, child_env = server_command()
            command, args = base_command, list(base_args)
            if self._source:
                args = args + ["--source", self._source]

        env = dict(os.environ)
        env["MCP_CONNECTOR_TOKEN"] = self._token
        if self._role:
            # Same reasoning as the token: the server process's environment is
            # the one channel that is not reachable from a tool call, so it is
            # where a deployment-level decision like "this credential is a
            # student's" belongs. It is deliberately NOT a parameter of
            # `read()`, so no caller -- least of all a model -- can raise its
            # own privileges by asking.
            env["MCP_CONNECTOR_ROLE"] = self._role
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.update(child_env)
        env.update(self._extra_env)

        return {
            "command": command,
            "args": args,
            "transport": "stdio",
            "env": env,
            "cwd": _project_root(),
        }

    def _ensure_tools(self) -> dict[str, Any]:
        """Start the server once, on a persistent loop, and keep the session open.

        See :class:`_LoopThread` for why the session has to stay open rather
        than being opened per call. The short version: opening it per call
        respawns the server subprocess on every call, which costs seconds and
        would make Lab 5's transport comparison and Lab 8's timing measurement
        both meaningless.

        ``handle_tool_errors=False`` is set deliberately; see
        :func:`_translate_mcp_error` for what happens without it.

        Raises:
            ConnectorTransportError: If the server cannot be started.
        """
        if self._tools:
            return self._tools
        if self._closed:
            raise ConnectorTransportError("this connector client has been closed")

        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools

        connection = self._connection()
        self._loop_thread = _LoopThread(f"connector-mcp-{self._connection_name}")
        self._release = threading.Event()
        self._tools_ready = threading.Event()
        self._startup_error: Optional[BaseException] = None

        async def _session() -> None:
            try:
                client = MultiServerMCPClient(
                    {self._connection_name: connection},
                    handle_tool_errors=False,
                )
                # NOT `get_tools()`, and NOT `async with client`. Both were
                # tried and both are wrong for a client that makes more than
                # one call, in ways the library documents and this file has
                # now been bitten by:
                #
                #   * `async with client:` raises NotImplementedError as of
                #     langchain-mcp-adapters 0.1.0 (verified by reading
                #     client.py:355 and by running it).
                #   * `await client.get_tools()` opens a session to list the
                #     tools and closes it on return
                #     (tools.py:585-596). The returned tools are built with
                #     `session=None`, so every `call_tool` re-enters
                #     `create_session` (tools.py:460-471) -- which spawns a new
                #     server subprocess. The docstring even says so: "A new
                #     session will be created for each tool call."
                #
                # `client.session(name)` is an async context manager over a
                # real ClientSession, and `load_mcp_tools(session, ...)` closes
                # that live session into every tool it returns. Holding the
                # `async with` open for the client's lifetime is what makes one
                # spawn serve every call.
                #
                # `_fd_backed_stderr` is what makes this work from a notebook
                # cell; see its docstring. It is a no-op everywhere else, which
                # is why it can sit here without conditional logic of its own.
                async with (
                    _fd_backed_stderr(),
                    client.session(self._connection_name) as live,
                ):
                    tools = await load_mcp_tools(
                        live,
                        connection=connection,
                        server_name=self._connection_name,
                        handle_tool_errors=False,
                    )
                    # langchain-mcp-adapters 0.3.2 returns a list[BaseTool]
                    # from get_tools and a list[BaseTool] from load_mcp_tools.
                    # Be tolerant of a Mapping either way, so a future release
                    # fixing an annotation cannot silently break this.
                    if isinstance(tools, Mapping):
                        self._tools = dict(tools)
                    else:
                        self._tools = {
                            getattr(t, "name", str(i)): t
                            for i, t in enumerate(tools)
                        }
                    self._tools_ready.set()
                    # Park here holding the session (and so the server
                    # subprocess) open for the client's whole life. This
                    # coroutine is spawned, not submitted, because waiting on
                    # it would block the caller forever.
                    await asyncio.to_thread(self._release.wait)
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                self._startup_error = exc
                self._tools_ready.set()

        self._loop_thread.spawn(_session())

        if not self._tools_ready.wait(timeout=_STARTUP_TIMEOUT):  # pragma: no cover
            self._cleanup_on_failure()
            raise ConnectorTransportError(
                f"the MCP connector server did not become ready within "
                f"{_STARTUP_TIMEOUT}s"
            )
        if self._startup_error is not None:
            error = self._startup_error
            self._cleanup_on_failure()
            if isinstance(error, ConnectorError):
                raise error
            raise ConnectorTransportError(
                f"could not start the MCP connector server "
                f"({connection['command']} {' '.join(connection['args'])}): "
                f"{type(error).__name__}: {error}. Doc 2.10 Lab 5 requires a "
                f"connector *server*; if the subprocess cannot start, the "
                f"transport is broken, not the policy."
            ) from error

        log_event(
            logger,
            "connector.mcp.connected",
            tools=sorted(self._tools),
            source=self._source or "<server default>",
        )
        return self._tools

    def _cleanup_on_failure(self) -> None:
        """Release the loop thread after a failed startup."""
        if self._release is not None:
            self._release.set()
        if self._loop_thread is not None:
            self._loop_thread.stop()
        self._loop_thread = None

    # -- the call --------------------------------------------------------
    def read(
        self, endpoint: str, payload: Optional[Mapping[str, Any]] = None
    ) -> BaseModel:
        """Call one endpoint on the server and validate what comes back.

        Args:
            endpoint: The endpoint name.
            payload: Arguments matching the endpoint's declared input model.

        Returns:
            The endpoint's declared output model, validated locally against
            this client's contract.

        Raises:
            ConnectorNotFoundError: The server does not expose the endpoint.
            ConnectorContractError: The response was not JSON, not an object,
                or did not match the declared model.
            ConnectorTransportError: The server could not be reached, or the
                call failed on the wire.
        """
        tools = self._ensure_tools()
        if endpoint not in tools:
            raise ConnectorNotFoundError(
                f"the MCP connector server does not expose {endpoint!r}; it "
                f"exposes {sorted(tools)}"
            )

        arguments = dict(payload or {})
        self._calls += 1
        try:
            raw = self._loop_thread.submit(
                tools[endpoint].ainvoke(arguments)
            )
        except ConnectorError:
            raise
        except Exception as exc:
            # A refusal raised inside the server arrives here as an McpError
            # carrying the original exception's type and message. Re-raise it
            # as that type, so a caller writes one `except` clause regardless
            # of which transport it is holding.
            raise _translate_mcp_error(endpoint, exc) from exc

        decoded = self._decode(endpoint, raw)
        value = self._contract.validate(endpoint, decoded)
        if self._descriptor is None:
            self._descriptor = _descriptor_from_payload(endpoint, value)
        return value

    def _decode(self, endpoint: str, raw: Any) -> Any:
        """Turn an MCP tool result into decoded JSON.

        Handles the three shapes this transport is known to produce, and is
        explicit that anything else is a contract failure rather than
        something to guess at:

        1. ``[{"type": "text", "text": "<json>"}]`` -- what
           ``langchain-mcp-adapters`` 0.3.2 actually returns. Confirmed by
           running it, not by reading the type hints.
        2. ``{"type": "text", "text": "<json>"}`` -- a bare single block.
        3. A mapping that is already structured content.

        Args:
            endpoint: The endpoint, for error messages.
            raw: The tool result.

        Returns:
            The decoded JSON value.

        Raises:
            ConnectorContractError: If the shape is unrecognised, or the text
                is not JSON.
        """
        block: Any = raw
        if isinstance(raw, list):
            if not raw:
                raise ConnectorContractError(
                    f"endpoint {endpoint!r} returned an empty content list; "
                    f"there is no payload to validate"
                )
            block = raw[0]

        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ConnectorContractError(
                    f"endpoint {endpoint!r} returned a text block whose 'text' "
                    f"is {type(text).__name__}, not a string"
                )
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConnectorContractError(
                    f"endpoint {endpoint!r} returned a text block that is not "
                    f"JSON: {exc}. A typed endpoint must return its declared "
                    f"model serialised as JSON inside the text block."
                ) from exc

        if isinstance(block, Mapping):
            return dict(block)

        raise ConnectorContractError(
            f"endpoint {endpoint!r} returned {type(raw).__name__}, which is "
            f"not a recognisable MCP content result. Expected a list of "
            f"content blocks, or a structured-content mapping."
        )

    # -- introspection ---------------------------------------------------
    def names(self) -> list[str]:
        """Every endpoint the server exposes, sorted.

        This is the *server's* answer over the wire, not the client's
        expectation -- which is what makes the difference between the two
        worth checking.
        """
        return sorted(self._ensure_tools())

    def descriptor(self) -> SourceDescriptor:
        """How the server's source describes itself.

        Read from a real call's provenance, because the wire carries it and
        inventing one locally would defeat the point of the transport.
        """
        if self._descriptor is None:
            self.read("list_courses")
        if self._descriptor is None:  # pragma: no cover - defensive
            raise ConnectorTransportError(
                "the MCP server did not report a source descriptor"
            )
        return self._descriptor

    def server_names(self) -> list[str]:
        """What the server advertises, versus what the client expects.

        Returns:
            ``(server_only, client_only)``, each sorted. Both empty means the
            wire contract and the code agree.
        """
        server = set(self.names())
        client = set(self._contract.names())
        return sorted(server - client), sorted(client - server)

    def describe(self) -> str:
        """A human-readable summary for the notebook and the report."""
        return (
            f"McpConnectorClient -> {self._connection_name} over stdio, "
            f"{len(self._ensure_tools())} endpoint(s) advertised, "
            f"contract covers {len(self._contract.names())}"
        )

    def close(self) -> None:
        """Shut the server subprocess down and stop the loop thread.

        Idempotent, and safe to call on a client that never connected. This
        terminates the spawned server process, so a script that opens many
        clients -- which Lab 5's swap comparison does -- must close them.
        """
        if self._closed:
            return
        self._closed = True
        if self._release is not None:
            self._release.set()
        if self._loop_thread is not None:
            self._loop_thread.stop()
        self._loop_thread = None
        self._tools = {}

    def __enter__(self) -> "McpConnectorClient":
        """Start the session eagerly, so startup cost lands here.

        Worth using before a timed section: the first :meth:`read` otherwise
        pays for spawning the server, which is exactly the kind of one-off
        cost that makes a wall-clock comparison meaningless.
        """
        self._ensure_tools()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Close on the way out, including on an exception."""
        self.close()

    @property
    def call_count(self) -> int:
        """How many calls this client has made. For the lab's cost report."""
        return self._calls


def _descriptor_from_payload(
    endpoint: str, value: BaseModel
) -> SourceDescriptor:
    """Build a :class:`SourceDescriptor` from a result's provenance block.

    Args:
        endpoint: The endpoint that produced the value, for the error message.
        value: Its validated output model.

    Returns:
        A descriptor naming the source the server used.

    Raises:
        ConnectorContractError: If the result carries no provenance block. That
            is worth refusing: a connector that returns data with no record of
            where it came from cannot support the audit doc 2.3 requires.
    """
    provenance = getattr(value, "provenance", None)
    if provenance is None:
        raise ConnectorContractError(
            f"endpoint {endpoint!r} returned no provenance block, so the "
            f"client cannot say which source answered. Doc 2.3's audit log "
            f"depends on the source travelling with the data."
        )
    return SourceDescriptor(
        name=provenance.source,
        transport=provenance.source_transport,
        provenance=provenance.provenance,
        writable=False,
        requires_source_credentials=provenance.source.startswith("google"),
        swap_note="reported by the connector server over MCP",
    )
