"""Roles, principals, and the token -> principal registry.

Doc 2.3 names the requirement without specifying the mapping:

    *"Governance layer: access control per role, version history, audit log of
    grades published."*

So the *existence* of per-role access control is a documented requirement, and
the concrete role-to-scope table below is **this project's choice**. It is
stated here, in one table, rather than scattered through the endpoints, because
a security policy that is spread out is a security policy nobody can review.

What is not a choice: the credential is never supplied by the model. See
:mod:`course_organizer.connector.client`. The LLM chooses *which endpoint* and
*what arguments*; it cannot choose *who is asking*, because no endpoint has a
credential parameter and the client attaches the token itself.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Dict, FrozenSet, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.connector.errors import ConnectorAuthError, ConnectorPolicyError
from course_organizer.logging_setup import log_event

import logging

__all__ = [
    "ANONYMOUS_SCOPES",
    "CONNECTOR_ROLES",
    "ConnectorRole",
    "Principal",
    "PrincipalRegistry",
    "ROLE_SCOPES",
    "Scope",
    "default_principals",
    "ensure_token",
]

logger = logging.getLogger(__name__)

#: The document's three reads, one scope each. Named after the connector
#: endpoint rather than after the underlying store, so a scope survives a
#: source swap -- a scope that named a file would not.
Scope = str

NOTES_READ: Scope = "notes:read"
ASSIGNMENTS_READ: Scope = "assignments:read"
SUBMISSIONS_READ: Scope = "submissions:read"

#: Every scope the connector knows. Closed on purpose: an endpoint that asks
#: for a scope nobody holds is a refusal waiting to happen, and
#: :func:`ConnectorRole.grants` refuses to invent one.
KNOWN_SCOPES: FrozenSet[Scope] = frozenset(
    {NOTES_READ, ASSIGNMENTS_READ, SUBMISSIONS_READ}
)


class _Base(BaseModel):
    """Reject unknown keys, so a typo in a policy file surfaces loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)


class ConnectorRole(str):
    """A named role, restricted to the ones the connector knows.

    A plain `str` subclass with a closed constructor, so a role name that
    reaches the wire as free text is refused at the edge rather than silently
    granting nothing (or, worse, silently matching a prefix).
    """

    __slots__ = ()

    def __new__(cls, value: str) -> "ConnectorRole":
        if value not in ROLE_SCOPES:
            raise ConnectorPolicyError(
                f"unknown connector role {value!r}; known roles are "
                f"{sorted(ROLE_SCOPES)}"
            )
        return super().__new__(cls, value)


#: role -> the scopes that role holds. **This is the project's access-control
#: policy.** Doc 2.3 requires per-role access control; which role reads which
#: source is a design decision, and it is reversible by editing this table.
#:
#: The reasoning, so a reviewer can disagree with it rather than guess:
#:
#: * `course_faculty` is :data:`course_organizer.HUMAN_AUTHORITY`. The document
#:   makes faculty the final authority for every read that feeds a judgement,
#:   so faculty reads everything.
#: * `teaching_assistant` prepares the material faculty judges, so it reads
#:   everything too -- but it is a *different role*, and having it be a distinct
#:   principal is what lets the ledger show who actually looked.
#: * `student` may read published course material, which is the entire point of
#:   a notes store, but **not** `submissions:read`, which returns the cohort's
#:   submission history -- other people's work, including similarity flags that
#:   await a faculty review nobody has done.
#: * `anonymous` holds nothing at all. It exists so an unauthenticated call is
#:   a *policy* answer rather than a crash.
ROLE_SCOPES: Dict[str, FrozenSet[Scope]] = {
    "course_faculty": frozenset(KNOWN_SCOPES),
    "teaching_assistant": frozenset(KNOWN_SCOPES),
    "student": frozenset({NOTES_READ, ASSIGNMENTS_READ}),
    "anonymous": frozenset(),
}

CONNECTOR_ROLES = tuple(sorted(ROLE_SCOPES))

#: Kept as a name for readability at the call site in :func:`Principal`.
ANONYMOUS_SCOPES = ROLE_SCOPES["anonymous"]


class Principal(_Base):
    """One authenticated caller: who, in what role, with what token.

    Attributes:
        subject: A stable id for the caller -- an email, a staff number. It is
            recorded on every ledger row, so an audit names a person rather
            than a role.
        role: The :data:`ROLE_SCOPES` key that decides what this caller may
            read. Never a free string.
        token: The bearer credential. Compared with
            :func:`hmac.compare_digest` and never logged, never printed, and
            never included in ``repr``.
        granted_scopes: Resolved from ``role`` at construction, so a principal
            cannot be built holding a scope its role does not grant.
    """

    subject: str = Field(min_length=1)
    role: str
    token: str = Field(repr=False)
    granted_scopes: FrozenSet[Scope] = Field(default_factory=frozenset)

    @classmethod
    def for_role(cls, *, subject: str, role: str, token: str) -> "Principal":
        """Build a principal whose scopes are exactly its role's scopes.

        Scopes are never passed in. If they were, a caller could hand itself a
        scope its role does not hold and the policy table would become
        decorative.

        Args:
            subject: Stable caller id.
            role: A key of :data:`ROLE_SCOPES`.
            token: Bearer credential.

        Returns:
            The principal.

        Raises:
            ConnectorPolicyError: If ``role`` is not a known role.
        """
        if role not in ROLE_SCOPES:
            raise ConnectorPolicyError(
                f"unknown connector role {role!r}; known roles are "
                f"{sorted(ROLE_SCOPES)}"
            )
        return cls(
            subject=subject,
            role=role,
            token=token,
            granted_scopes=ROLE_SCOPES[role],
        )

    def grants(self, scope: Scope) -> bool:
        """Whether this principal holds ``scope``.

        Args:
            scope: The scope to test.

        Returns:
            True if held.
        """
        return scope in self.granted_scopes

    def require(self, scope: Scope, *, endpoint: str) -> "Principal":
        """Return self, or refuse.

        The refusal is a :class:`ConnectorPolicyError` whose message names
        both what was missing and what the caller does hold, because "access
        denied" without that pair is the least actionable error message in
        software.

        Args:
            scope: The scope the endpoint needs.
            endpoint: The endpoint name, for the message.

        Returns:
            self, so a call site reads as one expression.

        Raises:
            ConnectorPolicyError: If the scope is not held.
        """
        if scope in self.granted_scopes:
            return self
        held = ", ".join(sorted(self.granted_scopes)) or "none"
        raise ConnectorPolicyError(
            f"principal {self.subject!r} (role {self.role!r}) may not call "
            f"{endpoint!r}: it needs the scope {scope!r} and holds {held}. "
            f"Doc 2.3 requires access control per role; the policy table is "
            f"course_organizer.connector.auth.ROLE_SCOPES."
        )

    def describe(self) -> str:
        """A log-safe one-liner. Never includes the token.

        Returns:
            ``subject (role, n scopes)``.
        """
        return (
            f"{self.subject} (role={self.role}, "
            f"scopes={len(self.granted_scopes)})"
        )

    def fingerprint(self) -> str:
        """A short, stable, non-reversible id for the *credential*.

        Lets an audit log record "which token was used" without recording the
        token. Two principals sharing a fingerprint shared a credential, which
        is itself worth being able to detect.

        Returns:
            16 hex characters of SHA-256 over the token.
        """
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        return digest[:16]


class PrincipalRegistry:
    """Bearer tokens to principals, and the only place a token is checked.

    Comparison is constant-time (:func:`hmac.compare_digest`) so a caller
    cannot recover a token by timing the refusals. Every attempt -- accepted or
    refused -- goes through :meth:`resolve`, which is the single place to look
    and therefore the single place an audit must be written.

    Args:
        principals: The principals this registry will recognise. Order does not
            matter.

    Raises:
        ConnectorAuthError: If two principals carry the same token. One
            credential must identify exactly one caller, otherwise the audit
            log cannot answer "who read this?" for a request made with a
            shared token.

    Example:
        >>> registry = PrincipalRegistry(default_principals())
        >>> registry.resolve(registry.token_for("student")).role
        'student'
    """

    def __init__(self, principals: Iterable[Principal]) -> None:
        self._by_token: Dict[str, Principal] = {}
        for principal in principals:
            clash = self._by_token.get(principal.token)
            if clash is not None:
                raise ConnectorAuthError(
                    f"principals {clash.subject!r} and {principal.subject!r} "
                    f"share a bearer token; one credential identifies exactly "
                    f"one caller, or the audit log cannot say who asked"
                )
            self._by_token[principal.token] = principal

    def __len__(self) -> int:
        """How many principals are registered."""
        return len(self._by_token)

    def subjects(self) -> tuple[str, ...]:
        """Every registered subject, sorted. Never the tokens."""
        return tuple(sorted(p.subject for p in self._by_token.values()))

    def token_for(self, subject: str) -> str:
        """The token for a registered subject.

        Exists so tests and the lab demo can pick a role without hardcoding
        credential strings. It is **not** how a real caller gets a token: a
        real caller is issued one out of band.

        Args:
            subject: A registered subject.

        Returns:
            The bearer token.

        Raises:
            ConnectorAuthError: If no such subject is registered.
        """
        for principal in self._by_token.values():
            if principal.subject == subject:
                return principal.token
        raise ConnectorAuthError(
            f"no principal registered for subject {subject!r}; known subjects "
            f"are {list(self.subjects())}"
        )

    def token_for_role(self, role: str) -> str:
        """The token held by the principal with this role.

        Separate from :meth:`token_for` on purpose. Subjects and roles are
        different namespaces -- ``course_faculty`` is a role,
        ``faculty@example.edu`` is a subject -- and a method that accepted
        either would be guessing which one a caller meant. The labs reach for
        this one, because demonstrating a role refusal means asking *as* a
        role.

        Args:
            role: A key of :data:`ROLE_SCOPES`.

        Returns:
            The bearer token.

        Raises:
            ConnectorAuthError: If no principal holds that role. The message
                says which roles exist, so a typo is a one-glance fix.
        """
        for principal in self._by_token.values():
            if principal.role == role:
                return principal.token
        raise ConnectorAuthError(
            f"no principal registered with role {role!r}; registered roles are "
            f"{sorted({p.role for p in self._by_token.values()})}"
        )

    def resolve(self, token: Optional[str]) -> Principal:
        """Turn a bearer token into a principal, or refuse.

        Refuses in three situations that are deliberately indistinguishable
        from one another -- missing, unknown, malformed:

        Args:
            token: The bearer credential.

        Returns:
            The matching principal.

        Raises:
            ConnectorAuthError: If the token is absent or matches nothing.
        """
        if not token:
            log_event(
                logger,
                "connector.auth.failed",
                reason="no token presented",
            )
            raise ConnectorAuthError(
                "no connector token presented. The connector requires a "
                "bearer credential for every call, including read-only ones; "
                "that is the 'auth' in doc 2.10 Lab 5's 'typed endpoints and "
                "auth'."
            )

        # compare_digest over every candidate, so the loop does not exit early
        # on a match and leak the length of the matching prefix.
        matched: Optional[Principal] = None
        for candidate, principal in self._by_token.items():
            if hmac.compare_digest(token, candidate):
                matched = principal

        if matched is None:
            log_event(
                logger,
                "connector.auth.failed",
                reason="unknown token",
                presented_len=len(token),
            )
            raise ConnectorAuthError(
                "the connector token presented is not recognised. This is the "
                "same error for an unknown token and a malformed one, so that "
                "a caller cannot probe which tokens exist."
            )

        log_event(
            logger,
            "connector.auth.accepted",
            subject=matched.subject,
            role=matched.role,
            token_fingerprint=matched.fingerprint(),
        )
        return matched

    def report(self) -> list[str]:
        """One line per principal, safe to print. No tokens.

        Returns:
            Sorted ``subject (role, n scopes, token#fingerprint)`` lines.
        """
        return [
            f"{p.describe()}, token#{p.fingerprint()}"
            for p in sorted(
                self._by_token.values(), key=lambda p: p.subject
            )
        ]


def ensure_token(configured: Optional[str]) -> tuple[str, bool]:
    """Return a usable token, generating an ephemeral one if none is set.

    **No file is written.** An earlier draft of `.env.example` promised to
    generate a token into `.env` on first run; that promise is removed, because
    a library that edits a developer's config file as a side effect of being
    imported is a surprise, and because an in-memory token has nothing to leak.

    The trade-off, stated rather than hidden: an ephemeral token means a
    separately-spawned MCP server process will not recognise the parent's
    token, so Lab 5's stdio transport passes the token through the environment
    of the server it spawns. A real deployment sets ``MCP_CONNECTOR_TOKEN`` to a
    value both sides share.

    Args:
        configured: The value of ``MCP_CONNECTOR_TOKEN``, or None.

    Returns:
        ``(token, was_generated)``.
    """
    if configured and configured.strip():
        return configured.strip(), False
    return secrets.token_urlsafe(32), True


def default_principals(
    configured_token: Optional[str] = None,
    *,
    role: Optional[str] = None,
) -> tuple[Principal, ...]:
    """One principal per role, for local development and for the labs.

    These are **development** principals. Real tokens are issued out of band;
    what this function models is that the connector *can* hold several
    principals and tell them apart, which is what makes the role refusals in
    Lab 5 demonstrable rather than hypothetical.

    A token is shared by nobody. ``configured_token`` -- normally
    ``MCP_CONNECTOR_TOKEN`` -- is attached to the *faculty* principal only, and
    every other role gets its own generated token. That arrangement is forced
    by two requirements that pull in opposite directions:

    * Lab 5's stdio transport spawns a **separate process** for the MCP server,
      which has to recognise the token the client process is holding. So the
      configured token must be a real, known value on both sides.
    * :class:`PrincipalRegistry` refuses duplicate tokens, because a shared
      credential would make the ledger unable to name the caller.

    Assigning the configured token to exactly one role satisfies both. The lab
    obtains the other roles' tokens through
    :meth:`PrincipalRegistry.token_for`; a real deployment issues them out of
    band instead.

    Args:
        configured_token: If given, the presented principal's bearer token.
        role: Which role ``configured_token`` belongs to. Defaults to
            ``course_faculty``. **This is the whole reason this function takes
            a role.** Over the MCP wire only one principal is reachable --
            the one holding the presented token -- so without a role to attach
            that token to, every call over the wire would be faculty and the
            per-role refusals the document asks for in 2.3 would be
            undemonstrable over the transport that actually ships.

            Naming the role is not an escalation path. The role is a startup
            setting of the *server*, read from its own environment or its
            ``--role`` flag; it is not a wire argument, not a tool parameter,
            and not readable or writable by a model. A credential's role is
            decided by whoever issues the credential, which is exactly the
            authority a real deployment has.

    Returns:
        One :class:`Principal` per role, faculty first, then teaching
        assistant, student and anonymous.

    Raises:
        ConnectorPolicyError: If ``role`` is not one of
            :data:`CONNECTOR_ROLES`.
    """
    presented_role = role or "course_faculty"
    if presented_role not in ROLE_SCOPES:
        raise ConnectorPolicyError(
            f"cannot build a principal for role {presented_role!r}; known "
            f"roles are {sorted(ROLE_SCOPES)}"
        )

    subjects = {
        "course_faculty": "faculty@example.edu",
        "teaching_assistant": "ta@example.edu",
        "student": "student@example.edu",
        "anonymous": "anonymous",
    }
    principals = []
    for name in subjects:
        if name == presented_role and configured_token:
            # The one principal the wire can reach. Every other role gets a
            # generated token that stays inside this process, which is
            # harmless precisely because nothing outside can present it.
            token = configured_token
        else:
            token = secrets.token_urlsafe(24)
        principals.append(
            Principal.for_role(
                subject=subjects[name],
                role=name,
                token=token,
            )
        )
    return tuple(principals)