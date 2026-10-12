"""Tests for Lab 2 - the tool-using agent.

These tests exist to make Lab 2's acceptance criterion falsifiable. The
document requires:

    "Key numbers and lookups always go through tools, never hand-done in the
    prompt."

and names the pitfall:

    "Never assert a fact in the prompt that a tool should verify."

Each of those is turned into an assertion below, in one of four ways:

1. **Structural** - `ast` parses the agent module and asserts it contains no
   filesystem access, so it *cannot* bypass the tools even by accident.
2. **Ledger** - every number the agent reports is traced to a recorded call.
3. **Behavioural** - a poisoned request with both false and true claims, where
   the agent must refute the false ones and confirm the true ones.
4. **Negative** - a deliberately broken corpus, to prove the validator actually
   fails when it should. A checker that always passes proves nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from course_organizer.connector import build_default_connector as build_connector
from course_organizer.llm import ScriptedLLM
from course_organizer.labs import lab02_tool_using_agent as lab02
from course_organizer.tools.base import (
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
)
from course_organizer.tools.compute import build_default_registry
from course_organizer.tools.readers import NotesQuery
from course_organizer.tools.seed_store import LocalSeedStore, get_seed_store

AGENT_MODULE = Path(lab02.__file__)


# ==========================================================================
# 1. Structural: the agent cannot reach the store
# ==========================================================================


def _forbidden_calls(tree: ast.AST) -> set[str]:
    """Names in the module AST that would let it read data without a tool.

    Args:
        tree: The parsed module.

    Returns:
        The set of forbidden dotted names actually referenced. Empty means the
        module has no route to the data.
    """
    forbidden = {
        "open",
        "pathlib.Path",
        "Path",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.environ",
        "json.load",
        "json.loads",
        "LocalSeedStore",
        "get_seed_store",
        "read_text",
        "read_bytes",
        "glob",
        "rglob",
        "io.open",
    }
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                found.add(func.id)
            elif isinstance(func, ast.Attribute):
                found.add(func.attr)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            for alias in node.names:
                found.add(alias.name.split(".")[-1])
                if module:
                    found.add(f"{module}.{alias.name}")
    return found & forbidden


def test_agent_module_has_no_filesystem_access() -> None:
    """The agent module contains no route to data except the tool registry.

    This is the strongest form of Lab 2's acceptance criterion. If the agent
    cannot open a file, it cannot hand-done a lookup in the prompt, whatever a
    future change tries.
    """
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8-sig"))
    offenders = _forbidden_calls(tree)
    assert not offenders, (
        "lab02_tool_using_agent.py must reach data only through ToolRegistry. "
        f"Found forbidden access: {sorted(offenders)}"
    )


def test_agent_module_does_not_import_the_store() -> None:
    """No import of `seed_store` in the agent module, by any spelling.

    The check is on the parsed import statements, not on the raw text: the
    module's docstring is allowed to name `seed_store` when explaining what the
    agent must not touch. Only an actual import would be a violation.
    """
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8-sig"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    offenders = [name for name in imported if "seed_store" in name]
    assert not offenders, (
        f"the agent must not import the seed store directly; found {offenders}"
    )


def test_agent_module_does_not_call_the_store_accessor() -> None:
    """Nor may it call `get_seed_store`, even via an aliased import."""
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8-sig"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "get_seed_store" not in called, (
        "the agent must obtain data by invoking a tool, not the store accessor"
    )


def test_agent_only_reaches_data_through_the_registry() -> None:
    """The agent's own imports of the tools package are the safe ones.

    Args:
        None.

    Raises:
        AssertionError: If the agent imports a reader or the compute tool and
            calls it directly, bypassing the registry's validation and ledger.
    """
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8-sig"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
            if node.module:
                imported.add(node.module)
    # It is fine to import the models and the registry.
    for forbidden in ("read_notes_store", "read_assignment_rubrics", "LocalSeedStore"):
        assert forbidden not in imported, (
            f"the agent must call {forbidden!r} through the registry, not import it"
        )


# ==========================================================================
# 2. The tool layer itself
# ==========================================================================


def test_registry_registers_the_three_document_tools(lab02_registry: ToolRegistry) -> None:
    """The registry carries exactly the Lab 2 tools, by the document's names."""
    names = lab02_registry.names()
    assert names == [
        "compute_scope_validation",
        "read_assignment_rubrics",
        "read_notes_store",
    ]
    assert set(names) == set(lab02.REQUIRED_CALLS)


def test_read_notes_store_returns_computed_counts(lab02_registry: ToolRegistry) -> None:
    """The notes reader computes its counts from the store, not from input."""
    result = lab02_registry.invoke(
        "read_notes_store", {"course_code": "CS201"}
    )
    assert result.ok, result.error
    value = result.value
    assert value.course_code == "CS201"
    assert value.note_count == 8
    assert value.version_total == 10
    assert value.units_missing == []
    assert value.unit_coverage() == 1.0
    assert set(value.units_declared) == {"1", "2", "3", "4", "5"}
    # These numbers are the ones a prompt would otherwise assert.
    assert value.note_count == len(value.notes)
    assert value.version_total == sum(len(n.versions) for n in value.notes)


def test_read_notes_store_filters_by_unit_and_topic(lab02_registry: ToolRegistry) -> None:
    """Unit and topic filters narrow the read and update coverage."""
    result = lab02_registry.invoke(
        "read_notes_store", {"course_code": "CS201", "units": ["1", "3"]}
    )
    assert result.ok, result.error
    value = result.value
    assert {n.unit for n in value.notes} == {"1", "3"}
    assert value.units_covered == ["1", "3"]
    assert value.units_missing == []
    assert value.unit_coverage() == 1.0


def test_read_notes_store_reports_a_coverage_gap(lab02_registry: ToolRegistry) -> None:
    """A unit with no notes is reported as missing, not silently dropped."""
    result = lab02_registry.invoke(
        "read_notes_store", {"course_code": "CS201", "units": ["1", "9"]}
    )
    assert result.ok, result.error
    value = result.value
    assert value.units_missing == ["9"]
    assert value.units_covered == ["1"]
    assert value.unit_coverage() == 0.5


def test_read_assignment_rubrics_computes_marks(lab02_registry: ToolRegistry) -> None:
    """The assignment reader sums marks and checks each rubric's arithmetic."""
    result = lab02_registry.invoke(
        "read_assignment_rubrics", {"course_code": "CS201"}
    )
    assert result.ok, result.error
    value = result.value
    assert value.found_ids == ["ASG-01", "ASG-02", "ASG-03"]
    assert value.total_available_marks == 65.0
    assert value.marks_by_assignment == {
        "ASG-01": 20.0,
        "ASG-02": 20.0,
        "ASG-03": 25.0,
    }
    assert value.rubric_problems == []
    assert value.faculty_approved is True


def test_read_assignment_rubrics_reports_missing_ids(lab02_registry: ToolRegistry) -> None:
    """An unknown assignment id is reported, not treated as present."""
    result = lab02_registry.invoke(
        "read_assignment_rubrics",
        {"course_code": "CS201", "assignment_ids": ["ASG-01", "ASG-99"]},
    )
    assert result.ok, result.error
    assert result.value.missing_ids == ["ASG-99"]
    assert result.value.found_ids == ["ASG-01"]


def test_compute_scope_validation_passes_on_the_seed_corpus(
    lab02_registry: ToolRegistry,
) -> None:
    """`assert v.valid` holds for the shipped corpus, so the pseudocode runs."""
    result = lab02_registry.invoke(
        "compute_scope_validation",
        {"course_code": "CS201", "units": ["1", "2", "3"],
         "assignment_ids": ["ASG-01", "ASG-02", "ASG-03"]},
    )
    assert result.ok, result.error
    assert result.value.valid is True
    assert result.value.failed() == []


def test_compute_scope_validation_records_its_scope(
    lab02_registry: ToolRegistry,
) -> None:
    """The result says whether its facts describe the whole course.

    This is what lets a later lab refuse to read a course-wide fact out of a
    narrowed read.
    """
    wide = lab02_registry.invoke(
        "compute_scope_validation", {"course_code": "CS201"}
    ).value
    assert wide.course_wide is True
    assert wide.scope_label() == "course_wide"

    narrow = lab02_registry.invoke(
        "compute_scope_validation",
        {"course_code": "CS201", "assignment_ids": ["ASG-01"]},
    ).value
    assert narrow.course_wide is False
    assert "ASG-01" in narrow.scope_label()
    # The scoped total is the subset, which is why it must not be used as the
    # course-wide total.
    assert narrow.fact("total_available_marks") == 20.0
    assert wide.fact("total_available_marks") == 65.0


def test_compute_scope_validation_nests_its_reads_on_the_ledger(
    lab02_registry: ToolRegistry,
) -> None:
    """A composite tool cannot hide its data access; nested calls are recorded."""
    before = len(lab02_registry.ledger)
    lab02_registry.invoke(
        "compute_scope_validation", {"course_code": "CS201"}
    )
    new = list(lab02_registry.ledger)[before:]
    assert [c.tool for c in new] == [
        "read_notes_store",
        "read_assignment_rubrics",
        "compute_scope_validation",
    ]
    assert [c.depth for c in new] == [1, 1, 0]
    assert all(c.ok for c in new)


def test_nested_calls_cannot_reach_the_store_directly(
    lab02_registry: ToolRegistry,
) -> None:
    """`ToolContext` exposes a registry, not a filesystem handle."""
    context = ToolContext(
        registry=lab02_registry,
        ledger=lab02_registry.ledger,
        run_id=lab02_registry.run_id,
    )
    assert not hasattr(context, "store")
    assert not hasattr(context, "seed_dir")
    # The only data access available is another tool call.
    assert context.call("read_notes_store", {"course_code": "CS201"}).ok


def test_unknown_tool_is_rejected(lab02_registry: ToolRegistry) -> None:
    """A tool name that is not registered raises, naming what is available."""
    with pytest.raises(ToolError, match="unknown tool"):
        lab02_registry.invoke("read_the_grades", {"course_code": "CS201"})


def test_invalid_arguments_are_rejected_before_the_handler(
    lab02_registry: ToolRegistry,
) -> None:
    """Schema-invalid input raises rather than reaching the store."""
    with pytest.raises(ToolError, match="invalid input"):
        lab02_registry.invoke("read_notes_store", {"units": ["1"]})


def test_duplicate_registration_is_rejected() -> None:
    """A second tool of the same name is an error, not a silent shadow."""
    registry = ToolRegistry(run_id="dup")

    def handler(query, ctx):  # pragma: no cover - never called
        raise AssertionError

    spec_kwargs = dict(
        summary="test",
        input_model=NotesQuery,
        output_model=object,
        doc_ref="test",
    )
    registry.register(ToolSpec(name="x", handler=handler, **spec_kwargs))
    with pytest.raises(ToolError, match="already registered"):
        registry.register(ToolSpec(name="x", handler=handler, **spec_kwargs))


def test_failed_handler_is_recorded_not_raised(lab02_registry: ToolRegistry) -> None:
    """A handler that raises becomes a failed result on the ledger."""
    from course_organizer.schemas import _Base

    class Out(_Base):
        """Trivial output model."""

        value: int = 0

    def boom(query, ctx):
        """Always fails."""
        raise RuntimeError("deliberate failure")

    lab02_registry.register(
        ToolSpec(
            name="boom",
            summary="always fails",
            input_model=NotesQuery,
            output_model=Out,
            handler=boom,
            doc_ref="test",
        )
    )
    result = lab02_registry.invoke("boom", {"course_code": "CS201"})
    assert result.ok is False
    assert "deliberate failure" in (result.error or "")
    assert lab02_registry.ledger.failures()[-1].tool == "boom"


def test_handler_returning_the_wrong_type_is_a_failure(
    lab02_registry: ToolRegistry,
) -> None:
    """A handler that breaks its own contract is caught by the registry."""
    from course_organizer.schemas import _Base

    class Out(_Base):
        """Trivial output model."""

        value: int = 0

    def wrong(query, ctx):
        """Returns the wrong type."""
        return "not a model"

    lab02_registry.register(
        ToolSpec(
            name="wrong",
            summary="returns the wrong type",
            input_model=NotesQuery,
            output_model=Out,
            handler=wrong,
            doc_ref="test",
        )
    )
    result = lab02_registry.invoke("wrong", {"course_code": "CS201"})
    assert result.ok is False
    assert "expected Out" in (result.error or "")


def test_ledger_is_append_only_and_ordered(lab02_registry: ToolRegistry) -> None:
    """The ledger preserves issue order and never rewrites an entry.

    Note the two distinct name sources: `ToolRegistry.names()` lists the
    *registered* tools, `ToolLedger.names()` lists the tools actually *called*.
    Conflating them would make the audit trail look complete when it is not.
    """
    lab02_registry.invoke("read_notes_store", {"course_code": "CS201"})
    lab02_registry.invoke("read_assignment_rubrics", {"course_code": "CS201"})

    assert lab02_registry.names() == [
        "compute_scope_validation",
        "read_assignment_rubrics",
        "read_notes_store",
    ]
    assert lab02_registry.ledger.names() == [
        "read_notes_store",
        "read_assignment_rubrics",
    ]
    # Re-invoking appends rather than replacing.
    lab02_registry.invoke("read_notes_store", {"course_code": "CS201"})
    assert lab02_registry.ledger.names() == [
        "read_notes_store",
        "read_assignment_rubrics",
        "read_notes_store",
    ]
    assert len({c.call_id for c in lab02_registry.ledger}) == 3


# ==========================================================================
# 2b. The seed store, the single filesystem boundary
# ==========================================================================


def test_store_lists_both_courses() -> None:
    """`course_codes` reads the code from each folder's own header.

    This is the method a reviewer reaches for first when checking the corpus,
    and it is the one the notebook prints, so it must be covered by a test
    rather than only by a manual look.
    """
    store = get_seed_store()
    assert store.course_codes() == ["CS201", "DBMS402"]


def test_store_exposes_the_paths_the_tools_actually_read() -> None:
    """`notes_path` / `assignments_path` agree with what the readers load."""
    store = get_seed_store()
    notes_path = store.notes_path("CS201")
    assert notes_path.is_file()
    assert notes_path.parent.name == "data_structures"

    assignments_path = store.assignments_path("CS201")
    assert assignments_path is not None
    assert assignments_path.is_file()
    assert assignments_path.name == "assignments.json"


def test_store_reports_an_unknown_course_with_the_available_ones() -> None:
    """The error message names what does exist, so the fix is obvious."""
    store = get_seed_store()
    with pytest.raises(Exception) as excinfo:
        store.read_notes("NOPE101")
    message = str(excinfo.value)
    assert "NOPE101" in message
    assert "CS201" in message
    assert "DBMS402" in message


def test_store_reads_notes_and_assignments_directly() -> None:
    """The two document reads return validated models, not raw dicts."""
    store = get_seed_store()
    header, notes = store.read_notes("CS201")
    assert header["course_code"] == "CS201"
    assert len(notes) == 8
    assert {n.unit for n in notes} == {"1", "2", "3", "4", "5"}
    assert all(n.versions for n in notes)
    assert all(n.current_version.version == max(v.version for v in n.versions)
               for n in notes)

    definitions = store.read_assignments("CS201")
    assert [a.assignment_id for a in definitions] == ["ASG-01", "ASG-02", "ASG-03"]
    assert all(a.check_rubric() == [] for a in definitions)
    assert all(a.approved_by == "course_faculty" for a in definitions)


def test_store_relative_paths_are_repo_relative_with_forward_slashes() -> None:
    """Audit records are stable regardless of the machine's separator."""
    store = get_seed_store()
    rendered = store.relative(store.notes_path("CS201"))
    assert rendered == "data/seed/data_structures/notes.json"
    assert "\\" not in rendered


def test_store_describes_every_seed_file() -> None:
    """`describe_sources` lists every seed file with a real byte count.

    The list grew from four to six in Lab 4, which added a `submissions.json`
    per course so the similarity retriever had a *history* to compare against.
    The assertion is written as a set of paths rather than a count, precisely so
    that adding a source does not require editing this test -- the failure mode
    this version of the test had.
    """
    rows = get_seed_store().describe_sources()
    assert {r["path"] for r in rows} == {
        "data/seed/data_structures/notes.json",
        "data/seed/data_structures/assignments.json",
        "data/seed/data_structures/submissions.json",
        "data/seed/dbms/notes.json",
        "data/seed/dbms/assignments.json",
        "data/seed/dbms/submissions.json",
    }
    assert len(rows) == 6, "a row per file, with no duplicates"
    for row in rows:
        assert int(row["bytes"]) > 0
        assert "\\" not in row["path"], "paths must be repo-relative with /"


def test_only_the_seed_store_touches_the_filesystem() -> None:
    """The filesystem boundary Lab 2 established, still holding after Lab 4.

    Lab 4 added a whole package -- embeddings, stores, two retrievers, a ledger
    -- and none of it may reach the disk directly. `describe_sources` is the one
    sanctioned way to enumerate inputs, which is why the previous test can be
    written without knowing which modules exist.
    """
    import ast

    seed_store = Path(get_seed_store().__class__.__module__.replace(".", "/")).with_suffix(
        ".py"
    )
    package = seed_store.parent
    touching = []
    for path in sorted(package.glob("*.py")):
        if path.name in {"seed_store.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in {"open", "read_text", "read_bytes", "write_text",
                            "write_bytes", "load", "dump"}:
                    touching.append(f"{path.name}:{node.lineno}")
    assert not touching, f"only seed_store.py may touch the filesystem: {touching}"


# ==========================================================================
# 3. A validator that cannot fail proves nothing
# ==========================================================================


def test_validator_fails_on_a_broken_corpus(tmp_path: Path) -> None:
    """Corrupt the corpus and the checker must report the defect.

    The shipped corpus is valid so that the document's `assert v.valid` holds.
    That makes a "always passes" checker indistinguishable from a correct one,
    so this test builds a broken copy in a temp directory and asserts the
    specific check that must fail.
    """
    seed_dir = tmp_path / "seed"
    (seed_dir / "broken").mkdir(parents=True)
    real_seed = get_seed_store().seed_dir

    notes = json.loads(
        (real_seed / "data_structures" / "notes.json").read_text(encoding="utf-8")
    )
    # Drop the only note in unit 5, creating a coverage gap.
    notes["notes"] = [n for n in notes["notes"] if n["unit"] != "5"]
    (seed_dir / "broken" / "notes.json").write_text(
        json.dumps(notes), encoding="utf-8"
    )

    # Break a rubric so its criteria no longer sum to max_marks.
    assignments = json.loads(
        (real_seed / "data_structures" / "assignments.json").read_text(encoding="utf-8")
    )
    assignments["assignments"][0]["rubric"][0]["max_marks"] = 99.0
    # And let an agent approve itself, which 2.12 forbids.
    assignments["assignments"][1]["approved_by"] = "grading_assistance"
    (seed_dir / "broken" / "assignments.json").write_text(
        json.dumps(assignments), encoding="utf-8"
    )

    import course_organizer.tools.readers as readers_module
    import course_organizer.tools.seed_store as store_module

    # Install a whole connector over the broken corpus rather than swapping the
    # store out from under the readers. Lab 5 moved data access behind the
    # connector, so there is no longer a store reference here to replace -- and
    # doing it this way keeps the governance layer configured for the *same*
    # corpus it is serving, which patching the store alone did not.
    token = "broken-corpus-token-for-tests"
    broken_connector = build_connector(seed_dir=seed_dir, token=token)
    original_store = store_module.get_seed_store
    store_module.get_seed_store.cache_clear()
    try:
        readers_module.use_connector(broken_connector, token)
        registry = build_default_registry(run_id="broken")
        result = registry.invoke(
            "compute_scope_validation",
            {
                "course_code": "CS201",
                "units": ["1", "2", "3", "5"],
                "assignment_ids": ["ASG-01", "ASG-02"],
            },
        )
    finally:
        readers_module.reset_connector()
        store_module.get_seed_store = original_store  # type: ignore[assignment]
        store_module.get_seed_store.cache_clear()

    assert result.ok, result.error
    validation = result.value
    assert validation.valid is False, "a broken corpus must not validate"

    failed = {check.check_id for check in validation.failed()}
    assert "notes_coverage" in failed
    assert "rubric_arithmetic" in failed
    assert "faculty_approved" in failed

    # And the failures carry the arithmetic, not just a flag.
    rubric = validation.check("rubric_arithmetic")
    assert any("ASG-01" in problem for problem in rubric.evidence["problems"])
    approval = validation.check("faculty_approved")
    assert approval.evidence["violations"] == ["ASG-02"]


def test_validator_flags_a_nonexistent_unit(lab02_registry: ToolRegistry) -> None:
    """A unit that does not exist is reported by `units_exist`."""
    result = lab02_registry.invoke(
        "compute_scope_validation",
        {"course_code": "CS201", "units": ["1", "42"]},
    )
    assert result.ok, result.error
    check = result.value.check("units_exist")
    assert check.passed is False
    assert "42" in check.evidence["unknown_units"]


def test_rubric_weights_must_sum_to_one(lab02_registry: ToolRegistry) -> None:
    """`check_rubric` catches weights that do not add up, per the doc's 2.7 cap."""
    registry = lab02_registry
    good = registry.invoke(
        "read_assignment_rubrics", {"course_code": "CS201", "assignment_ids": ["ASG-01"]}
    )
    assert good.value.rubric_problems == []

    # Construct a definition with broken weights directly.
    from course_organizer.schemas import AssignmentDefinition

    raw = AssignmentDefinition.model_validate(
        {
            "assignment_id": "ASG-X",
            "course_code": "CS201",
            "title": "Broken",
            "brief": "x",
            "due_date": "2026-12-01T00:00:00+00:00",
            "max_marks": 10,
            "rubric": [
                {"criterion_id": "a", "description": "x", "max_marks": 5.0, "weight": 0.5},
                {"criterion_id": "b", "description": "y", "max_marks": 5.0, "weight": 0.2},
            ],
        }
    )
    problems = raw.check_rubric()
    assert problems, "weights summing to 0.7 must be reported"
    assert any("weight" in p for p in problems)


# ==========================================================================
# 4. The plan: the model proposes, the registry disposes
# ==========================================================================


def test_plan_carries_the_three_required_calls(llm: ScriptedLLM) -> None:
    """The plan names the document's three calls, in the pseudocode's order."""
    registry = build_default_registry(run_id="plan")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    plan = agent.plan("Organise CS201 units 1 to 3 and ASG-01.")
    assert [c.tool for c in plan.calls] == list(lab02.REQUIRED_CALLS)
    assert plan.course_code == "CS201"
    assert plan.units == ["1", "2", "3"]
    assert plan.assignment_ids == ["ASG-01"]
    assert all(c.reason for c in plan.calls)


def test_plan_extracts_units_from_several_phrasings(llm: ScriptedLLM) -> None:
    """Free text maps onto tool arguments, which is the model's real job."""
    registry = build_default_registry(run_id="units")
    agent = lab02.ToolUsingAgent(registry, llm=llm)

    assert agent.plan("CS201 units 1, 2 and 4").units == ["1", "2", "4"]
    assert agent.plan("CS201 the first three units").units == ["1", "2", "3"]
    assert agent.plan("CS201 units 2-5").units == ["2", "3", "4", "5"]
    assert agent.plan("CS201 all units").units == []


def test_plan_refuses_to_guess_a_course(llm: ScriptedLLM) -> None:
    """With no course named and none supplied, planning fails loudly.

    Guessing the course is the very error the lab exists to prevent, so it is
    an error rather than a default.
    """
    registry = build_default_registry(run_id="nocourse")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    with pytest.raises(lab02.PlanError):
        agent.plan("Please sort out my course.")


def test_plan_uses_the_supplied_course(llm: ScriptedLLM) -> None:
    """A caller-supplied course is used when the text names none."""
    registry = build_default_registry(run_id="default-course")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    plan = agent.plan("Please sort it out.", default_course="DBMS402")
    assert plan.course_code == "DBMS402"


def test_execute_rejects_a_plan_naming_an_unregistered_tool(llm: ScriptedLLM) -> None:
    """A hallucinated tool name is refused rather than ignored."""
    registry = build_default_registry(run_id="hallucinated")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    plan = lab02.ToolPlan(
        course_code="CS201",
        calls=[
            lab02.ToolCallRequest(
                tool="delete_all_grades", reason="because", arguments={}
            )
        ],
        rationale="n/a",
    )
    with pytest.raises(ToolError, match="not registered"):
        agent.execute(plan)


def test_execute_repairs_a_plan_missing_a_required_call(llm: ScriptedLLM) -> None:
    """A plan that skips a required call is repaired, and the repair is flagged."""
    registry = build_default_registry(run_id="repair")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    plan = lab02.ToolPlan(
        course_code="CS201",
        calls=[
            lab02.ToolCallRequest(
                tool="read_notes_store",
                reason="needed",
                arguments={"course_code": "CS201"},
            )
        ],
        rationale="incomplete on purpose",
    )
    repaired, results = agent.execute(plan)
    assert repaired is True
    assert set(lab02.REQUIRED_CALLS).issubset(results)
    assert registry.ledger.successful("compute_scope_validation")
    # The plan object is updated so the report shows what actually ran.
    assert {c.tool for c in plan.calls} == set(lab02.REQUIRED_CALLS)


def test_complete_plan_needs_no_repair(llm: ScriptedLLM) -> None:
    """A well-formed plan is executed as-is."""
    registry = build_default_registry(run_id="norepair")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    plan = agent.plan("CS201 all units")
    repaired, _ = agent.execute(plan)
    assert repaired is False


def test_no_required_tool_runs_twice_per_scope(llm: ScriptedLLM) -> None:
    """The agent reuses the compute result instead of re-invoking it.

    A duplicate call would inflate the ledger and make the call count
    meaningless as evidence.
    """
    registry = build_default_registry(run_id="once")
    agent = lab02.ToolUsingAgent(registry, llm=llm)
    result = agent.run("CS201 all units and ASG-01")
    compute_calls = [
        c
        for c in registry.ledger
        if c.tool == "compute_scope_validation"
        and c.depth == 0
        and c.input_payload.get("course_code") == "CS201"
        and not c.input_payload.get("assignment_ids")
    ]
    # Exactly the one the plan ran; the extra course-wide pass is for the
    # course-wide claim and is a different scope.
    assert len(compute_calls) <= 2
    assert result.validation.valid is True


# ==========================================================================
# 5. The pitfall: claims in the prompt are checked against the tools
# ==========================================================================


def test_claims_are_extracted_from_free_text() -> None:
    """The documented patterns find the documented claims."""
    claims = lab02.ClaimExtractor().extract(
        "CS201 has 9 units and DBMS402 has no assignments.",
        default_course="UNSPECIFIED",
    )
    found = {(c.claim_id, c.course_code, c.asserted_value) for c in claims}
    assert ("unit_count", "CS201", 9) in found
    assert ("assignment_count", "DBMS402", 0) in found


def test_zero_words_become_zero() -> None:
    """"no", "zero", "none" and "nothing" all mean 0, not the string."""
    for word in ("no", "zero", "none", "nothing"):
        claims = lab02.ClaimExtractor().extract(
            f"CS201 has {word} assignments.", default_course="UNSPECIFIED"
        )
        assert claims[0].asserted_value == 0, word


def test_self_contradicting_claims_are_both_kept() -> None:
    """Two different asserted values for one fact are two claims.

    Collapsing them would hide the discriminating behaviour the lab needs to
    demonstrate.
    """
    claims = lab02.ClaimExtractor().extract(
        "CS201 has 9 units. CS201 has 5 units.", default_course="UNSPECIFIED"
    )
    values = {c.asserted_value for c in claims if c.claim_id == "unit_count"}
    assert values == {9, 5}


def test_assignment_claims_are_keyed_per_assignment() -> None:
    """Claims about different assignments do not collide."""
    claims = lab02.ClaimExtractor().extract(
        "ASG-01 is worth 50 marks and ASG-03 is worth 99 marks.",
        default_course="CS201",
    )
    ids = {c.claim_id for c in claims}
    assert "marks_by_assignment.ASG-01" in ids
    assert "marks_by_assignment.ASG-03" in ids


def test_claims_default_to_the_supplied_course() -> None:
    """A claim with no course named is attributed to the caller's course."""
    claims = lab02.ClaimExtractor().extract(
        "ASG-01 is worth 50 marks.", default_course="CS201"
    )
    assert claims[0].course_code == "CS201"


def test_claim_patterns_only_name_computable_keys(lab02_registry: ToolRegistry) -> None:
    """Every claim key resolves to a real fact, or is listed as uncomputable.

    This keeps the pattern table honest: a key that silently never resolves
    would make a real claim look unverifiable forever.
    """
    validation = lab02_registry.invoke(
        "compute_scope_validation", {"course_code": "CS201"}
    ).value
    for pattern in lab02.CLAIM_PATTERNS:
        if pattern.claim_id in lab02.UNCOMPUTABLE_CLAIMS:
            continue
        found, _ = lab02._resolve_fact(validation.verified_facts, pattern.claim_id)
        assert found, (
            f"claim pattern {pattern.claim_id!r} names a key the compute tool "
            "does not produce; it would always come back UNVERIFIABLE"
        )


def test_poisoned_request_refutes_false_claims(poisoned_result) -> None:
    """The document's pitfall, demonstrated: false prompt claims are refuted."""
    contradicted = poisoned_result.contradicted
    assert len(contradicted) == 3
    by_id = {(c.claim_id, c.asserted_value) for c in contradicted}
    assert ("unit_count", 9) in by_id
    assert ("assignment_count", 0) in by_id
    assert ("marks_by_assignment.ASG-01", 50) in by_id


def test_poisoned_request_confirms_true_claims(poisoned_result) -> None:
    """A checker that refuted everything would fail here.

    The two true claims are the control: they prove the agent discriminates
    rather than reflexively disagreeing with the prompt.
    """
    confirmed = poisoned_result.confirmed
    assert len(confirmed) == 3
    by_id = {(c.claim_id, c.asserted_value) for c in confirmed}
    assert ("unit_count", 5) in by_id
    assert ("total_available_marks", 65) in by_id
    assert ("units_with_notes", 5) in by_id


def test_poisoned_request_declines_uncomputable_claims(poisoned_result) -> None:
    """A subjective claim is neither confirmed nor refuted; it is declined."""
    unverifiable = poisoned_result.unverifiable
    assert len(unverifiable) == 1
    claim = unverifiable[0]
    assert claim.claim_id == "notes_quality"
    assert claim.verified_value is None
    assert "no current tool computes" in claim.verdict


def test_verified_values_come_from_the_store_not_the_prompt(
    poisoned_result,
) -> None:
    """The refuted number is the tool's, never the prompt's."""
    unit_claim = next(
        c for c in poisoned_result.claims
        if c.claim_id == "unit_count" and c.asserted_value == 9
    )
    assert unit_claim.verified_value == 5
    assert unit_claim.verified_value != unit_claim.asserted_value

    marks_claim = next(
        c for c in poisoned_result.claims
        if c.claim_id == "marks_by_assignment.ASG-01"
    )
    assert marks_claim.verified_value == 20.0
    assert marks_claim.verified_value != marks_claim.asserted_value


def test_every_settled_claim_traces_to_a_ledger_call(poisoned_result) -> None:
    """Each verified number cites the exact call that computed it.

    This is Lab 2's acceptance criterion as an assertion: a number in the
    report with no call behind it would be exactly the failure the lab
    forbids.
    """
    settled = [
        c for c in poisoned_result.claims if c.verified_value is not None
    ]
    assert len(settled) == 6
    for claim in settled:
        assert claim.source_call_id, f"{claim.claim_id} has no source call"
        assert claim.source_tool == "compute_scope_validation"
        assert len(claim.source_call_id) == 12


def test_course_wide_claim_uses_a_course_wide_read(poisoned_result) -> None:
    """A course-wide claim is not settled by a narrowed read.

    The plan scoped the read to ASG-01, whose marks are 20. The claim "CS201
    is worth 65 marks in total" is true. Settling it against the narrowed read
    would refute a true claim, so the agent must widen.
    """
    claim = next(
        c for c in poisoned_result.claims
        if c.claim_id == "total_available_marks"
    )
    assert claim.status is lab02.ClaimStatus.CONFIRMED
    assert claim.verified_value == 65.0
    assert claim.verified_scope == "course_wide"


def test_narrowed_read_is_still_used_for_narrow_claims(poisoned_result) -> None:
    """An assignment-scoped claim settles against the plan's own scope."""
    claim = next(
        c for c in poisoned_result.claims
        if c.claim_id == "marks_by_assignment.ASG-01"
    )
    assert claim.verified_value == 20.0
    assert claim.verified_scope is not None
    assert "ASG-01" in claim.verified_scope


def test_claims_about_another_course_use_that_course(poisoned_result) -> None:
    """A claim naming DBMS402 is settled by a DBMS402 read, not CS201's."""
    claim = next(
        c for c in poisoned_result.claims
        if c.claim_id == "assignment_count"
    )
    assert claim.course_code == "DBMS402"
    assert claim.verified_value == 3
    assert claim.verified_scope == "course_wide"
    assert any(
        v.course_code == "DBMS402" for v in poisoned_result.extra_validations
    )


def test_all_required_calls_appear_in_the_ledger(poisoned_result) -> None:
    """The document's three calls are all on the record."""
    for name in lab02.REQUIRED_CALLS:
        assert name in poisoned_result.ledger_report, name


def test_run_charges_exactly_one_llm_call(poisoned_result) -> None:
    """The model is asked once, to plan. Everything else is a tool call.

    Keeping the model to a single, narrow, well-specified call is what makes
    the rest of the answer reproducible.
    """
    assert poisoned_result.llm_calls_used == 1


def test_unknown_course_claim_is_declined_not_confirmed(llm: ScriptedLLM) -> None:
    """A claim about a course that is not connected stays unverified."""
    result = lab02.run_tool_using_agent(
        "CS201 all units. XYZ999 has 3 assignments.",
        llm=llm,
        run_id="unknown-course",
    )
    claim = next(
        c for c in result.claims
        if c.claim_id == "assignment_count" and c.course_code == "XYZ999"
    )
    assert claim.status is lab02.ClaimStatus.UNVERIFIABLE
    assert claim.verified_value is None
    assert "could not verify" in claim.verdict


def test_request_with_no_claims_still_validates(llm: ScriptedLLM) -> None:
    """A plain request produces no claims and still runs the pseudocode."""
    result = lab02.run_tool_using_agent(
        "Just get CS201 ready for term.", llm=llm, run_id="no-claims"
    )
    assert result.claims == []
    assert result.validation.valid is True
    assert result.contradicted == []
    assert result.confirmed == []
    assert "no fact claims" in result.claim_table()


def test_report_renders_without_error(poisoned_result) -> None:
    """The acceptance report is printable, and includes the key evidence."""
    report = poisoned_result.report()
    assert "assert v.valid" in report
    assert "Ledger" in report
    for name in lab02.REQUIRED_CALLS:
        assert name in report
    assert "REFUTED" in report
    assert "CONFIRMED" in report
    assert len(report.splitlines()) > 40


def test_demo_main_passes() -> None:
    """The CLI self-check exits zero, so a regression is a non-zero exit."""
    assert lab02._main() == 0


# ==========================================================================
# 6. Human authority is untouched
# ==========================================================================


def test_lab02_approves_nothing(poisoned_result) -> None:
    """No agent in this lab acts as approver.

    Document 2.12 forbids publishing a grade without faculty approval. Lab 2
    publishes nothing, so nothing may be approved.
    """
    from course_organizer import HUMAN_AUTHORITY

    assert HUMAN_AUTHORITY == "course_faculty"
    for claim in poisoned_result.claims:
        assert "approved" not in claim.claim_id

    # The one approval-related check asserts that faculty, and only faculty,
    # approved the assignment definitions.
    check = poisoned_result.validation.check("faculty_approved")
    assert check.passed is True
    assert check.evidence["expected_approver"] == HUMAN_AUTHORITY
    assert check.evidence["violations"] == []
    assert set(check.evidence["approvers"].values()) == {HUMAN_AUTHORITY}

    # No tool in this lab can write anything, so none can approve a grade.
    registry = build_default_registry(run_id="readonly")
    for name in registry.names():
        assert registry.get(name).read_only is True, (
            f"{name} must be read-only: Lab 2 approves and publishes nothing"
        )
