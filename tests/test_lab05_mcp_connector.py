"""Tests for doc 2.10 Lab 5: the connector, its server, and its boundary.

The lab's three claims, and where each is checked here:

1. *A connector server with typed endpoints and auth; agents call only through
   it.*  Sections 1-3 check the boundary holds structurally -- an `ast` walk
   over the package, so "no agent touches a source" is a property of the import
   graph rather than a claim in a report.
2. *All data access goes through the connector; swapping a source needs no agent
   change.*  Sections 4-6 run the same tool code against two sources.
3. *No agent should touch a source directly.*  Section 7 demonstrates the
   bypass, then proves two tests stop it from escaping into the package.

The MCP transport is exercised for real: a subprocess, a stdio pipe, the
protocol. Nothing here mocks the wire.
"""

from __future__ import annotations

import ast
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from course_organizer.connector import (
    DEFAULT_ENDPOINTS,
    build_default_connector,
    build_default_principals,
)
from course_organizer.connector.adapters import InMemoryAdapter, LocalSeedAdapter
from course_organizer.connector.auth import (
    CONNECTOR_ROLES,
    ROLE_SCOPES,
    Principal,
    PrincipalRegistry,
    default_principals,
    ensure_token,
)
from course_organizer.connector.client import (
    ConnectorContract,
    InProcessConnectorClient,
    McpConnectorClient,
    contract_from_registry,
    run_coroutine,
    server_command,
)
from course_organizer.connector.core import DOCUMENT_READ_PHRASES, EndpointRegistry
from course_organizer.connector.endpoints import Provenance
from course_organizer.connector.errors import (
    ConnectorAuthError,
    ConnectorContractError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
    ConnectorTransportError,
    SourceUnavailableError,
    tag_for_wire,
    untag_from_wire,
)
from course_organizer.connector.mcp_server import build_server
from course_organizer.connector.port import SourceAdapter
from course_organizer.tools import readers
from course_organizer.tools.compute import build_default_registry

PACKAGE = Path(readers.__file__).resolve().parents[1]

#: The four endpoints, which are the three the document names plus the one this
#: project added. Kept as a literal so a change to ``DEFAULT_ENDPOINTS`` fails
#: loudly here instead of being silently adopted.
EXPECTED_ENDPOINTS = {
    "list_courses",
    "read_assignment_rubrics",
    "read_notes_store",
    "read_submission_records",
}

#: Endpoint names from doc 2.7's ``connector.expose`` block.
DOC_ENDPOINTS = {
    "read_notes_store",
    "read_assignment_rubrics",
    "read_submission_records",
}


@pytest.fixture
def token() -> str:
    """A token no test shares, so the registries cannot cross-talk."""
    return secrets.token_urlsafe(24)


@pytest.fixture
def registry(token: str) -> EndpointRegistry:
    """A local-seed connector whose faculty token is known."""
    return build_default_connector(source="local", token=token)


@pytest.fixture
def mcp_client(registry: EndpointRegistry, token: str) -> Iterator[McpConnectorClient]:
    """A real MCP client over a real spawned server, closed afterwards."""
    client = McpConnectorClient(
        contract=contract_from_registry(registry), token=token, source="local"
    )
    try:
        yield client
    finally:
        client.close()


# ==========================================================================
# 1. The boundary: only the connector package may name a source
# ==========================================================================


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _source_of(path: Path) -> str:
    """A file's text, with any byte-order mark removed.

    Python's *import* machinery strips a leading U+FEFF, so a file carrying one
    imports cleanly and then behaves normally -- which is why ``lab01`` shipped
    with a BOM for a whole lab without any test noticing. ``ast.parse`` does not
    strip it, because it is handed a ``str``.

    Two consequences, and this helper addresses both:

    * the BOM itself was removed from ``labs/lab01_planner_loop.py``, which is
      the real fix;
    * the helper stays tolerant, because an AST test that dies with
      ``SyntaxError: invalid non-printable character U+FEFF`` reports an encoding
      artefact instead of the bypass it was written to find.
    """
    text = path.read_text(encoding="utf-8")
    return text[1:] if text.startswith("\ufeff") else text


def _tree_of(path: Path) -> ast.Module:
    """Parse a source file, naming it in any error so a failure is legible."""
    try:
        return ast.parse(_source_of(path), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a broken file is the news
        raise AssertionError(f"{path} does not parse: {exc}") from exc


def test_no_module_outside_connector_imports_a_source_store() -> None:
    """`tools.seed_store` is importable only by the connector package.

    This is the load-bearing test of the whole lab. Doc 2.10 Lab 5: *"All data
    access goes through the connector"* and *"No agent should touch a source
    directly; a bypass defeats governance later."*

    Checked by `ast` rather than by importing and inspecting, so a module that
    imports the store *inside a function* is caught too -- which a runtime check
    would miss, and which is exactly how a "temporary" bypass gets written.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path.parent.name == "connector":
            continue
        tree = _tree_of(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if "seed_store" in target:
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "seed_store" in alias.name:
                        offenders.append(
                            f"{path.relative_to(PACKAGE)}:{node.lineno}"
                        )
    assert not offenders, (
        "only course_organizer.connector may import tools.seed_store; found "
        f"{offenders}"
    )


def test_no_module_outside_connector_constructs_a_source_adapter() -> None:
    """An agent must not be able to reach past the connector to the source.

    Importing the store is one bypass; instantiating an adapter directly is
    another, and the second does not need the first.
    """
    offenders: list[str] = []
    adapter_names = {"LocalSeedAdapter", "GoogleWorkspaceAdapter", "InMemoryAdapter"}
    for path in _python_files():
        if path.parent.name == "connector":
            continue
        tree = _tree_of(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in adapter_names:
                    offenders.append(
                        f"{path.relative_to(PACKAGE)}:{node.lineno} {name}()"
                    )
            elif isinstance(node, ast.Name) and node.id in adapter_names:
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {node.id}")
    assert not offenders, (
        f"source adapters are the connector's business only: {offenders}"
    )


def test_the_readers_module_does_not_reach_for_a_store() -> None:
    """`readers.py` is the module Lab 5 rewrote, so check it by name too.

    A cheap, very specific assertion next to the general one, so that when
    someone adds a store reference there the failure names the file rather than
    making the reader reason about the whole package.

    Checked against the **AST**, not the text. `readers.py` discusses the change
    it made -- its docstring explains what the readers used to call and why they
    no longer do -- so a substring search reports the documentation of the fix as
    the fix. That is not a hypothetical: it is what the first version of this
    test did, and it failed on `readers.py`'s own docstring.
    """
    path = PACKAGE / "tools" / "readers.py"
    banned = {"get_seed_store", "LocalSeedStore", "SeedStoreError", "build_source_adapter"}
    offenders: list[str] = []
    for node in ast.walk(_tree_of(path)):
        # A bare name: `get_seed_store()`.
        if isinstance(node, ast.Name) and node.id in banned:
            offenders.append(f"line {node.lineno}: {node.id}")
        # An attribute: `store.relative(...)`, or a re-import inside a function.
        elif isinstance(node, ast.Attribute) and node.attr in banned:
            offenders.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "seed_store"
        ):
            offenders.append(f"line {node.lineno}: import from {node.module}")
    assert not offenders, f"readers.py must read through the connector: {offenders}"

    # And the positive half: it does hold a connector, and it is used.
    assert "connector" in _source_of(path)
    tree = _tree_of(path)
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "use_connector"
        for node in tree.body
    ), "the swap seam (use_connector/reset_connector) is the documented entry point"


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Ids of the string nodes that are docstrings.

    A docstring is an ``ast.Constant`` like any other, so a test that walks for
    suspicious string literals will find the module's own documentation and
    fail on it. ``memory/stores.py``'s docstring quotes ``data/seed/...``
    because it describes the Lab 4 requirement it implements -- which is the
    documentation *doing its job*, not a hardcoded path.

    Args:
        tree: A parsed module.

    Returns:
        ``id()`` of every constant that is the sole expression in a docstring
        position (module, class, function, or nested function).
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def test_memory_names_its_allow_list_through_the_connector_not_the_store() -> None:
    """`memory/stores.py` once held a second front door; close it by name.

    Regression. `ArchiveStore` originally resolved its allowed-provenance set
    from ``LocalSeedStore.archive_sources()`` and built its two provenance
    strings from ``store.relative(...)``. Both are *reads of the corpus from
    outside the connector*, which is the bypass doc 2.10 Lab 5 forbids -- and a
    worse one than an agent's, because the set it produced is the allow-list that
    `MemoryStore.append` enforces. A layer that can name its own allow-list
    without authorisation could name a wider one and quietly weaken the check it
    exists to perform.

    Checked three ways, because the ways this can come back are different: an
    import, a constructed adapter, and a hardcoded path.
    """
    path = PACKAGE / "memory" / "stores.py"
    tree = _tree_of(path)
    relative = path.relative_to(PACKAGE).as_posix()

    # 1. No import of the seed store, anywhere in the file.
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and "seed_store" in (node.module or ""):
            offenders.append(f"{relative}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "seed_store" in alias.name:
                    offenders.append(f"{relative}:{node.lineno} import {alias.name}")
    assert not offenders, offenders

    # 2. No adapter constructed here either.
    adapters = {"LocalSeedAdapter", "GoogleWorkspaceAdapter", "InMemoryAdapter"}
    built = [
        f"{relative}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) in adapters
    ]
    assert not built, built

    # 3. No hardcoded corpus path. The provenance must arrive from the connector's
    #    own report; a literal here would let a source swap keep reporting the old
    #    location, which is the failure a swap is supposed to expose.
    #    Docstrings are excluded -- see `_docstring_constants`.
    docstrings = _docstring_constants(tree)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and ("data/seed" in node.value or node.value.endswith(".json"))
    ]
    assert not literals, literals

    # And positively: the file does route through the connector's reader seam.
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "course_organizer.tools.readers" in imported, sorted(imported)


def test_the_archives_allow_list_is_exactly_what_the_connector_reported(
    token: str,
) -> None:
    """Governance decides the allow-list, and the result is the union of three.

    Behavioural companion to the AST test above: it proves the rerouting still
    *works*, and that the allow-list is precisely the provenance of the reads the
    archive is built from -- no wider (which would weaken the check) and no
    narrower (which would refuse records the connector legitimately served).

    A test that only inspected imports would still pass if `_resolve_sources`
    returned `frozenset()`, because nothing about an empty allow-list is a
    syntax error.
    """
    from course_organizer.memory.stores import ArchiveStore

    registry = build_default_connector(source="local", token=token)
    readers.use_connector(registry, token)
    try:
        expected: set[str] = set()
        for endpoint in ArchiveStore._ARCHIVE_ENDPOINTS:
            value = registry.invoke(
                endpoint, {"course_code": "CS201"}, token=token
            ).unwrap()
            expected.update(value.provenance.sources)

        archive = ArchiveStore(run_id="t-allow", course_code="CS201")
        assert set(archive.allowed_provenance) == expected, (
            f"archive={sorted(archive.allowed_provenance)} "
            f"connector={sorted(expected)}"
        )
        assert expected, "the connector reported no provenance, so this proves nothing"
        assert archive.sources == sorted(expected)
    finally:
        readers.reset_connector()


def test_every_adapter_satisfies_the_port() -> None:
    """The Protocol is a real check, not decoration.

    `@runtime_checkable` verifies attribute *presence* only, so this catches an
    adapter missing a method -- which would otherwise surface as an
    `AttributeError` inside a tool call rather than as a build failure.
    """
    for adapter in (LocalSeedAdapter(), InMemoryAdapter()):
        assert isinstance(adapter, SourceAdapter), type(adapter).__name__


def test_documentation_is_decided_by_the_documents_words_not_by_a_label(
    registry: EndpointRegistry,
) -> None:
    """Regression: `added_by_lab` was once mistaken for "is it required?".

    `EndpointSpec.added_by_lab` defaulted to ``"Lab 5"``, and Lab 5 implemented
    all four endpoints -- so all four carried it, and the field could not
    distinguish the three reads the document asks for from the one this project
    added. Anything reading it to decide requirement status then reported the
    three documented endpoints as additions, which is the exact inversion a
    traceability matrix exists to prevent.

    `EndpointSpec.documented` now matches `doc_read` against
    `DOCUMENT_READ_PHRASES` and nothing else.
    """
    assert registry.get("read_notes_store").documented is True
    assert registry.get("read_assignment_rubrics").documented is True
    assert registry.get("read_submission_records").documented is True
    assert registry.get("list_courses").documented is False

    # The added endpoint says so, in one line a reviewer can read.
    origin = registry.get("list_courses").origin
    assert "not a documented requirement" in origin
    assert "Lab 5" in origin

    # And the reason cannot be asserted into existence: an endpoint whose
    # doc_read happens to name a phrase is not thereby documented, because the
    # check is an exact membership test against the document's own three lines.
    spec = registry.get("list_courses")
    assert spec.doc_read not in DOCUMENT_READ_PHRASES
    assert spec.documented is False


def test_which_lab_wrote_an_endpoint_is_not_the_same_question(
    registry: EndpointRegistry,
) -> None:
    """`added_by_lab` still answers its own question, and only its own.

    Every endpoint says ``"Lab 5"`` because Lab 5 wrote every endpoint. That is
    a true and useful fact, and it is deliberately *not* the fact
    :attr:`EndpointSpec.documented` reports.
    """
    labs = {registry.get(n).added_by_lab for n in EXPECTED_ENDPOINTS}
    assert labs == {"Lab 5"}, labs
    documented = {n for n in EXPECTED_ENDPOINTS if registry.get(n).documented}
    assert documented == DOC_ENDPOINTS, (
        "the two questions must give different answers, or one of them is not "
        "asking anything"
    )


def test_every_endpoint_reports_where_its_data_came_from(
    registry: EndpointRegistry, token: str
) -> None:
    """No endpoint may return a result whose provenance is blank.

    Regression. Two separate defects made ``list_courses`` the one endpoint that
    could not account for itself:

    * ``list_courses``' handler called ``_provenance(...)`` without
      ``adapter=``, so ``_backing`` was handed ``None`` and returned ``[]``.
      The provenance block still looked complete -- every other field was
      populated -- so nothing about the *shape* hinted at the gap.
    * ``LocalSeedAdapter.backing`` returned ``[]`` for ``kind="courses"``
      regardless, because it only knew per-course file resolvers.

    A blank provenance is worse than a wrong one, because it is
    indistinguishable from an adapter that simply forgot to answer, and a
    reviewer reading the audit cannot tell which they are looking at.
    """
    for endpoint in sorted(EXPECTED_ENDPOINTS):
        value = registry.invoke(endpoint, dict(_payload(endpoint)), token=token).unwrap()
        assert value.provenance.sources, (
            f"{endpoint} reported no source location; every read must be "
            f"attributable"
        )
        assert all(isinstance(s, str) and s for s in value.provenance.sources)

    listed = registry.invoke("list_courses", {}, token=token).unwrap()
    assert listed.provenance.sources == ["data/seed"], (
        "enumerating courses reads the corpus root, which is the one thing it "
        "can honestly name"
    )


def test_the_local_adapter_can_name_every_kind_the_connector_asks_about(
    token: str,
) -> None:
    """`backing()` answers for all three connector vocabularies, plus courses.

    A `kind` the adapter does not recognise returns ``[]`` -- see the next
    test -- so this one exists to make the *known* kinds a deliberate list
    rather than an accident of which dict lookups happen to be present.
    """
    adapter = LocalSeedAdapter()
    assert adapter.backing("notes", "CS201") == [
        "data/seed/data_structures/notes.json"
    ]
    assert adapter.backing("assignments", "CS201") == [
        "data/seed/data_structures/assignments.json"
    ]
    assert adapter.backing("submissions", "CS201") == [
        "data/seed/data_structures/submissions.json"
    ]
    assert adapter.backing("courses", None) == ["data/seed"]


def test_an_unknown_kind_reports_nothing_rather_than_guessing(
    token: str,
) -> None:
    """Silence is the correct answer for a kind the source cannot place.

    Asserted because it is the tempting thing to get wrong: returning the
    corpus root for every unknown kind would make *every* read look attributed
    while saying nothing true.
    """
    assert LocalSeedAdapter().backing("gradebook", "CS201") == []


def _payload(endpoint: str) -> dict[str, Any]:
    """A minimal valid payload for one endpoint.

    Args:
        endpoint: Which endpoint.

    Returns:
        Its required arguments, and nothing optional.
    """
    if endpoint == "list_courses":
        return {}
    return {"course_code": "CS201"}


# ==========================================================================
# 2. The endpoints the document names
# ==========================================================================


def test_default_endpoints_are_the_three_document_reads_plus_one() -> None:
    assert set(DEFAULT_ENDPOINTS) == EXPECTED_ENDPOINTS
    assert DOC_ENDPOINTS <= set(DEFAULT_ENDPOINTS)


def test_every_endpoint_declares_a_scope_and_a_doc_reference(
    registry: EndpointRegistry,
) -> None:
    """Auth and traceability cannot be "added later" if they are required here.

    Doc 2.3's governance layer asks for *"access control per role"*, and doc
    2.10 Lab 5's deliverable says *"typed endpoints and auth"*. An endpoint with
    no scope would be callable by anyone, so the absence is a defect, not a
    default.
    """
    for spec in registry:
        assert spec.scope, f"{spec.name} has no scope"
        assert spec.doc_ref, f"{spec.name} has no doc_ref"
        assert spec.input_model is not None, spec.name
        assert spec.output_model is not None, spec.name


def test_the_three_document_reads_return_their_documented_thing(
    registry: EndpointRegistry, token: str
) -> None:
    """Doc 2.7's three reads, each returning what its name promises."""
    notes = registry.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=token
    ).unwrap()
    assert notes.note_count == 8
    assert notes.total_versions == 10
    assert notes.declared_units == ["1", "2", "3", "4", "5"]
    assert notes.title == "Data Structures", (
        "the course name comes from the header's course_name, not a fallback"
    )

    rubrics = registry.invoke(
        "read_assignment_rubrics", {"course_code": "CS201"}, token=token
    ).unwrap()
    assert rubrics.assignment_count == 3
    assert rubrics.rubric_criteria == 9

    subs = registry.invoke(
        "read_submission_records", {"course_code": "CS201"}, token=token
    ).unwrap()
    assert subs.submission_count == 8
    assert subs.late_count == 1
    assert [s.submission_id for s in subs.submissions if s.is_late] == ["DS-A3-S008"]


def test_a_datetime_survives_the_wire_as_a_datetime(mcp_client: McpConnectorClient) -> None:
    """The round trip must not flatten a timestamp to a string.

    Worth its own test because the failure is quiet: a string still *compares*
    acceptably in a formatted report, and only a strict type check catches it
    before it reaches something that does arithmetic on the value.
    """
    from datetime import datetime

    result = mcp_client.read("read_submission_records", {"course_code": "CS201"})
    first = result.submissions[0]
    assert isinstance(first.submitted_at, datetime), type(first.submitted_at)
    assert first.submitted_at.tzinfo is not None, "an aware datetime must stay aware"

    notes = mcp_client.read("read_notes_store", {"course_code": "CS201"})
    assert isinstance(notes.notes[0].versions[0].created_at, datetime)


# ==========================================================================
# 3. Auth: roles, scopes, and refusals
# ==========================================================================


def test_the_role_scope_table_is_stated_and_closed() -> None:
    """The policy is one table, and it is the project's own choice.

    Doc 2.3 requires per-role access control but does not say which role reads
    what. That decision is recorded in ``ROLE_SCOPES`` and nowhere else, so a
    reviewer can disagree with it by editing one table.
    """
    assert set(ROLE_SCOPES) == set(CONNECTOR_ROLES)
    assert ROLE_SCOPES["course_faculty"] >= ROLE_SCOPES["student"]
    assert "submissions:read" in ROLE_SCOPES["course_faculty"]
    assert "submissions:read" not in ROLE_SCOPES["student"], (
        "a student may read course material but not the cohort's submission "
        "history, which is other people's work"
    )
    assert ROLE_SCOPES["anonymous"] == frozenset()


def test_a_student_is_refused_submissions_and_allowed_notes(
    registry: EndpointRegistry, token: str
) -> None:
    principals = registry.principal_registry
    student = principals.token_for_role("student")
    allowed = registry.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=student
    ).unwrap()
    assert allowed.note_count == 8
    with pytest.raises(ConnectorPolicyError) as excinfo:
        registry.invoke(
            "read_submission_records", {"course_code": "CS201"}, token=student
        )
    assert "submissions:read" in str(excinfo.value)
    assert "student" in str(excinfo.value)


def test_anonymous_is_refused_everything(registry: EndpointRegistry) -> None:
    """An unauthenticated-looking call gets a policy answer, not a crash.

    Having a principal that holds nothing makes "you may not" a runtime property
    of the system rather than a sentence in a design document.
    """
    token = registry.principal_registry.token_for_role("anonymous")
    with pytest.raises(ConnectorPolicyError):
        registry.invoke("list_courses", {}, token=token)


def test_a_missing_and_an_unknown_token_are_the_same_error(
    registry: EndpointRegistry, token: str
) -> None:
    """Indistinguishable on purpose: no probing for which tokens exist."""
    with pytest.raises(ConnectorAuthError):
        registry.invoke("list_courses", {}, token=None)
    with pytest.raises(ConnectorAuthError):
        registry.invoke("list_courses", {}, token="not-a-real-token-anywhere")
    # And a valid token still works, so the refusals above are not vacuous.
    assert registry.invoke("list_courses", {}, token=token).unwrap().source_count == 2


def test_two_principals_never_share_a_token(token: str) -> None:
    """A shared credential would make the ledger unable to name the caller."""
    principals = default_principals(token)
    tokens = [p.token for p in principals]
    assert len(set(tokens)) == len(tokens), "duplicate bearer tokens"
    assert principals[0].token == token, "the configured token is the faculty one"
    registry = PrincipalRegistry(principals)
    for principal in principals:
        assert registry.resolve(principal.token).subject == principal.subject


def test_a_duplicate_token_is_refused_rather_than_accepted() -> None:
    """The invariant is enforced, not merely documented."""
    doubled = default_principals("shared")
    clashing = list(doubled)
    clashing[1] = Principal.for_role(
        subject=clashing[1].subject, role=clashing[1].role, token="shared"
    )
    with pytest.raises(ConnectorError, match="(?i)share a bearer token"):
        PrincipalRegistry(clashing)


def test_an_unknown_role_cannot_be_constructed() -> None:
    """A role name from the wire is refused at the edge, not half-matched."""
    from course_organizer.connector.auth import ConnectorRole

    with pytest.raises(ConnectorPolicyError, match="(?i)unknown connector role"):
        ConnectorRole("course_faculty_admin")


def test_ensure_token_never_writes_a_file(tmp_path: Path) -> None:
    """The corrected `.env.example` promise, asserted.

    An earlier draft of `.env.example` said a token would be "generated into
    `.env` on first run". ``ensure_token`` deliberately does not, and this test
    exists so that promise cannot be reintroduced silently.
    """
    before = sorted(p.name for p in tmp_path.iterdir())
    token, generated = ensure_token(None)
    assert generated is True
    assert token.strip()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    configured, generated_again = ensure_token(token)
    assert configured == token and generated_again is False


def test_no_credential_reaches_the_ledger_or_a_payload(
    registry: EndpointRegistry, token: str
) -> None:
    """A token must never appear in anything a model could read.

    The ledger is the audit log, and a ledger containing bearer tokens is a
    credential store wearing a log's name.
    """
    registry.invoke("read_notes_store", {"course_code": "CS201"}, token=token)
    report = registry.ledger.report()
    assert token not in json.dumps(report, default=str)
    for row in registry.ledger:
        assert token not in str(row.to_dict() if hasattr(row, "to_dict") else row)
    # Nor in the payload the agent receives.
    result = registry.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=token
    ).unwrap()
    assert token not in result.model_dump_json()


def test_principal_repr_never_shows_the_token(token: str) -> None:
    principal = Principal.for_role(subject="s", role="course_faculty", token=token)
    assert token not in repr(principal)
    assert token not in f"{principal}"


# ==========================================================================
# 4. Provenance travels with the data
# ==========================================================================


def test_provenance_records_the_caller_and_the_source(
    registry: EndpointRegistry, token: str
) -> None:
    result = registry.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=token
    ).unwrap()
    provenance = result.provenance
    assert isinstance(provenance, Provenance)
    assert provenance.source == "local_seed"
    assert provenance.endpoint == "read_notes_store"
    assert provenance.fetched_by == "faculty@example.edu"
    assert provenance.role == "course_faculty"


def test_provenance_names_the_concrete_file_behind_a_read(
    registry: EndpointRegistry, token: str
) -> None:
    """The audit trail must be specific enough to check.

    Lab 2's tools used to assemble this by naming a seed file directly. Going
    through the connector means the *source* has to say where it read from, or
    the field would have had to be dropped.
    """
    result = registry.invoke(
        "read_notes_store", {"course_code": "CS201"}, token=token
    ).unwrap()
    assert result.provenance.sources, "an audit trail that names no file is empty"
    assert result.provenance.sources[0].endswith("notes.json")
    assert "data_structures" in result.provenance.sources[0]


def test_provenance_changes_when_the_source_changes(token: str) -> None:
    """Two sources, two provenances -- the evidence that a swap really swapped."""
    local = build_default_connector(source="local", token=token)
    memory = build_default_connector(source="in_memory", token=token)
    a = local.invoke("read_notes_store", {"course_code": "CS201"}, token=token).unwrap()
    b = memory.invoke("read_notes_store", {"course_code": "ZZ999"}, token=token).unwrap()
    assert a.provenance.source == "local_seed"
    assert b.provenance.source == "in_memory"
    assert a.provenance.sources != b.provenance.sources


# ==========================================================================
# 5. Refusals that would otherwise look like answers
# ==========================================================================


def test_an_unknown_filter_is_refused_with_the_real_values(
    registry: EndpointRegistry, token: str
) -> None:
    """An empty list is a claim a grader might act on, so it must not be faked.

    Both of these would otherwise return `[]`, which reads as "this course has
    no notes in unit 'Unit 1'" -- a sentence that is wrong, and plausible.
    """
    with pytest.raises(ConnectorNotFoundError) as excinfo:
        registry.invoke(
            "read_notes_store",
            {"course_code": "CS201", "unit": "Unit 1"},
            token=token,
        )
    message = str(excinfo.value)
    assert "'Unit 1'" in message
    assert "1" in message, "the real unit ids must be named"

    with pytest.raises(ConnectorNotFoundError):
        registry.invoke(
            "read_submission_records",
            {"course_code": "CS201", "assignment_id": "CS201-A1"},
            token=token,
        )


def test_an_unknown_course_is_refused_and_names_what_exists(
    registry: EndpointRegistry, token: str
) -> None:
    with pytest.raises(ConnectorNotFoundError) as excinfo:
        registry.invoke("read_notes_store", {"course_code": "NOPE"}, token=token)
    assert "CS201" in str(excinfo.value)


def test_arguments_are_validated_against_the_declared_model(
    registry: EndpointRegistry, token: str
) -> None:
    with pytest.raises(ConnectorContractError):
        registry.invoke("read_notes_store", {"course_code": ""}, token=token)
    with pytest.raises(ConnectorContractError):
        registry.invoke("read_notes_store", "not-a-mapping", token=token)
    with pytest.raises(ConnectorNotFoundError):
        registry.invoke("read_everything", {}, token=token)


def test_the_ledger_counts_records_correctly(registry: EndpointRegistry, token: str) -> None:
    """An audit log that undercounts looks correct, which is the danger.

    Regression for a real bug: `_count_records` used "the first list-valued
    field", which found `declared_units` (5) instead of `notes` (8).
    """
    counts = {}
    for endpoint, payload in (
        ("read_notes_store", {"course_code": "CS201"}),
        ("read_assignment_rubrics", {"course_code": "CS201"}),
        ("read_submission_records", {"course_code": "CS201"}),
        ("list_courses", {}),
    ):
        registry.invoke(endpoint, payload, token=token)
        last = registry.ledger.last()
        assert last is not None
        counts[endpoint] = last.records
    assert counts == {
        "read_notes_store": 8,
        "read_assignment_rubrics": 3,
        "read_submission_records": 8,
        "list_courses": 2,
    }
    assert registry.ledger.total_records() == 8 + 3 + 8 + 2


def test_every_call_is_ledgered_with_its_outcome(
    registry: EndpointRegistry, token: str
) -> None:
    """Three calls, three outcomes, three rows -- refusals included.

    An audit log that only records successes cannot answer "did anyone try to
    read the cohort's submissions without permission?", which is the question
    doc 2.3's *\"audit log of grades published\"* exists to make answerable.
    """
    registry.invoke("read_notes_store", {"course_code": "CS201"}, token=token)
    student = registry.principal_registry.token_for_role("student")
    with pytest.raises(ConnectorPolicyError):
        registry.invoke("read_submission_records", {"course_code": "CS201"}, token=student)
    # A bad credential is an auth failure, not a not-found. Distinct types so a
    # caller can tell "you may not" from "it is not there".
    with pytest.raises(ConnectorAuthError):
        registry.invoke("read_notes_store", {"course_code": "CS201"}, token="bogus")

    ledger = registry.ledger
    assert len(ledger) == 3
    assert ledger.allowed("read_notes_store") is True
    assert ledger.allowed("read_submission_records") is False
    assert len(ledger.refusals()) == 2, "both refusals are on the record"
    assert ledger.failures() == [], "a refusal is not a crash"
    assert set(ledger.principals()) == {
        "faculty@example.edu",
        "student@example.edu",
        "<unauthenticated>",
    }, "the ledger names who asked, including for the refused calls"
    # The refusal row records which scope was missing, which is what makes it
    # actionable rather than merely a count.
    policy_rows = [c for c in ledger.refusals() if c.error]
    assert any("submissions:read" in c.error for c in policy_rows)


# ==========================================================================
# 6. The swap: same agent code, different source
# ==========================================================================


def test_swapping_the_source_changes_no_agent_code(token: str) -> None:
    """The acceptance criterion, run end to end through the *tools*.

    *"Swapping a source needs no agent change."* The claim is only meaningful if
    the same `ToolRegistry` invocation produces different data with no edit
    between the two runs, which is what this does.
    """
    seen: list[tuple[str, str, int]] = []
    for source, course in (("local", "CS201"), ("in_memory", "ZZ999")):
        connector = build_default_connector(source=source, token=token)
        readers.use_connector(connector, token)
        try:
            tools = build_default_registry(run_id=f"swap-{source}")
            result = tools.invoke("read_notes_store", {"course_code": course})
            assert result.ok, result.error
            value = result.value
            seen.append((value.course_name, value.course_code, value.note_count))
            # The provenance follows the source, which is how a reader knows.
            assert value.source_files, "a read must say where it came from"
        finally:
            readers.reset_connector()

    assert seen[0] == ("Data Structures", "CS201", 8)
    assert seen[1] == ("In-memory exchange course", "ZZ999", 1)


def test_the_swapped_source_really_is_read(token: str) -> None:
    """`InMemoryAdapter.read_count` is positive, so the swap was not cosmetic."""
    adapter = InMemoryAdapter()
    connector = build_default_connector(
        source="in_memory", token=token, transport=None
    )
    # The connector builds its own adapter, so check that one rather than the
    # one constructed above: what matters is the adapter actually serving calls.
    assert isinstance(connector.adapter, InMemoryAdapter)
    assert adapter.read_count == 0
    connector.invoke("read_notes_store", {"course_code": "ZZ999"}, token=token)
    assert connector.adapter.read_count > 0, "the in-memory adapter was bypassed"


def test_a_source_that_cannot_be_built_says_which_setting_is_missing() -> None:
    """`SourceUnavailableError` names the knob, so a fix takes minutes.

    Doc 2.10 Lab 5's swap is only real if the *other* source can be named.
    """
    with pytest.raises(SourceUnavailableError) as excinfo:
        build_default_connector(source="google")
    message = str(excinfo.value)
    assert "SOURCE_ADAPTER" in message or "oauth" in message.lower()
    assert "local" in message, "the message should point at the working default"


# ==========================================================================
# 7. The pitfall: an agent that bypasses the connector
# ==========================================================================


class BypassingReader:
    """Reads the corpus directly, as a real agent would and should not.

    Exists to make doc 2.10 Lab 5's pitfall concrete: *"No agent should touch a
    source directly; a bypass defeats governance later."*

    It is a faithful simulation, not a strawman. It calls the same
    ``get_seed_store()`` the tools used to call, and it gets real data that is
    real data -- the notes, the rubric, the cohort's submissions. Nothing about
    the data is wrong. What is wrong is everything *around* it:

    * no role check, so a student principal reads other people's submissions;
    * no ledger row, so the audit log has a hole exactly where the read
      happened;
    * no provenance, so the answer cannot say where it came from.

    Confined to this test module on purpose. Two tests below assert that, so a
    bypass cannot be quietly promoted into the shipped package.
    """

    def __init__(self, seed_dir: str | os.PathLike[str] | None = None) -> None:
        from course_organizer.tools.seed_store import get_seed_store

        self._store = get_seed_store(seed_dir)

    def read_submissions(self, course_code: str) -> list[Any]:
        """The cohort's submissions, with no policy in the way."""
        return self._store.read_submissions(course_code)


def test_the_bypass_returns_real_data_with_no_governance(token: str) -> None:
    """The temptation is real, which is the point of demonstrating it.

    The bypass succeeds. That is what makes the pitfall worth a section in the
    document: a bypass is not an error you can detect by watching for errors.
    """
    bypass = BypassingReader()
    records = bypass.read_submissions("CS201")
    assert len(records) == 8, "the bypass really does read the cohort's history"

    # Same data, through the connector, with the governance the bypass skipped.
    connector = build_default_connector(source="local", token=token)
    student = connector.principal_registry.token_for_role("student")
    with pytest.raises(ConnectorPolicyError):
        connector.invoke("read_submission_records", {"course_code": "CS201"}, token=student)
    assert len(connector.ledger.refusals()) == 1, "and the refusal is on the record"

    # The bypass left no trace at all. That is the difference: the governed read
    # that was *denied* is auditable, and the ungoverned read that *succeeded*
    # is not.
    assert len(connector.ledger) == 1, "the bypassed read is invisible to the ledger"


def test_the_bypass_class_does_not_escape_into_the_package() -> None:
    """Keep the demonstration in the demonstration."""
    offenders = [
        str(path.relative_to(PACKAGE))
        for path in _python_files()
        if "BypassingReader" in _source_of(path)
    ]
    assert not offenders, (
        f"the pitfall demo belongs in the test module, not in {offenders}"
    )


#: Call *names* that read or write bytes. Bare or module-qualified forms both
#: count: ``open(path)`` and ``os.open(path, ...)`` are the same act, and
#: ``audit.py`` needs the second precisely because it wants ``O_APPEND``.
_IO_VERBS = {"open", "read_text", "read_bytes", "write_text", "write_bytes"}

#: Serialisation verbs that take a *file object*: ``json.load(f)`` reads bytes
#: from disk, ``json.loads(s)`` parses a string that is already in memory. Only
#: the two-word forms are filesystem access, and counting the four-word forms
#: flagged eleven innocent ``json.dumps`` calls across the package -- the sort of
#: noise that makes an invariant worthless.
_CODECS = {"json", "pickle", "yaml", "tomllib", "marshal", "dill"}

#: The files in the package that own durable state, and the ones that write a
#: file as part of a deliverable. Named one at a time, with the reason, so that a
#: *new* filesystem call anywhere -- including inside these -- still fails.
_DURABILITY_OWNERS = {
    "runtime/audit.py": "the hash-chained JSONL log; its whole purpose is the file",
    "runtime/checkpoint.py": "the SQLite checkpoint store; its whole purpose is the file",
    "labs/lab06_governed_runtime.py": (
        "writes the crash child program so the exact process that dies can be read"
    ),
    "graph/nodes.py": (
        "the publish node writes the approved workspace document; that document "
        "is Lab 7's deliverable, not an incidental cache"
    ),
    "labs/lab07_node_graph.py": (
        "writes the optional --json acceptance-check dump the CLI offers"
    ),
    "graph/swarm.py": (
        "writes each candidate's own evidence record before the merge, so the "
        "rejected candidates are logged to disk and not just to memory"
    ),
    "labs/lab08_parallel_swarm.py": (
        "reads the published document back to prove the approval marker, and "
        "writes the optional --json acceptance-check dump the CLI offers"
    ),
}

#: How many filesystem calls each owner is expected to make. Checked, so the
#: allowlist cannot quietly grow into a blank cheque.
_EXPECTED_IO_CALLS = {
    "runtime/audit.py": 3,  # two reads to verify the chain, one append to write
    "runtime/checkpoint.py": 0,  # SQLite does its own file handling
    "labs/lab06_governed_runtime.py": 1,  # writes the crash child program
    "graph/nodes.py": 1,  # writes the published workspace document
    "labs/lab07_node_graph.py": 1,  # the --json checks dump
    "graph/swarm.py": 1,  # one evidence record per candidate
    "labs/lab08_parallel_swarm.py": 3,  # read published doc x2, write checks dump
}


def _filesystem_calls(tree: ast.Module) -> list[int]:
    """Line numbers in ``tree`` that reach the filesystem, in source order.

    The receiver decides. ``os.open(path, ...)`` and ``self._path.read_bytes()``
    are both I/O. ``self.load(thread_id)`` is a method call whose *name* happens
    to collide with a codec verb, and ``json.dumps(obj)`` is arithmetic on
    strings. Judging by the name alone -- which an earlier version of this helper
    did -- reported ``checkpoint.py`` for reading a database.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _IO_VERBS or func.id in {"load", "dump"}:
                hits.append(node.lineno)
            continue
        if not isinstance(func, ast.Attribute):
            continue
        receiver = func.value
        if isinstance(receiver, ast.Name):
            if receiver.id in _CODECS:
                if func.attr in {"load", "dump"}:
                    hits.append(node.lineno)
            elif receiver.id in {"os", "io", "pathlib"}:
                if func.attr in _IO_VERBS:
                    hits.append(node.lineno)
            elif func.attr in _IO_VERBS:
                # `self._path.read_bytes()`, `Path(p).write_text(...)`: the
                # receiver is a path object, however it was spelled.
                hits.append(node.lineno)
        elif isinstance(receiver, (ast.Attribute, ast.Call)) and func.attr in _IO_VERBS:
            hits.append(node.lineno)
    return sorted(set(hits))


def test_the_only_filesystem_access_is_a_named_durability_owner() -> None:
    """Lab 2's invariant, restated once per lab that legitimately needs a file.

    The *reason* it holds has changed twice, and each change is worth stating:

    * it held because the readers went through the store;
    * then because the readers stopped going through anything;
    * and now because the only files in the package belong to modules whose
      purpose is to own them.

    The allowlist is per-module and enumerated, not a prefix match, so a new
    module that opens a file fails here rather than slipping under a wildcard.
    """
    # `seed_store.py` is the Lab 2 store itself. `__init__.py` files only
    # re-export. Neither is an exception -- they are the front door itself.
    allowed = {"tools/seed_store.py"} | {p.name for p in _python_files()
                                         if p.name == "__init__.py"}
    allowed |= set(_DURABILITY_OWNERS)

    offending: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in allowed:
            continue
        for lineno in _filesystem_calls(_tree_of(path)):
            offending.append(f"{rel}:{lineno}")
    assert not offending, (
        "only the modules that own a file may touch the filesystem; unexpected "
        f"access in {offending}. If a new component genuinely needs durable "
        f"state, add it to _DURABILITY_OWNERS with the reason it needs one."
    )


def test_the_durability_owners_only_reach_their_own_file() -> None:
    """The allowlist is not a blank cheque.

    Each owner is expected to make a counted number of filesystem calls. Adding
    a fourth read to the audit log, say, fails here rather than passing silently
    under a wildcard that has stopped meaning anything.
    """
    for rel, expected in _EXPECTED_IO_CALLS.items():
        path = PACKAGE / rel
        assert path.exists(), f"{rel} is allowlisted but does not exist"
        hits = _filesystem_calls(_tree_of(path))
        assert len(hits) == expected, (
            f"{rel} now makes {len(hits)} filesystem calls, expected {expected}. "
            f"If that is intended, update _EXPECTED_IO_CALLS and justify it in "
            f"the doc for the lab that owns the module."
        )

    # And the owner really does own a file: the allowlist is not a list of
    # modules that happen to be clean.
    assert (PACKAGE / "runtime/audit.py").exists()
    assert (PACKAGE / "runtime/checkpoint.py").exists()


# ==========================================================================
# 8. The wire: a real MCP server in a real subprocess
# ==========================================================================


def test_the_server_advertises_exactly_the_contract(
    mcp_client: McpConnectorClient, registry: EndpointRegistry
) -> None:
    assert mcp_client.names() == sorted(EXPECTED_ENDPOINTS)
    assert mcp_client.server_names() == ([], []), (
        "the server and the client's contract disagree about the surface"
    )


def test_mcp_and_in_process_return_the_same_payload(
    mcp_client: McpConnectorClient, registry: EndpointRegistry, token: str
) -> None:
    """Same principal, same call, same answer -- through a process boundary.

    The one field allowed to differ is ``provenance.fetched_at``, which is a
    timestamp and is *supposed* to differ between two reads. Everything else
    being identical is what makes the MCP client a transport rather than a
    second, subtly different implementation.
    """
    direct = InProcessConnectorClient(registry=registry, token=token)
    for endpoint, payload in (
        ("list_courses", {}),
        ("read_notes_store", {"course_code": "CS201"}),
        ("read_assignment_rubrics", {"course_code": "CS201"}),
        ("read_submission_records", {"course_code": "CS201"}),
    ):
        a = direct.read(endpoint, payload).model_dump(mode="json")
        b = mcp_client.read(endpoint, payload).model_dump(mode="json")
        a["provenance"].pop("fetched_at")
        b["provenance"].pop("fetched_at")
        assert a == b, f"{endpoint} differs between transports"


def test_a_refusal_keeps_its_type_across_the_process_boundary(
    mcp_client: McpConnectorClient, registry: EndpointRegistry
) -> None:
    """A caller writes one ``except`` clause regardless of transport.

    This is the reason for the ``[ClassName]`` tag in
    :func:`tag_for_wire`. ``mcp.server.fastmcp`` formats a tool failure as
    ``f"Error executing tool {name}: {e}"`` and carries the *message* only, so
    without an explicit tag the client cannot tell a policy refusal from a bug.
    """
    student = registry.principal_registry.token_for_role("student")
    client = McpConnectorClient(
        contract=contract_from_registry(registry),
        token=student,
        source="local",
        role="student",
    )
    try:
        client.read("read_notes_store", {"course_code": "CS201"})
        with pytest.raises(ConnectorPolicyError) as excinfo:
            client.read("read_submission_records", {"course_code": "CS201"})
        assert "submissions:read" in str(excinfo.value)
    finally:
        client.close()


def test_an_unknown_endpoint_over_the_wire_is_a_not_found(
    mcp_client: McpConnectorClient,
) -> None:
    with pytest.raises(ConnectorNotFoundError):
        mcp_client.read("read_everything")


def test_the_credential_is_never_a_wire_argument(
    mcp_client: McpConnectorClient,
) -> None:
    """Structural proof that a model cannot escalate.

    Checked against the *server's* advertised input schemas, which is where a
    smuggled parameter would have to appear to be usable.
    """
    for name in mcp_client.names():
        tool = mcp_client._ensure_tools()[name]  # noqa: SLF001 - on purpose
        # `MCPTool.args_schema` is already a plain JSON-schema dict in
        # langchain-mcp-adapters 0.3.2, not a model class. Read it as what it is
        # rather than calling `model_json_schema()` on it.
        schema = tool.args_schema
        assert isinstance(schema, dict), type(schema).__name__
        properties = set(schema.get("properties", {}))
        assert not properties & {
            "token",
            "role",
            "scope",
            "principal",
            "subject",
            "auth",
        }, f"{name} accepts a credential on the wire: {sorted(properties)}"
        # And the whole advertised schema, printed, must not contain one either.
        # A credential hidden in a nested object would not show in `properties`.
        rendered = json.dumps(schema)
        assert "token" not in rendered.lower(), f"{name} mentions a token: {rendered}"
        assert "role" not in rendered.lower(), f"{name} mentions a role: {rendered}"


def test_the_server_never_advertises_a_similarity_verdict(
    mcp_client: McpConnectorClient,
) -> None:
    """Lab 4's flag must not become an answer by travelling over the wire.

    Regression guard for a design decision made in Lab 4: `SimilarityFlag`
    carries a routing signal and no verdict. If a future schema grew an
    ``is_plagiaristic`` field, the similarity check would have quietly become a
    judgement, which doc 2.12 forbids an automated system from making.
    """
    for name in mcp_client.names():
        tool = mcp_client._ensure_tools()[name]  # noqa: SLF001 - on purpose
        schema = tool.args_schema
        rendered = json.dumps(schema)
        assert "is_plagiaristic" not in rendered, (
            f"{name} advertises a plagiarism verdict on the wire"
        )
        forbidden = _keys_of(schema) & {
            "is_plagiaristic",
            "verdict",
            "plagiarism_score",
            "decision",
        }
        assert not forbidden, f"{name} advertises {sorted(forbidden)}"


def _keys_of(schema: dict[str, Any]) -> set[str]:
    """Every property name anywhere in a JSON schema, recursively."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            else:
                found.update(_keys_of(value))
    elif isinstance(schema, list):
        for item in schema:
            found.update(_keys_of(item))
    return found


def test_a_misbehaving_server_is_caught_by_the_contract(
    registry: EndpointRegistry, token: str
) -> None:
    """The client validates the payload, so a wrong server cannot feed an agent.

    Doc 2.10 Lab 5 calls for *typed* endpoints. A typed endpoint is only real if
    something checks the type on the way in, so this points the client at a
    server that returns well-formed JSON of the wrong shape and asserts the
    client refuses it rather than handing the agent a dict.

    The fake is a real MCP server -- a subprocess speaking the real protocol --
    so the test covers the wire and the validator together rather than mocking
    away the interesting half.
    """
    # Start from a payload that is *exactly* right -- a real read, dumped to
    # JSON by the same models the client will validate with -- and introduce
    # precisely one lie. The earlier version of this fake returned a stub whose
    # `provenance` was `{}`, which failed validation on its first missing field,
    # so the assertion about `note_count` was only ever reached by luck of
    # field order. A contract test should isolate the defect it names.
    honest = registry.invoke("read_notes_store", {"course_code": "CS201"}, token=token).unwrap()
    payload = honest.model_dump(mode="json")
    assert payload["note_count"] == 8, "fixture assumption: CS201 has 8 notes"
    payload["note_count"] = "eight"  # a string where the model demands an int

    script = (
        "import json, sys\n"
        "from mcp.server.fastmcp import FastMCP\n"
        f"PAYLOAD = json.loads({json.dumps(json.dumps(payload))!r})\n"
        "server = FastMCP('liar')\n"
        "@server.tool(name='read_notes_store')\n"
        "def read_notes_store(course_code: str) -> dict:\n"
        "    # Byte-identical to a real read, except one field's type. Right\n"
        "    # names, right shape, wrong types inside: the client must notice.\n"
        "    return PAYLOAD\n"
        "server.run(transport='stdio')\n"
    )
    path = _write_script("liar_server.py", script)

    # The client is given the *real* contract, built from the real registry.
    # The liar advertises a matching tool name, so the only thing standing
    # between its payload and an agent is the client's own validation -- which
    # is exactly the property under test.
    client = McpConnectorClient(
        contract=contract_from_registry(registry),
        token=token,
        command=[sys.executable, str(path)],
    )
    try:
        with pytest.raises(ConnectorContractError) as excinfo:
            client.read("read_notes_store", {"course_code": "CS201"})
        message = str(excinfo.value)
        assert "read_notes_store" in message
        assert "note_count" in message, (
            f"the error must name the field that failed: {message}"
        )
    finally:
        client.close()


def test_a_server_that_cannot_start_is_a_transport_error(token: str) -> None:
    """Broken transport must not be reported as a policy problem.

    A caller who sees "you are not allowed" when the truth is "the subprocess
    died" will go looking for the wrong bug for an hour.
    """
    registry = build_default_connector(source="local", token=token)
    client = McpConnectorClient(
        contract=contract_from_registry(registry),
        token=token,
        command=[sys.executable, "-c", "raise SystemExit(3)"],
    )
    try:
        with pytest.raises(ConnectorTransportError) as excinfo:
            client.names()
        assert "could not start" in str(excinfo.value)
    finally:
        client.close()


def test_the_errlog_default_lives_under_the_asynccontextmanager_wrapper() -> None:
    """Why `_stdio_errlog_target` exists at all.

    `mcp.client.stdio.stdio_client` is wrapped by `@asynccontextmanager`, and
    `functools.wraps` copies `__name__`, `__doc__` and `__dict__` but **not**
    `__defaults__`. So the wrapper's `__defaults__` is `None` while the bound
    `sys.stderr` sits one level down on `__wrapped__`.

    Asserted directly because the failure it causes is silent: rebinding the
    wrapper's defaults looks like it worked, changes nothing, and the notebook
    cell still fails with `fileno`.
    """
    from course_organizer.connector.client import _stdio_errlog_target
    from mcp.client.stdio import stdio_client

    assert stdio_client.__defaults__ is None, (
        "if this ever becomes non-None the wrapper no longer hides the defaults"
    )
    target = _stdio_errlog_target()
    assert target is stdio_client.__wrapped__
    assert target.__defaults__ is not None
    # Which stream it is depends on the host: the real stderr in a plain process,
    # pytest's captured stderr here, ipykernel's descriptor-less OutStream in a
    # notebook. What must hold in *this* process is that it is a usable one --
    # which is exactly what makes `_fd_backed_stderr` a no-op below.
    target.__defaults__[0].fileno()


def test_the_stderr_default_is_only_touched_when_it_has_to_be() -> None:
    """`_fd_backed_stderr` is a no-op in every ordinary process.

    Asserted first and separately because the alternative -- unconditional
    reassignment -- would make every code path in this project depend on a
    workaround for a Jupyter kernel, including the CLI and the tests, where the
    problem does not exist.

    Note the block is asynchronous: it is entered from inside the connector's
    event loop, next to the spawn it exists to make possible.
    """
    from course_organizer.connector.client import _fd_backed_stderr, _stdio_errlog_target

    async def _exercise() -> object:
        target = _stdio_errlog_target()
        before = target.__defaults__
        async with _fd_backed_stderr():
            during = target.__defaults__
        return before, during, target.__defaults__

    before, during, after = run_coroutine(_exercise())
    assert before is during is after, (
        "a process with a real stderr must leave the library's default alone"
    )

    # And the bound default is genuinely usable here, which is why nothing moved.
    bound = before[0]
    assert bound is not None
    bound.fileno()  # raises if it is not a real descriptor, which would be the bug


def test_a_stderr_without_a_descriptor_is_replaced_and_then_restored() -> None:
    """The Jupyter case, constructed rather than waited for.

    Regression, and the reason the fix exists. `mcp.client.stdio.stdio_client` is
    declared `async def stdio_client(server, errlog: TextIO = sys.stderr)` --
    the default is bound when the module is imported -- and it passes that stream
    to `subprocess.Popen(stderr=errlog)`, which needs a real descriptor.
    `ipykernel/kernelapp.py` replaces `sys.stderr` with an `OutStream` that
    forwards to the kernel's iopub channel and has no OS-level file, so a spawn
    from a notebook cell died with `io.UnsupportedOperation: fileno` before the
    server ran a line.

    `langchain_mcp_adapters.sessions` calls `stdio_client(server_params)` with one
    argument, so the default is the only seam. Here the substitution is forced by
    pointing the bound default at a filenoneless object first, which is exactly
    the state ipykernel leaves behind.
    """
    import io

    from course_organizer.connector.client import _fd_backed_stderr, _stdio_errlog_target

    class _NoDescriptor(io.StringIO):
        """Looks like a stream, is not a descriptor -- like ipykernel's."""

        def fileno(self) -> int:
            raise io.UnsupportedOperation("fileno")

    target = _stdio_errlog_target()
    original = target.__defaults__
    try:
        target.__defaults__ = (_NoDescriptor(),)
        fake_bound = target.__defaults__[0]

        async def _exercise() -> object:
            async with _fd_backed_stderr() as stream:
                rebound = target.__defaults__[0]
                assert stream is not None
                stream.fileno()  # the replacement must really work
            return rebound, target.__defaults__[0]

        rebound, restored = run_coroutine(_exercise())

        assert rebound is not fake_bound
        assert rebound is not None and rebound is sys.__stderr__
        assert restored is fake_bound, "the library's own default must be put back"
    finally:
        target.__defaults__ = original


def test_an_unusable_bound_default_and_no_descriptor_two_is_reported_not_hidden() -> (
    None
):
    """With no real stderr anywhere, the block does nothing and says so.

    The failure mode this guards is a workaround that swallows the original
    error. `_fd_backed_stderr` yields ``None`` when :func:`_real_stderr` finds
    nothing, the spawn proceeds, and `Popen` raises its own
    `fileno` error -- which the client already reports as a transport failure
    naming the command. Nothing here pretends to fix a host with no descriptor.
    """
    import io

    from course_organizer.connector.client import _fd_backed_stderr, _stdio_errlog_target

    class _NoDescriptor(io.StringIO):
        def fileno(self) -> int:
            raise io.UnsupportedOperation("fileno")

    target = _stdio_errlog_target()
    original_stderr = sys.__stderr__
    original_defaults = target.__defaults__
    try:
        sys.__stderr__ = _NoDescriptor()  # type:ignore[assignment]
        target.__defaults__ = (_NoDescriptor(),)
        fake_bound = target.__defaults__[0]

        async def _exercise() -> object:
            async with _fd_backed_stderr() as stream:
                assert stream is None, "there was nothing to substitute"
                assert target.__defaults__[0] is fake_bound
            return target.__defaults__[0]

        assert run_coroutine(_exercise()) is fake_bound
    finally:
        sys.__stderr__ = original_stderr
        target.__defaults__ = original_defaults


def test_a_spawn_from_a_notebook_kernel_serves_the_connector(token: str) -> None:
    """End to end: the ipykernel case, without needing a kernel.

    The three unit tests above prove the substitution happens and is undone. This
    one proves the reason they exist still holds: with `sys.stderr` replaced by a
    stream that has no descriptor -- exactly what `ipykernel/kernelapp.py` does
    -- the connector still starts a real server subprocess and serves a call.

    Without `_fd_backed_stderr` this raises `io.UnsupportedOperation: fileno` from
    `subprocess.Popen`, surfaced as `ConnectorTransportError`.
    """
    import io

    from course_organizer.connector.client import McpConnectorClient

    class _NoDescriptor(io.StringIO):
        def fileno(self) -> int:
            raise io.UnsupportedOperation("fileno")

    registry = build_default_connector(source="local", token=token)
    client = McpConnectorClient(
        contract=contract_from_registry(registry), token=token, source="local"
    )
    original_stderr = sys.stderr
    try:
        sys.stderr = _NoDescriptor()  # type:ignore[assignment]
        assert sorted(client.names()) == sorted(EXPECTED_ENDPOINTS), (
            "a server must have answered"
        )
        value = client.read("read_notes_store", {"course_code": "CS201"})
        assert value.note_count == 8
        assert value.provenance.sources, "provenance must survive the wire"
    finally:
        sys.stderr = original_stderr
        client.close()


def test_server_command_uses_this_interpreter_and_no_token(token: str) -> None:
    """A command line is visible to every process; an env var is not."""
    executable, args, child_env = server_command()
    assert executable == sys.executable, (
        "spawning bare 'python' can land outside this virtualenv"
    )
    assert "--token" not in args, (
        "the credential must not appear in the process list"
    )
    assert "course_organizer.connector.mcp_server" in args
    assert "PYTHONPATH" in child_env


def test_the_server_prints_its_contract_without_a_token() -> None:
    """`--describe` is how the contract is checked by a human or by CI."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "course_organizer.connector.mcp_server", "--describe"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PACKAGE / "..")},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for endpoint in EXPECTED_ENDPOINTS:
        assert endpoint in result.stdout
    assert "local_seed" in result.stdout


def test_build_server_attaches_its_registry_for_inspection(token: str) -> None:
    """The server's *own* ledger must be reachable, or the wire is unauditable."""
    server = build_server(source="local", token=token)
    registry = server.course_organizer_registry  # type: ignore[attr-defined]
    assert isinstance(registry, EndpointRegistry)
    assert set(registry.names()) == EXPECTED_ENDPOINTS


# ==========================================================================
# 9. Error types survive a serialisation round trip
# ==========================================================================


@pytest.mark.parametrize(
    "error",
    [
        ConnectorAuthError("no token"),
        ConnectorPolicyError("not allowed"),
        ConnectorNotFoundError("no such thing"),
        ConnectorContractError("wrong shape"),
        ConnectorTransportError("no pipe"),
    ],
)
def test_the_wire_tag_round_trips(error: ConnectorError) -> None:
    """The convention the two halves share, tested on every type.

    ``mcp.server.fastmcp`` discards the exception class, so the tag is the only
    thing that carries it. If these drift apart, a caller silently starts seeing
    transport errors where it used to see policy errors.
    """
    tagged = tag_for_wire(error)
    name, body = untag_from_wire(f"Error executing tool x: {tagged}")
    assert name == type(error).__name__
    assert body == str(error)


def test_an_untagged_error_is_not_mistaken_for_a_known_type() -> None:
    """Unknown text must degrade to a transport failure, not a wrong answer."""
    name, body = untag_from_wire("Error executing tool x: something went wrong")
    assert name is None
    assert body == "Error executing tool x: something went wrong"


def test_bracketed_prose_is_not_mistaken_for_a_tag() -> None:
    """A tag must be a token, not any bracketed word.

    The tag is required to be followed by whitespace so an ordinary message
    mentioning ``[the rubric]`` cannot be re-typed as an error class.
    """
    name, _ = untag_from_wire("see [the rubric] for details")
    assert name is None or name not in {
        c.__name__
        for c in (
            ConnectorAuthError,
            ConnectorPolicyError,
            ConnectorContractError,
            ConnectorNotFoundError,
            ConnectorTransportError,
        )
    }


# ==========================================================================
# 10. The client contract is a real, checkable object
# ==========================================================================


def test_a_contract_validates_the_endpoints_it_declares(
    registry: EndpointRegistry, token: str
) -> None:
    contract = contract_from_registry(registry)
    assert set(contract.names()) == EXPECTED_ENDPOINTS
    value = registry.invoke("list_courses", {}, token=token).unwrap()
    # `validate` returns the re-validated model, so this is a round trip rather
    # than a boolean: the payload that crossed `model_dump` comes back as a
    # typed object with its fields intact.
    again = contract.validate("list_courses", value.model_dump(mode="json"))
    assert again.course_codes == value.course_codes
    assert again.provenance.source == "local_seed"


def test_a_contract_rejects_a_payload_of_the_wrong_shape(
    registry: EndpointRegistry,
) -> None:
    contract = contract_from_registry(registry)
    with pytest.raises(ConnectorContractError):
        contract.validate("read_notes_store", {"course_code": "CS201"})
    with pytest.raises(ConnectorContractError):
        contract.validate("read_notes_store", "not even an object")


def test_the_client_interface_takes_no_credential() -> None:
    """`read()` must not accept a token, a role, or a scope.

    Asserted on the signature rather than in a docstring, because this is the
    structural version of "a model cannot escalate": there is nowhere to put the
    thing it would need to change.
    """
    import inspect

    for cls in (InProcessConnectorClient, McpConnectorClient):
        params = set(inspect.signature(cls.read).parameters) - {"self"}
        assert params == {"endpoint", "payload"}, (
            f"{cls.__name__}.read takes {sorted(params)}"
        )
        # And the credential is not hiding in a keyword-only parameter either;
        # `parameters` covers those, but the check is cheap to make explicit.
        for param in inspect.signature(cls.read).parameters.values():
            assert param.kind not in (
                param.VAR_KEYWORD,
                param.VAR_POSITIONAL,
            ), f"{cls.__name__}.read accepts **kwargs; nothing can be smuggled"
        # The token exists on the client, because it must be held somewhere --
        # just nowhere a call can reach it.
        assert "token" in inspect.signature(cls.__init__).parameters


def test_run_coroutine_is_usable_from_sync_code() -> None:
    """The sync bridge the notebooks rely on."""

    async def value() -> int:
        return 42

    assert run_coroutine(value()) == 42


def _write_script(name: str, source: str) -> Path:
    """Write a helper server module to a temp file and return its path."""
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="lab05-"))
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path
