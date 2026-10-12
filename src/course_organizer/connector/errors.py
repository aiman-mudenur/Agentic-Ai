"""Errors the connector raises, and why each one is its own type.

Every class here exists because an agent or a reviewer needs to tell these
failures apart. "The connector call failed" is not actionable; "your token
does not grant `submissions:read`" is.

Doc 2.10 Lab 5 acceptance: *"All data access goes through the connector;
swapping a source needs no agent change."* An error that cannot be classified
makes the second clause hard to check: a caller cannot tell a swap problem from
an authorisation problem without reading the source.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "ConnectorAuthError",
    "ConnectorContractError",
    "ConnectorError",
    "ConnectorNotFoundError",
    "ConnectorPolicyError",
    "ConnectorTransportError",
    "SourceUnavailableError",
    "WIRE_ERROR_RE",
    "tag_for_wire",
]


class ConnectorError(RuntimeError):
    """Base class for every connector failure.

    Catch this to mean "the governed surface refused or could not serve the
    call". The subclasses are the useful ones; this one exists so a caller can
    trap the whole family without knowing the taxonomy in advance.
    """


class ConnectorAuthError(ConnectorError):
    """No usable credential was presented.

    Raised for a missing token, an unknown token, and a malformed one. The
    three are deliberately the same type: an attacker should not be able to
    learn which of the three they got wrong, and a caller who fixes the
    problem does not need to know which flavour it was.
    """


class ConnectorPolicyError(ConnectorError):
    """The credential is valid but does not grant what was asked for.

    This is the error that makes *"access control per role"* (doc 2.3,
    Governance layer) a runtime property rather than a sentence in a design
    document. A student token asking for the cohort's submission history gets
    this, and the message names the scope that was missing and the scope the
    principal actually holds.
    """


class ConnectorNotFoundError(ConnectorError):
    """The requested endpoint, or a named entity inside it, does not exist.

    Kept distinct from a policy refusal on purpose: "you may not read that"
    and "there is no such thing" are different answers, and collapsing them
    turns a typo into an authorisation puzzle.
    """


class ConnectorContractError(ConnectorError):
    """A payload did not match the endpoint's declared type.

    The interesting one. MCP carries arguments and results as JSON inside text
    blocks, so a *correct* server and a *malicious or buggy* one look identical
    on the wire until something validates against a declared schema. Without
    this check an untyped dict flows into an agent and is treated as data.

    Raised on both directions of the call:

    * arguments that do not validate against the endpoint's input model, and
    * a result that does not validate against its output model.
    """


class ConnectorTransportError(ConnectorError):
    """The governed surface could not be reached at all.

    No server process, a crashed one, a closed pipe, a timeout. Distinct from
    every other error here because nothing about the *call* was wrong -- the
    channel was.
    """


class SourceUnavailableError(ConnectorError):
    """The connector is fine; the source behind it is not usable.

    The one error that belongs to the swappable layer. `SOURCE_ADAPTER=google`
    with no OAuth client configured raises this, and it says which setting is
    missing. An agent cannot fix it, but an operator can, and telling them
    which knob to turn is the difference between a five-minute fix and a
    support ticket.
    """


# ---------------------------------------------------------------------------
# Preserving the error *type* across a stdio process boundary
# ---------------------------------------------------------------------------
# Why this section exists, since it is the one non-obvious thing about the
# connector's error handling.
#
# `mcp.server.fastmcp.tools.base` line 117 is the whole problem:
#
#     except Exception as e:
#         raise ToolError(f"Error executing tool {self.name}: {e}") from e
#
# The protocol then carries that ToolError to the client as
# `CallToolResult(isError=True, content=[TextContent(text=...)])`. So what
# crosses the wire is the **message only**. Verified by running it, not by
# reading the source: a server-side `ConnectorPolicyError` arrives at the
# client inside a `langchain_mcp_adapters` `_MCPToolExecutionError` whose
# text names the student, the missing scope and the policy table -- and does
# not mention that it was a policy error at all.
#
# An earlier version of the client searched the message for a connector error
# class name and recovered the type that way. That was wrong twice over: it
# only worked if the message happened to contain the class name, and a message
# that *mentioned* a class name in prose would be misread as being that class.
#
# The fix is to make the tag explicit and machine-owned: the server writes
# `[ConnectorPolicyError]` in front of the message, and the client matches
# that exact bracketed form. Prose can then mention whatever it likes.
#
# This is not MCP structured error data, because `mcp` 1.30.0's stdio path does
# not carry a structured error payload for tool failures. It is a deliberate,
# documented convention between this project's two halves, and both halves
# read the constants below, so they cannot drift apart silently.

#: Finds the bracketed tag anywhere in a wire error message.
#:
#: Anchored nowhere on purpose. The text the client receives is
#: ``Error executing tool <name>: [ConnectorPolicyError] <message>``, because
#: the MCP layer prefixes its own wrapper around whatever the tool raised. A
#: ``match()`` against the start of the string would therefore never fire; an
#: earlier attempt to strip the prefix by splitting on it and re-matching the
#: fragments also failed, because the fragment begins with the *tool* name, not
#: with the tag. Searching is both simpler and correct.
#:
#: The tag is required to be followed by whitespace so that a bracketed word in
#: ordinary prose (``see [the rubric]``) is not mistaken for one. The client
#: additionally requires the captured name to be a connector error class, so a
#: stray match degrades to a transport error rather than a wrong type.
WIRE_ERROR_RE = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]\s")


def tag_for_wire(exc: BaseException) -> str:
    """Render an exception as a message a client can re-type.

    Args:
        exc: The exception to serialise. Only :class:`ConnectorError` and its
            subclasses carry a name the client can map back; anything else is
            tagged with its own type name, which the client will not recognise
            and will therefore report as a transport failure. That is the
            correct outcome -- an untagged, unrecognised server error is a bug
            or a crash, and must not masquerade as a policy decision.

    Returns:
        ``"[ClassName] message"``, the form the server puts on the wire.
    """
    return f"[{type(exc).__name__}] {exc}"


def untag_from_wire(text: str) -> tuple[Optional[str], str]:
    """Split a wire error message back into a type name and a body.

    Args:
        text: The text the client received, which may or may not carry a tag.

    Returns:
        ``(class_name_or_None, body)``. ``class_name`` is None when there is no
        tag, in which case ``body`` is the untouched text. A tag that is
        present but unrecognised still returns its name, so the caller can
        report the mismatch rather than silently dropping information.
    """
    match = WIRE_ERROR_RE.search(text)
    if not match:
        return None, text
    return match.group(1), text[match.end():]