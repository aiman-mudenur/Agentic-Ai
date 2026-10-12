"""Tests for the Lab 5 lab module: the evidence it produces, and its honesty.

``tests/test_lab05_mcp_connector.py`` checks the *product* -- the connector, its
server, its boundary. This file checks the *demonstration*, which is a different
thing with a different failure mode: a lab module that prints "PASS" while its
measurements do not support the claim is worse than no lab module, because it
is evidence.

Three things are asserted here:

1. **The document's words are quoted exactly.** Section (a) of the write-up and
   the printed report both rest on these constants, and a paraphrase that
   slipped through would make every downstream citation wrong.
2. **The checks are derived from measurements, not from flags set during the
   run.** A check that reads a field the run wrote optimistically proves
   nothing; each of these re-derives its verdict from the observations.
3. **A skipped check cannot masquerade as a passing one.** ``--no-mcp`` must
   produce ``OVERALL: PASS`` with the transport checks counted separately, and
   the summary must say so in words.

The MCP half spawns three real server processes and costs several seconds, so
the end-to-end run is session-scoped and shared.
"""

from __future__ import annotations

import inspect
import re
import secrets
import pytest

from course_organizer.connector import CONNECTOR_ROLES, ROLE_SCOPES
from course_organizer.connector.auth import NOTES_READ, SUBMISSIONS_READ
from course_organizer.connector.client import InProcessConnectorClient
from course_organizer.labs import lab05_mcp_connector as lab05
from course_organizer.labs.lab05_mcp_connector import (
    AccessRow,
    BypassObservation,
    CLOCK_PATHS,
    DEFAULT_SWAP_COURSES,
    LAB,
    LabRunResult,
    McpConnectorClient,
    ParityRow,
    ReadObservation,
    SwapLeg,
    SwapReport,
    TransportReport,
    WireOutcome,
    _flatten,
    _row_marker,
    access_matrix,
    build_default_connector,
    demonstrate_bypass,
    measure_source_swap,
    observe_reads,
)

#: The endpoints the document's three ``read(...)`` lines map to, derived from
#: the module's own mapping rather than restated here. Restating it would give
#: the mapping a second place to be wrong.
DOCUMENTED_ENDPOINTS: tuple[str, ...] = tuple(
    name for _, name in lab05.PSEUDOCODE_TO_ENDPOINT
)

#: The endpoint this project added, because a lab has to enumerate something
#: and the document names no enumeration read.
ADDED_ENDPOINT = "list_courses"

#: Doc 2.10, section 2.10's Lab 5 row, verbatim. Seven fields. Each one is
#: quoted somewhere in the report and again in ``docs/lab05.md``, so a drift
#: here is drift in both.
DOC_LAB5_FIELDS: dict[str, str] = {
    "Goal": "Expose every data source through one typed connector.",
    "Deliverable": (
        "A connector server with typed endpoints and auth; agents call only "
        "through it."
    ),
    "Accept": (
        "All data access goes through the connector; swapping a source needs no "
        "agent change."
    ),
    "Pitfall": (
        "No agent should touch a source directly; a bypass defeats governance "
        "later."
    ),
}

#: The three `read(...)` lines the document's pseudocode contains.
DOC_READ_LINES: tuple[str, ...] = (
    "read('the notes/materials store')",
    "read('assignment & rubric definitions')",
    "read('submission records')",
)


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture(scope="session")
def session_token() -> str:
    """One credential for the whole session's end-to-end run."""
    return secrets.token_urlsafe(24)


@pytest.fixture(scope="session")
def fast_result(session_token: str) -> LabRunResult:
    """One in-process run, shared. No subprocess, so this is cheap."""
    return lab05.run_mcp_connector(
        run_id="t-lab05-fast",
        token=session_token,
        course_code="CS201",
        with_mcp=False,
    )


@pytest.fixture(scope="session")
def full_result(session_token: str) -> LabRunResult:
    """The complete run, including three spawned MCP servers. Slower.

    Session-scoped because each server spawn costs seconds and the run has
    nothing to vary between tests -- the assertions below all read the same
    object.
    """
    return lab05.run_mcp_connector(
        run_id="t-lab05-full",
        token=session_token,
        course_code="CS201",
        with_mcp=True,
    )


@pytest.fixture
def registry(session_token: str):
    """A local-seed connector over the shared session token."""
    return build_default_connector(source="local", token=session_token)


# ==========================================================================
# 1. The document's words
# ==========================================================================


@pytest.mark.parametrize(
    ("constant", "expected"),
    [
        (lab05.DOC_GOAL, DOC_LAB5_FIELDS["Goal"]),
        (lab05.DOC_DELIVERABLE, DOC_LAB5_FIELDS["Deliverable"]),
        (lab05.DOC_ACCEPTANCE, DOC_LAB5_FIELDS["Accept"]),
        (lab05.DOC_PITFALL, DOC_LAB5_FIELDS["Pitfall"]),
    ],
)
def test_the_documents_fields_are_quoted_exactly(
    constant: str, expected: str
) -> None:
    """Exact string equality, not a prefix or a fuzzy match.

    These four constants are the spine of the write-up. A paraphrase here would
    propagate into ``docs/lab05.md``, into the report, and into the viva
    answers, and nobody reading them would have the document to check against.
    """
    assert constant == expected


def test_the_pseudocode_is_quoted_with_its_own_comment() -> None:
    """The document's ``#`` comment is part of the pseudocode, not an aside.

    It states the pitfall in the document's own voice. Dropping it from the
    printed pseudocode would leave the report quoting the doc's *code* while
    omitting the doc's *warning*, which is the part a reviewer most wants to see
    attributed to the document rather than to this project.
    """
    assert "agents.use(connector)" in lab05.DOC_PSEUDOCODE
    assert "# no agent touches a source directly" in lab05.DOC_PSEUDOCODE
    for line in DOC_READ_LINES:
        assert line in lab05.DOC_PSEUDOCODE


def test_each_document_line_maps_to_one_endpoint() -> None:
    """Three lines in, three endpoints out, and the mapping is total.

    Asserted as a bijection because it is the traceability claim in miniature:
    if two lines mapped to one endpoint, the third read would have no governed
    route; if one endpoint answered to no line, it would be undocumented.
    """
    lines = [line for line, _ in lab05.PSEUDOCODE_TO_ENDPOINT]
    endpoints = [name for _, name in lab05.PSEUDOCODE_TO_ENDPOINT]
    assert lines == list(DOC_READ_LINES)
    assert len(set(endpoints)) == len(endpoints) == 3
    assert set(endpoints) == set(DOCUMENTED_ENDPOINTS)


def test_the_read_payloads_are_valid_for_every_endpoint() -> None:
    """Every endpoint's declared payload validates against its own model.

    Checked by validation rather than by eye, because an invalid payload fails
    deep inside ``invoke`` with a message about the wrong thing.
    """
    from course_organizer.connector.endpoints import (
        AssignmentQuery,
        CourseListResult,
        NotesQuery,
        SubmissionQuery,
    )
    from pydantic import BaseModel

    models: dict[str, type[BaseModel]] = {
        "list_courses": CourseListResult,  # input is the shared empty model
        "read_notes_store": NotesQuery,
        "read_assignment_rubrics": AssignmentQuery,
        "read_submission_records": SubmissionQuery,
    }
    for endpoint, payload in lab05.READ_PAYLOADS.items():
        model = models[endpoint]
        if endpoint == "list_courses":
            assert payload == {}, payload
            continue
        model.model_validate(payload)


# ==========================================================================
# 2. Section 2 -- the observations are measurements
# ==========================================================================


def test_the_reads_report_what_the_connector_counted(
    registry, session_token: str
) -> None:
    """Each observation's count is the ledger's, not the payload's own claim.

    The connector counts records itself, from ``EndpointSpec.count_field``, and
    writes that number to the ledger. Printing the payload's ``describe()``
    instead would report what the *source* says about itself, which is the
    number a source swap could change -- and so is not evidence that the
    governance counted correctly.
    """
    observations = observe_reads(registry, session_token, "CS201")
    assert len(observations) == 4
    by_endpoint = {o.endpoint: o for o in observations}

    assert by_endpoint["read_notes_store"].record_count == 8
    assert by_endpoint["read_assignment_rubrics"].record_count == 3
    assert by_endpoint["read_submission_records"].record_count == 8
    assert by_endpoint["list_courses"].record_count == 2

    for observation in observations:
        assert observation.outcome == "allowed", observation.endpoint
        assert observation.record_count > 0, observation.endpoint
        assert "(no location reported)" not in observation.provenance
        assert "by faculty@example.edu" in observation.provenance


def test_only_the_three_document_reads_are_labelled_documented(
    registry, session_token: str
) -> None:
    """``documented_reads`` has exactly three members.

    If the added endpoint ever became labelled documented, or one of the
    document's three stopped being, this is where it shows.
    """
    observations = observe_reads(registry, session_token, "CS201")
    documented = [o for o in observations if o.documented]
    assert [o.endpoint for o in documented] == [
        "read_notes_store",
        "read_assignment_rubrics",
        "read_submission_records",
    ]
    added = [o for o in observations if not o.documented]
    assert [o.endpoint for o in added] == ["list_courses"]
    assert "not a documented requirement" in added[0].declared_origin


def test_an_observation_renders_its_own_evidence(registry, session_token: str) -> None:
    """``render()`` prints the numbers it was given, not placeholders.

    A report whose renderer silently substituted a summary line would pass every
    structural test here while telling the reader nothing.
    """
    observations = observe_reads(registry, session_token, "CS201")
    text = observations[0].render()
    assert "read_notes_store" in text
    assert "-> records     : 8" in text
    assert "data/seed/data_structures/notes.json" in text


# ==========================================================================
# 3. Section 3 -- the access matrix
# ==========================================================================


def test_every_cell_matches_the_role_scope_table(
    registry, session_token: str
) -> None:
    """The matrix is the policy table, re-derived by calling it.

    Not compared against a hardcoded grid: the point is that
    :func:`access_matrix` reaches the same verdict the declared policy says it
    should, by actually asking. A hardcoded grid would only test that the
    function returns what the test already believed.
    """
    rows = access_matrix(registry, "CS201", registry.names())
    assert len(rows) == len(CONNECTOR_ROLES) * len(registry.names())

    for row in rows:
        scope = registry.get(row.endpoint).scope
        expected_allowed = scope in ROLE_SCOPES[row.role]
        assert row.has_scope is expected_allowed, row
        assert row.outcome == ("allowed" if expected_allowed else "refused"), row
        if row.outcome == "allowed":
            assert row.record_count > 0, row
            assert row.error == ""
        else:
            assert row.record_count == 0, row
            assert row.explained, f"{row.endpoint}/{row.role} -> {row.error}"


def test_the_student_refusal_is_on_the_cohort_not_on_the_students_own_material(
    registry, session_token: str
) -> None:
    """A student reads notes but not the cohort's submissions.

    The positive half matters as much as the negative one. A policy that refused
    *everything* to a student would satisfy "students cannot read submissions"
    while being broken, and the difference is only visible from the allowed
    cells.
    """
    rows = access_matrix(registry, "CS201", registry.names())
    student = {r.endpoint: r for r in rows if r.role == "student"}
    assert student["read_notes_store"].outcome == "allowed"
    assert student["read_notes_store"].record_count == 8
    assert student["read_assignment_rubrics"].outcome == "allowed"
    assert student["read_submission_records"].outcome == "refused"
    assert registry.get("read_submission_records").scope == SUBMISSIONS_READ


def test_anonymous_is_refused_everywhere_and_the_ledger_knows(
    registry,
) -> None:
    """No credential means no access, and every attempt is recorded."""
    rows = access_matrix(registry, "CS201", registry.names())
    anonymous = [r for r in rows if r.role == "anonymous"]
    assert len(anonymous) == len(registry.names())
    assert all(r.outcome == "refused" for r in anonymous)
    assert all(r.explained for r in anonymous)
    # Not a single successful read in the whole table.
    assert not any(r.outcome == "allowed" for r in rows if r.role == "anonymous")


def test_a_refused_cell_is_marked_differently_from_an_unexplained_one() -> None:
    """``R`` and ``!`` are rendered differently, and that distinction survives.

    ``!`` would mean a refusal that was not a typed policy failure. Rendering
    both as ``R`` would make a broken connector look like a working one, which
    is the specific confusion this module is meant to make impossible.
    """
    explained = AccessRow("student", "read_submission_records", False, "refused",
                          "ConnectorPolicyError", 0, 0.1)
    unexplained = AccessRow("student", "read_submission_records", False, "refused",
                            "KeyError", 0, 0.1)
    assert _row_marker(explained) == "R"
    assert _row_marker(unexplained) == "!"
    assert explained.cell == "R"


def test_a_refusal_costs_nothing_like_reading_everything(
    registry, session_token: str
) -> None:
    """A policy check happens before the read, not after it.

    Measured on timings from the same call set. Loose as an inequality -- CI
    machines vary -- but it fails loudly if someone ever moves the scope check
    to after the handler, which is the change this guards against.
    """
    rows = access_matrix(registry, "CS201", registry.names())
    refusals = [r for r in rows if r.outcome == "refused"]
    allowed = [r for r in rows if r.outcome == "allowed"]
    assert refusals and allowed
    assert max(r.elapsed_ms for r in refusals) < max(r.elapsed_ms for r in allowed), (
        "a refusal was slower than the reads it refused to perform"
    )


def test_the_matrix_renders_as_a_table_rather_than_a_run_on_line(
    registry, session_token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered matrix is readable, which is the whole point of rendering it.

    Regression. The scope column was padded to a hardcoded nine characters while
    its longest value -- ``assignments:read, notes:read, submissions:read`` -- is
    forty-five. So the faculty and TA rows ran their scope list straight into the
    first record count and printed as one 47-character field, in the one table a
    reader is most likely to consult for the answer to "what does a student get?"

    Asserted structurally rather than by snapshot, so the fix cannot be to
    hardcode a wider constant: every column width has to come from the data it
    holds, which shows up as every line ending at the same offset.
    """
    rows = access_matrix(registry, "CS201", registry.names())
    lab05._print_matrix(rows, registry.names())
    lines = capsys.readouterr().out.splitlines()

    table = [line for line in lines if line.strip()]
    assert len(table) >= len(CONNECTOR_ROLES) + 1, table

    header, rule = table[0], table[1]
    body = table[2 : 2 + len(CONNECTOR_ROLES)]
    assert all(line.startswith("  -") for line in [rule]), rule

    # One width, measured from the data. Trailing padding is compared too,
    # because a column width is only "measured from the data" if every row was
    # padded by the same rule.
    widths = {len(header), len(rule)} | {len(line) for line in body}
    assert len(widths) == 1, f"ragged table, widths {sorted(widths)}"

    # And the specific defect: a scope list followed by a record count.
    for role in ("course_faculty", "teaching_assistant"):
        line = next(line for line in body if line.strip().startswith(role))
        assert re.search(r"submissions:read\s{2,}\d", line), (
            f"the scope column ran into the count column: {line!r}"
        )

    # The student's row is the one that carries the argument, so check it too.
    student = next(line for line in body if line.strip().startswith("student"))
    assert re.search(r"assignments:read, notes:read\s{2,}\d", student), student
    assert student.rstrip().endswith("R"), student

    anonymous = next(line for line in body if line.strip().startswith("anonymous"))
    assert re.search(r"none\s{2,}\s*R", anonymous), anonymous


# ==========================================================================
# 4. Unit tests for the helpers the measurements rest on
# ==========================================================================


def test_flatten_names_the_leaf_that_differs() -> None:
    """Nested differences are reported as paths, not as "the block differs".

    The whole parity check depends on this. Comparing two nested payloads with
    ``!=`` yields one bit -- different or not -- and the single field that
    legitimately differs (a timestamp, in a different process) then makes every
    endpoint look broken with nothing to point at.
    """
    a = {"provenance": {"fetched_at": "t1", "sources": ["x"]}, "n": 1}
    b = {"provenance": {"fetched_at": "t2", "sources": ["x"]}, "n": 1}
    flat_a, flat_b = _flatten(a), _flatten(b)
    assert set(flat_a) == {"provenance.fetched_at", "provenance.sources[0]", "n"}
    differing = sorted(k for k in flat_a if flat_a[k] != flat_b[k])
    assert differing == ["provenance.fetched_at"]


def test_flatten_indexes_list_items_so_a_swapped_record_is_visible() -> None:
    """A list of records compares element-wise, not wholesale."""
    a = {"items": [{"id": "a"}, {"id": "b"}]}
    b = {"items": [{"id": "a"}, {"id": "z"}]}
    differing = sorted(
        k for k in set(_flatten(a)) | set(_flatten(b))
        if _flatten(a).get(k) != _flatten(b).get(k)
    )
    assert differing == ["items[1].id"]


def test_a_populated_list_is_not_equal_to_an_absent_one() -> None:
    """An empty container contributes no leaves, so the union is what counts.

    This is the sharp edge of :func:`_flatten` and the reason the parity loop
    iterates ``set(a) | set(b)``. A wire result whose ``sources`` list was empty
    would flatten to no keys at all, and a one-sided comparison would report the
    two payloads as equal -- the most dangerous possible reading of "identical",
    since the wire silently dropped data.
    """
    populated = {"sources": ["one", "two"]}
    empty = {"sources": []}
    keys = set(_flatten(populated)) | set(_flatten(empty))
    assert keys == {"sources[0]", "sources[1]"}
    assert _flatten(empty) == {}

    differences = sorted(k for k in keys if _flatten(populated).get(k) != _flatten(empty).get(k))
    assert differences == ["sources[0]", "sources[1]"]


def test_a_parity_verdict_names_how_much_was_compared_and_where_it_differs() -> None:
    """The rendered verdict must be usable as evidence, not as a status flag.

    "identical" alone is a claim with no magnitude: three leaves compared and
    three hundred compared produce the same word. "differs" alone is worse -- it
    costs the reader the run to find the field.
    """
    same = ParityRow("read_notes_store", {"n": 1}, {"n": 1}, ())
    assert same.identical is True
    assert same.verdict() == "1 leaf field(s) identical"

    differs = ParityRow(
        "read_notes_store", {"provenance": {"fetched_at": "t1"}},
        {"provenance": {"fetched_at": "t2"}}, ("provenance.fetched_at",),
    )
    assert differs.identical is False
    assert differs.verdict() == (
        "1 leaf field(s) differ on provenance.fetched_at"
    )


def test_the_real_parity_rows_render_a_magnitude_not_just_a_verdict(
    full_result: LabRunResult,
) -> None:
    """Every delivered parity row reports a leaf count of at least two digits.

    A single-digit count would mean the comparison reached a top-level key and
    stopped, which is the failure mode a bare "identical" hides.
    """
    for row in full_result.transport.parity:
        text = row.verdict()
        assert text.endswith("identical"), text
        count = len(_flatten(row.direct_json))
        assert count >= 10, (row.endpoint, count, text)
        assert text.startswith(f"{count} "), (row.endpoint, text)


def test_the_only_excluded_path_is_the_clock() -> None:
    """One exclusion, named, and it is a timestamp.

    If this list grew, the parity result would weaken without the report
    saying so -- so the count is asserted.
    """
    assert CLOCK_PATHS == ("provenance.fetched_at",)


# ==========================================================================
# 5. Section 4 -- the transport report (against the real run)
# ==========================================================================


def test_the_full_run_passes_every_check(full_result: LabRunResult) -> None:
    """The delivered evidence: 18 checks, none failed, none skipped."""
    assert full_result.failed == (), [c[0] for c in full_result.failed]
    assert full_result.skipped == ()
    assert full_result.passed is True
    assert len(full_result.checks) == 18, len(full_result.checks)


def test_a_real_server_served_every_endpoint(full_result: LabRunResult) -> None:
    """The subprocess advertised the same four endpoints as the registry.

    Count equality rather than name equality, because the wire's names were
    already asserted field-by-field in
    ``test_lab05_mcp_connector.py``. What matters here is that nothing was
    missing -- a server that advertised three of four would pass a
    name-subset check.
    """
    t = full_result.transport
    assert t.tool_count == len(full_result.endpoint_names) == 4
    assert t.server_names == ([], []), (
        "an unnamed stdio connection is the design; a name would mean the tools "
        "came from somewhere else"
    )


def test_the_wire_and_the_direct_client_agree_on_every_compared_leaf(
    full_result: LabRunResult,
) -> None:
    """Parity is measured on hundreds of fields, not on the payload's truthiness.

    Each row reports how many leaves were compared, and the counts are large
    enough that "identical" is a real statement. A parity check that compared
    three top-level keys would pass while the nested provenance and every record
    inside every note went unexamined.
    """
    rows = full_result.transport.parity
    assert len(rows) == 4
    counts = {r.endpoint: len(_flatten(r.direct_json)) for r in rows}
    assert counts["list_courses"] >= 10, counts
    assert counts["read_notes_store"] >= 150, counts
    assert counts["read_assignment_rubrics"] >= 70, counts
    assert counts["read_submission_records"] >= 80, counts
    for row in rows:
        assert row.identical, (row.endpoint, row.differences)
        assert row.ignored == CLOCK_PATHS, (row.endpoint, row.ignored)


def test_a_refusal_crossed_the_wire_as_a_policy_failure(
    full_result: LabRunResult,
) -> None:
    """Both refused roles arrived as ``ConnectorPolicyError``, body intact.

    The exception *type* does not survive MCP on its own: the server formats a
    tool failure as text, so the client sees a string. The connector tags that
    string with ``[ClassName]`` and the client decodes the tag back into the
    class -- which is why the type name here is evidence that the round trip
    works, rather than just that something was refused.

    Asserted in both directions. A tagged message reaching the caller would mean
    the decode never ran; a decoded *type* with a stripped body would mean the
    explanation was thrown away with the tag, leaving an exception that says
    only that something was not allowed.
    """
    refusals = full_result.transport.all_wire_refusals
    assert {r.role for r in refusals} == {"student", "anonymous"}
    for refusal in refusals:
        assert refusal.error == "ConnectorPolicyError", refusal
        assert "submissions:read" in refusal.message
        assert "ROLE_SCOPES" in refusal.message, (
            "the message must still carry the policy pointer; a decode that ate "
            "the body would leave the reader with no way to look it up"
        )
        assert "[ConnectorPolicyError]" not in refusal.message, (
            "the wire tag is transport metadata and must not leak into the "
            "exception a caller reads"
        )
    assert full_result.transport.policy_refusals_survived is True


def test_one_spawn_served_four_calls(full_result: LabRunResult) -> None:
    """Session reuse, evidenced by the cost of four calls against one spawn.

    The comparison is deliberately *aggregate*: four calls costing less than one
    spawn. A mean would hide the failure mode -- one slow call in four is
    enough to make a per-call respawn look healthy.
    """
    t = full_result.transport
    assert len(t.warm_call_ms) == 4
    assert t.startup_ms > 0
    assert t.total_warm_ms < t.startup_ms, (
        f"{t.warm_call_ms} totalling {t.total_warm_ms}ms against a "
        f"{t.startup_ms}ms spawn"
    )
    assert t.session_is_reused is True


def test_each_refused_role_cost_its_own_spawn(full_result: LabRunResult) -> None:
    """One server per refused role, and that is the design, not an inefficiency.

    The role reaches the server through the server's *environment*. Serving a
    second role therefore needs a second process; the alternative -- putting the
    role on the wire -- is precisely what Lab 5 exists to prevent. Each spawn's
    time is therefore seconds rather than milliseconds, and must be reported
    separately from the warm-call figures, where it would read as a slow
    endpoint.

    The cost claim is anchored on the warm calls rather than on
    :attr:`TransportReport.startup_ms`, because spawn time is not a stable
    quantity: the faculty server pays for a cold ``.pyc`` cache and the refusal
    servers do not, so on a warm filesystem a refusal can measure *less* than
    the recorded startup. "Spawn-scale, not call-scale" is the claim that holds
    either way.
    """
    refusals = full_result.transport.refusals
    t = full_result.transport
    assert len(refusals) == 2
    assert {r.role for r in refusals} == {"student", "anonymous"}
    assert all(r.elapsed_ms > max(t.warm_call_ms) for r in refusals), (
        f"refusal spawns {[r.elapsed_ms for r in refusals]} cost less than a "
        f"warm call {max(t.warm_call_ms)}; a spawn was not paid"
    )
    assert t.spawns_per_refusal_role == 2


def test_warm_call_times_are_milliseconds_not_microseconds(
    full_result: LabRunResult,
) -> None:
    """Regression: the transport timings were once microseconds labelled as ms.

    The original comparison appended ``elapsed * 1000`` to a list the report
    then labelled in milliseconds, so the first call read as 66198ms while the
    row beside it printed 66.2ms. Both numbers came from the same measurement,
    which is what made it survivable: a units bug that agrees with itself.

    The check now is that the tuple the report iterates and the figure each
    ``WireOutcome`` carries are the same number, so a units change in one place
    cannot survive.
    """
    t = full_result.transport
    assert len(t.warm_call_ms) == len(t.outcomes)
    for outcome, elapsed in zip(t.outcomes, t.warm_call_ms):
        assert outcome.elapsed_ms == elapsed
        assert 0 < elapsed < t.startup_ms, (
            f"{outcome.endpoint} took {elapsed}ms against a {t.startup_ms}ms "
            f"spawn; a value in the thousands means the units are wrong"
        )


def test_a_wire_outcome_renders_its_own_evidence() -> None:
    """A refusal renders the class name; an allowed call renders the count."""
    allowed = WireOutcome("read_notes_store", "course_faculty", "allowed", "", 8, 1.5)
    refused = WireOutcome(
        "read_submission_records", "student", "refused",
        "ConnectorPolicyError", 0, 2.0,
    )
    assert "8 record(s)" in allowed.render()
    assert "ConnectorPolicyError" in refused.render()
    assert "read_submission_records" in refused.render()


# ==========================================================================
# 6. Section 5 -- the source swap
# ==========================================================================


def test_the_swap_changed_the_data_and_not_the_agent(session_token: str) -> None:
    """Same four lines, two corpora, one interface.

    Both the agent's own tuple and the measured outcome are asserted, so the
    claim "no agent change" rests on the code that ran rather than on a
    description of it.
    """
    report = measure_source_swap(
        token=session_token, run_id="t-swap", before_source="local",
        after_source="in_memory",
    )
    assert report.data_actually_changed is True
    assert report.same_agent_code is True
    assert report.before.course_name == "Data Structures"
    assert report.after.course_name == "In-memory exchange course"
    assert report.before.note_count == 8
    assert report.after.note_count == 1
    assert report.before.source_files == ("data/seed/data_structures/notes.json",)
    assert report.after.source_files == ("in-memory://ZZ999/notes",)
    assert report.result_type == "NotesReadResult"
    assert report.query_keys == ("course_code",)
    assert len(report.code) == 4


def test_the_swap_agent_tuple_is_the_code_that_actually_runs() -> None:
    """The printed agent lines are real, and they name real callables.

    A "here is the agent code" block that had drifted from the code would make
    the most persuasive part of the report a lie.
    """
    joined = "\n".join(lab05.SWAP_AGENT_CODE)
    assert "build_default_connector" in joined
    assert "use_connector" in joined
    assert "build_lab04_registry" in joined
    assert 'tools.invoke("read_notes_store"' in joined
    # No branch on `source` inside the agent: that is the whole point.
    assert "if source" not in joined


def test_a_source_without_a_configured_course_is_refused_loudly(
    session_token: str,
) -> None:
    """A missing course mapping raises rather than defaulting.

    Defaulting would let a typo in a source name produce a leg that reads a
    *different* course and still reports a successful swap.
    """
    with pytest.raises(KeyError, match="no course configured"):
        measure_source_swap(
            token=session_token,
            run_id="t-swap-bad",
            before_source="local",
            after_source="nonesuch",
            course_codes={"local": "CS201"},
        )


def test_both_swappable_sources_have_a_configured_course() -> None:
    """Every source the lab names is in the course map.

    Keeps :data:`DEFAULT_SWAP_COURSES` and the CLI's default ``--swap-source``
    from drifting apart, which would fail at run time on a flag that looked fine.
    """
    assert set(DEFAULT_SWAP_COURSES) == {"local", "in_memory"}
    assert DEFAULT_SWAP_COURSES["in_memory"] == "ZZ999"


# ==========================================================================
# 7. Section 6 -- the pitfall
# ==========================================================================


def test_the_pitfall_withheld_real_data_and_recorded_the_refusal(
    registry, session_token: str
) -> None:
    """The whole argument, in four assertions.

    The withheld amount must be real (obtained legitimately as faculty, so it
    is a measurement rather than an estimate), the refusal must be a policy
    failure, and the audit row must name both the caller and the scope that was
    missing -- because "someone was refused" does not answer "who".
    """
    observation = demonstrate_bypass(registry, session_token, "CS201")
    assert observation.withheld_records == 8
    assert observation.student_error == "ConnectorPolicyError"
    assert observation.student_own_read_allowed == 8
    assert observation.ledger_rows_for_endpoint >= 1
    assert observation.ledger_names_the_caller is True
    assert observation.ledger_names_the_scope is True
    assert SUBMISSIONS_READ in observation.student_message
    assert observation.read_params == ("endpoint", "payload")


def test_the_escalation_seam_is_computed_from_names_not_from_a_string(
    registry, session_token: str
) -> None:
    """The verdict is a name comparison; the string is for the reader.

    Asserted both ways. An earlier version compared the *rendered* signature
    against a literal, which would have failed the moment someone added a type
    annotation -- the kind of brittleness that gets a structural check quietly
    deleted.
    """
    observation = demonstrate_bypass(registry, session_token, "CS201")
    assert observation.no_escalation_parameter is True
    assert observation.escalation_seam == "(endpoint, payload)"
    # And the real signature agrees, for both client implementations.
    for cls in (InProcessConnectorClient, McpConnectorClient):
        params = set(inspect.signature(cls.read).parameters) - {"self"}
        assert params == {"endpoint", "payload"}, cls.__name__


def test_the_refusal_adds_exactly_one_named_ledger_row(
    registry, session_token: str
) -> None:
    """The audit trail is neither silent nor double-counted.

    Silence is the pitfall the document names. A double-count would be a
    different defect that makes the audit untrustworthy in the other direction,
    and it is the easier of the two to introduce by accident -- recording the
    refusal once at the scope check and again when the exception propagates.

    The three rows are asserted individually, so a ledger that gained a row for
    some unrelated reason cannot pass by matching the total.
    """
    before = len(registry.ledger)
    demonstrate_bypass(registry, session_token, "CS201")
    rows_added = len(registry.ledger) - before
    assert rows_added == 3, f"{rows_added} rows added, expected 3"

    refusals = registry.ledger.refusals()
    assert len(refusals) == 1, [r.principal for r in refusals]
    refusal = refusals[0]
    assert refusal.endpoint == "read_submission_records"
    assert refusal.principal == "student@example.edu"
    assert refusal.role == "student"
    assert refusal.allowed is False
    assert refusal.records == 0, "a refused read must not report records served"

    # The two rows that framed the comparison. Asserted by principal and count so
    # a ledger that gained an unrelated row cannot pass on the total alone.
    submissions = registry.ledger.for_endpoint("read_submission_records")
    assert len(submissions) == 2, [r.outcome for r in submissions]
    served = [r for r in submissions if r.allowed]
    assert len(served) == 1
    assert served[0].principal == "faculty@example.edu"
    assert served[0].records == 8

    notes = registry.ledger.for_endpoint("read_notes_store")
    assert [(r.principal, r.allowed, r.records) for r in notes] == [
        ("student@example.edu", True, 8)
    ]


# ==========================================================================
# 8. The run, the checks, and the skip path
# ==========================================================================


def test_the_fast_run_passes_but_does_not_claim_the_transport(
    fast_result: LabRunResult,
) -> None:
    """``--no-mcp`` reports the transport as skipped, in words.

    The dangerous version of this feature is a fast mode that prints
    ``OVERALL: PASS`` with the deliverable it exists to demonstrate never
    exercised. So: ``passed`` is True, ``failed`` is empty, *and* four to five
    checks are in ``skipped`` with a detail string that says they did not run.
    """
    assert fast_result.with_mcp is False
    assert fast_result.failed == ()
    assert fast_result.passed is True
    skipped = fast_result.skipped
    assert len(skipped) == len(lab05.TRANSPORT_CHECK_LABELS) == 5
    for label, passed, detail in skipped:
        assert label in lab05.TRANSPORT_CHECK_LABELS
        assert passed is True
        assert "NOT RUN" in detail
        assert "not evidence" in detail


def test_every_skipped_label_exists_as_a_real_check(fast_result: LabRunResult) -> None:
    """The skip list cannot name a check that does not exist.

    Otherwise a renamed check would silently stop being skipped *and* stop being
    evaluated -- and ``--no-mcp`` would report PASS without it.
    """
    labels = {label for label, _, _ in fast_result.checks}
    assert set(lab05.TRANSPORT_CHECK_LABELS) <= labels
    assert fast_result.skipped_labels == frozenset(lab05.TRANSPORT_CHECK_LABELS)


def test_the_full_run_skips_nothing(full_result: LabRunResult) -> None:
    """With MCP on, no check is skipped."""
    assert full_result.with_mcp is True
    assert full_result.skipped_labels == frozenset()
    assert full_result.skipped == ()


def test_each_check_label_is_unique(fast_result: LabRunResult) -> None:
    """No label appears twice.

    Duplicated labels are how a skipped check hides behind a passing one of the
    same name, and how a reader counts checks and gets the wrong answer.
    """
    labels = [label for label, _, _ in fast_result.checks]
    assert len(labels) == len(set(labels)), [
        label for label in labels if labels.count(label) > 1
    ]


def test_a_failed_check_makes_the_run_fail() -> None:
    """A synthetic failure propagates: ``passed`` is not hardcoded true.

    Constructed by hand rather than by breaking the connector, so the test is
    fast and does not depend on being able to sabotage a real one.
    """
    good = lab05.LabRunResult.__dataclass_fields__
    assert "checks" not in good, "checks is a property, not a field"

    # The real report, with one observation's count forced to zero.
    observations = [
        ReadObservation(
            pseudocode_line="read('the notes/materials store')",
            endpoint="read_notes_store",
            declared_doc_ref="doc 2.10 Lab 5",
            declared_origin="documented",
            documented_endpoint=True,
            scope=NOTES_READ,
            outcome="allowed",
            error="",
            record_count=0,
            summary="",
            provenance="data/seed",
        )
    ]
    registry = build_default_connector(source="local", token="unused-token")
    result = LabRunResult(
        run_id="t-synthetic",
        course_code="CS201",
        source="local",
        endpoint_names=("read_notes_store",),
        observations=observations,
        rows=(),
        transport=TransportReport((), (), (), 0.0, (), ([], []), 0),
        swap=SwapReport(
            before=SwapLeg("local", "CS201", "X", 1, ("a",), "d"),
            after=SwapLeg("local", "CS201", "X", 1, ("a",), "d"),
            tool_names=("read_notes_store",),
            query_keys=("course_code",),
            result_type="NotesReadResult",
            code=lab05.SWAP_AGENT_CODE,
        ),
        bypass=BypassObservation(
            withheld_records=0,
            withheld_description="",
            student_error="",
            student_message="",
            student_own_read_allowed=0,
            ledger_rows_for_endpoint=0,
            ledger_names_the_caller=False,
            ledger_names_the_scope=False,
            read_params=("endpoint", "payload"),
        ),
        ledger_report="",
        tools_ledger_report="",
        with_mcp=False,
        total_elapsed_ms=0.0,
    )
    del registry
    assert result.passed is False
    assert result.failed, "a zero-record documented read must fail a check"


def test_the_cli_exits_zero_on_the_fast_path() -> None:
    """``python -m ...lab05_mcp_connector --no-mcp`` exits 0 and says it skipped.

    The exit code is the acceptance criterion, so it is asserted directly rather
    than inferred from a captured report.
    """
    code = lab05._main(["--no-mcp", "--log-level", "ERROR", "--run-id", "t-cli"])
    assert code == 0, "the in-process run must pass on its own evidence"


def test_the_module_exposes_its_run_id_convention() -> None:
    """``LAB`` is the run-id prefix, so ledger rows are attributable to a lab."""
    assert LAB == "lab05_mcp_connector"
    assert LAB.startswith("lab05")
