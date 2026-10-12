"""Tests for the Lab 6 lab module and the runtime controller's ordering.

Two groups, because they answer two different questions:

* :class:`TestRuntimeOrdering` -- does the controller do things in the *right
  order*? The document's pitfall is about order, so these tests assert sequence,
  not merely outcome. A runtime that produced the right final state by auditing
  after the gate would pass an outcome-only test and still lose state on a crash.
* :class:`TestLabModule` -- does the lab deliver what the document asks for, and
  does it still pass when run twice, in a different order, and from a notebook's
  working directory?

The crash-and-resume proof spawns real subprocesses, so those tests are
slower than the rest of this file. That is deliberate: the acceptance criterion
is about surviving process death, and an in-process simulation of it would prove
nothing that a reader could not have found out by reading the simulation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from course_organizer.labs import lab06_governed_runtime as lab06
from course_organizer.runtime import (
    Budget,
    BudgetExceeded,
    CheckpointUnavailable,
    DenyAllGate,
    GateRefused,
    InMemoryAuditLog,
    InMemoryCheckpointStore,
    JsonlAuditLog,
    PreapprovedGate,
    RecordingGate,
    ResumeDivergence,
    Runtime,
    RuntimeBoundaryError,
    ScriptedGate,
    Step,
    StepContext,
    plan_digest,
)
from course_organizer.runtime.gates import UNANSWERED, ApprovalDecision, ApprovalRequest
from course_organizer.runtime.runtime import (
    DEFAULT_GATE,
    DEFAULT_PRINCIPAL,
    HUMAN_AUTHORITY,
    KIND_DIMENSION,
    StepStatus,
)

#: Where the package lives, for subprocesses that must import it.
SRC = str(Path(__file__).resolve().parents[1] / "src")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    """A fresh in-memory audit log."""
    return InMemoryAuditLog(run_id="test")


@pytest.fixture
def store() -> InMemoryCheckpointStore:
    """A fresh in-memory checkpoint store, for the ordering tests.

    Ordering is a property of the controller, not of SQLite, so these tests use
    the cheap store. The crash proof and
    :class:`TestLabModule::test_the_crash_proof_survives_process_death` use the
    real one.
    """
    return InMemoryCheckpointStore()


@pytest.fixture
def permissive() -> PreapprovedGate:
    """A gate that approves everything."""
    return PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory) -> lab06.LabRunResult:
    """One full Lab 6 run, shared by the acceptance assertions.

    Module-scoped and not per-test: a run spawns two subprocesses for the crash
    proof, so re-running it per assertion would make this file several times
    slower to prove the same thing.
    """
    return lab06.run_lab06(tmp_path_factory.mktemp("lab06-acceptance"))


@pytest.fixture(scope="module")
def crash_proof_root(tmp_path_factory) -> Path:
    """The directory one crash-and-resume proof wrote into."""
    return tmp_path_factory.mktemp("lab06-crash")


@pytest.fixture(scope="module")
def crash_proof(crash_proof_root: Path) -> lab06.CheckpointProof:
    """One crash-and-resume proof: two processes, one log, one checkpoint."""
    return lab06.crash_and_resume(crash_proof_root)


@pytest.fixture(scope="module")
def cli(tmp_path_factory) -> subprocess.CompletedProcess:
    """``python -m course_organizer.labs.lab06_governed_runtime``, once.

    Run as a real subprocess rather than by calling ``_main``: the exit code is
    part of what the lab promises, and capturing it that way would prove nothing.
    """
    return subprocess.run(
        [sys.executable, "-m", "course_organizer.labs.lab06_governed_runtime",
         str(tmp_path_factory.mktemp("lab06-cli"))],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": SRC},
    )


@pytest.fixture(scope="module")
def two_lab_runs(tmp_path_factory):
    """Two independent runs, in two directories. Deliberately repeated work."""
    root = tmp_path_factory.mktemp("lab06-twice")
    return lab06.run_lab06(root / "a"), lab06.run_lab06(root / "b")


def _runtime(
    *,
    audit: InMemoryAuditLog,
    store: InMemoryCheckpointStore,
    gates=None,
    budget: Budget | None = None,
    **kwargs,
) -> Runtime:
    """A runtime over in-memory storage, for order-sensitive tests.

    ``gates=None`` registers no gate at all; a single gate instance is wrapped
    under :data:`DEFAULT_GATE`. Tests that exercise a gate register one
    explicitly, and a test that asserts on ``runtime.gates`` should not find a
    gate it never asked for.
    """
    registry: dict = {}
    if isinstance(gates, dict):
        registry = dict(gates)
    elif gates is not None:
        registry = {DEFAULT_GATE: gates}
    return Runtime(
        budget=budget or Budget.defaults(),
        gates=registry,
        audit=audit,
        checkpoints=store,
        **kwargs,
    )


def _step(name: str, *, kind: str = "tool", risky: bool = False, gate: str = "",
          returns=None, raises: Exception | None = None, record: list | None = None,
          summary: str = ""):
    """Build a step that records its own invocation into ``record``."""
    payload = {"records": 1} if returns is None else dict(returns)
    error = raises

    def body(ctx: StepContext):
        if record is not None:
            record.append(name)
        if error is not None:
            raise error
        return dict(payload)

    return Step(
        name=name,
        intent=f"intent for {name}",
        action=f"action_{name}",
        kind=kind,
        run=body,
        risky=risky,
        gate=gate,
        summary=summary,
    )


# --------------------------------------------------------------------------
# the controller's ordering
# --------------------------------------------------------------------------


class TestRuntimeOrdering:
    """Doc pseudocode vs the pitfall's correction.

    The document's loop audits before it gates and checkpoints after the gate.
    The pitfall corrects both. These tests pin the corrected order.
    """

    def test_steps_run_in_plan_order(self, audit, store) -> None:
        calls: list[str] = []
        plan = [_step(n, record=calls) for n in ("a", "b", "c")]
        report = _runtime(audit=audit, store=store).run(plan)
        assert calls == ["a", "b", "c"]
        assert report.steps_run() == ("a", "b", "c")
        assert report.ok

    def test_the_gate_is_consulted_before_the_risky_step_runs(
        self, audit, store
    ) -> None:
        calls: list[str] = []
        gate = ScriptedGate(
            DEFAULT_GATE,
            [ApprovalDecision.refuse(by="prof", reason="no")],
        )
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE, record=calls)]
        report = _runtime(audit=audit, store=store, gates=gate).run(plan)
        assert calls == [], "the risky step's body must never run"
        assert report.status == "stopped"
        assert report.steps_run() == ()

    def test_a_risky_step_is_checkpointed_before_the_gate_is_consulted(
        self, audit, store
    ) -> None:
        """The pitfall's second clause, asserted as a sequence.

        The gate records how many checkpoints existed when it was asked. If the
        write happened after the question, the count is zero.
        """
        seen: list[int] = []

        class CountingGate(PreapprovedGate):
            def request(self, req: ApprovalRequest) -> ApprovalDecision:
                seen.append(len(store.history(req.thread_id)))
                return super().request(req)

        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        _runtime(
            audit=audit, store=store, gates=CountingGate(DEFAULT_GATE, by="prof")
        ).run(plan)
        assert seen == [1], (
            "the gate was asked before anything was checkpointed, so a crash "
            "inside wait_for_approval would resume without recording the question"
        )

    def test_the_audit_record_names_the_pre_step_checkpoint(self, audit, store) -> None:
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        _runtime(
            audit=audit, store=store, gates=PreapprovedGate(DEFAULT_GATE, by="prof")
        ).run(plan)
        record = audit.for_step("publish")[-1]
        assert record.checkpoint_id, "the gated record must name its pre-step checkpoint"

    def test_every_step_gets_exactly_one_audit_record(self, audit, store) -> None:
        plan = [_step(n) for n in ("a", "b", "c")]
        _runtime(audit=audit, store=store).run(plan)
        assert len(audit) == 3
        assert [r.step for r in audit] == ["a", "b", "c"]

    def test_the_audit_record_carries_the_documents_triple(self, audit, store) -> None:
        """`audit.log(step.intent, step.action, step.result)`."""
        plan = [_step("a")]
        _runtime(audit=audit, store=store).run(plan)
        record = audit.records()[0]
        assert record.intent == "intent for a"
        assert record.action == "action_a"
        assert dict(record.outputs) == {"records": 1}

    def test_the_runtime_sets_the_principal_not_the_step(self, audit, store) -> None:
        """A step must not be able to log itself under someone else's name."""
        plan = [_step("a")]
        _runtime(
            audit=audit, store=store, principal="coordinator", role="agent"
        ).run(plan)
        record = audit.records()[0]
        assert record.principal == "coordinator"
        assert record.role == "agent"

    def test_a_blank_principal_is_refused_at_construction(self, audit, store) -> None:
        with pytest.raises(ValueError, match="principal must not be blank"):
            _runtime(audit=audit, store=store, principal="   ")

    def test_a_gate_registered_twice_is_refused(self, audit, store) -> None:
        """Pairs, not a dict.

        A dict collapses the duplicate before the runtime can see it, which is why
        the constructor accepts a sequence of pairs as well.
        """
        with pytest.raises(ValueError, match="registered twice"):
            Runtime(
                budget=Budget.defaults(),
                gates=[
                    (DEFAULT_GATE, PreapprovedGate(DEFAULT_GATE)),
                    (DEFAULT_GATE, DenyAllGate(DEFAULT_GATE)),
                ],
                audit=audit,
                checkpoints=store,
            )

    def test_a_dict_of_gates_is_accepted(self, audit, store) -> None:
        rt = _runtime(
            audit=audit,
            store=store,
            gates={"publish": PreapprovedGate("publish"), "other": DenyAllGate("other")},
        )
        assert rt.gates == ("publish", "other")

    def test_a_non_gate_under_a_gate_name_is_refused(self, audit, store) -> None:
        """A step must not find `None` where it expects a gate."""
        with pytest.raises(TypeError, match="not an ApprovalGate"):
            Runtime(
                budget=Budget.defaults(),
                gates=[(DEFAULT_GATE, None)],  # type: ignore[list-item]
                audit=audit,
                checkpoints=store,
            )

    def test_a_blank_gate_name_is_refused(self, audit, store) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            Runtime(
                budget=Budget.defaults(),
                gates=[("  ", PreapprovedGate("x"))],
                audit=audit,
                checkpoints=store,
            )

    def test_a_step_naming_an_unregistered_gate_raises(self, audit, store) -> None:
        """A step that thinks it is gated but is not must fail, not proceed."""
        plan = [_step("publish", risky=True, gate="nonexistent")]
        with pytest.raises(KeyError):
            _runtime(audit=audit, store=store).run(plan)


class TestBudgetEnforcement:
    """A budget checked after the work is a report, not a budget."""

    def test_a_step_beyond_the_budget_never_runs(self, audit, store) -> None:
        calls: list[str] = []
        plan = [_step(n, record=calls) for n in ("a", "b", "c")]
        report = _runtime(
            audit=audit, store=store, budget=Budget.only(steps=2)
        ).run(plan)
        assert calls == ["a", "b"], "the third step's body must never be called"
        assert report.status == "stopped"
        assert report.escalated, "doc 2.12 requires escalation on exhaustion"

    def test_the_refusal_is_not_an_audit_record_because_nothing_acted(
        self, audit, store
    ) -> None:
        """A budget refusal has no inputs or outputs, so there is nothing to log."""
        plan = [_step(n) for n in ("a", "b", "c")]
        _runtime(audit=audit, store=store, budget=Budget.only(steps=2)).run(plan)
        assert len(audit) == 2
        assert [r.step for r in audit] == ["a", "b"]

    def test_each_kind_is_charged_to_its_own_dimension(self, audit, store) -> None:
        plan = [
            _step("t", kind="tool"),
            _step("s", kind="skill"),
            _step("c", kind="connector"),
            _step("l", kind="llm"),
        ]
        report = _runtime(audit=audit, store=store).run(plan)
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["tool_calls"] == 1
        assert spent["skill_calls"] == 1
        assert spent["connector_calls"] == 1
        assert spent["llm_calls"] == 1
        assert spent["steps"] == 4

    def test_memory_work_is_charged_to_the_skill_dimension(self, audit, store) -> None:
        """Lab 4's memory work is bounded like a skill call."""
        report = _runtime(audit=audit, store=store).run([_step("m", kind="memory")])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent[KIND_DIMENSION["memory"]] == 1

    def test_a_declared_cost_is_charged(self, audit, store) -> None:
        step = Step(
            name="read",
            intent="i",
            action="a",
            kind="connector",
            run=lambda ctx: {"records": 3},
            cost={"records_read": 8},
        )
        report = _runtime(audit=audit, store=store).run([step])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["records_read"] == 11, "8 declared plus 3 counted from output"

    def test_the_record_count_is_read_from_a_listed_key_only(self, audit, store) -> None:
        """An unlisted key cannot silently escape the volume budget.

        ``records_read`` is present in the snapshot at 0 because bounded-but-unspent
        dimensions are reported; the assertion is on the *spend*, not on presence.
        """
        step = Step(
            name="read",
            intent="i",
            action="a",
            kind="connector",
            run=lambda ctx: {"my_custom_count": 99},
        )
        report = _runtime(audit=audit, store=store).run([step])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["records_read"] == 0

    def test_an_unparseable_record_count_is_ignored_rather_than_crashing(
        self, audit, store
    ) -> None:
        step = Step(
            name="read",
            intent="i",
            action="a",
            kind="connector",
            run=lambda ctx: {"records": "not a number"},
        )
        report = _runtime(audit=audit, store=store).run([step])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["records_read"] == 0, (
            "a count that cannot be read must not crash the run, and must not "
            "be silently treated as a large one"
        )

    def test_a_step_is_never_double_charged(self, audit, store) -> None:
        report = _runtime(audit=audit, store=store).run([_step("a", kind="tool")])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["tool_calls"] == 1, "reserve commits as it checks; do not re-charge"
        assert spent["steps"] == 1

    def test_the_wall_clock_is_charged_at_least_one_ms(self, audit, store) -> None:
        """Rounding down would leave the dimension at 0 and bound nothing."""
        report = _runtime(audit=audit, store=store).run([_step("fast")])
        spent = {s.dimension: s.spent for s in report.budget}
        assert spent["wall_clock_ms"] >= 1


class TestGateRefusal:
    def test_a_refused_step_produces_no_output(self, audit, store) -> None:
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        gate = DenyAllGate(DEFAULT_GATE, by="prof", reason="not yet")
        report = _runtime(audit=audit, store=store, gates=gate).run(plan)
        assert report.outputs_of("publish") == {}

    def test_the_refusal_is_logged_with_who_and_why(self, audit, store) -> None:
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        gate = DenyAllGate(DEFAULT_GATE, by="prof", reason="rubric needs a look")
        _runtime(audit=audit, store=store, gates=gate).run(plan)
        record = audit.for_step("publish")[-1]
        assert record.outcome == "refused"
        assert record.decided_by == "prof"
        assert record.reason == "rubric needs a look"
        assert record.risky and record.gate == DEFAULT_GATE

    def test_the_refusal_records_the_approval_request_as_inputs(
        self, audit, store
    ) -> None:
        """The pitfall's first clause, applied to a refusal.

        A refusal is the case where a message-shaped log is most tempting and
        most useless: "publish refused" says nothing about what was proposed.
        """
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE, summary="Publish grades")]
        _runtime(
            audit=audit, store=store, gates=DenyAllGate(DEFAULT_GATE, by="prof")
        ).run(plan)
        record = audit.for_step("publish")[-1]
        assert "approval_request" in record.inputs
        assert record.inputs["approval_request"]["summary"] == "Publish grades"
        assert dict(record.outputs) == {}

    def test_a_deferral_is_distinguished_from_a_refusal(self, audit, store) -> None:
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        gate = ScriptedGate(DEFAULT_GATE, [ApprovalDecision.deferred(reason="no reply")])
        _runtime(audit=audit, store=store, gates=gate).run(plan)
        record = audit.for_step("publish")[-1]
        assert record.outcome == "deferred"
        assert record.decided_by == UNANSWERED

    def test_the_run_stops_at_the_refusal(self, audit, store) -> None:
        calls: list[str] = []
        plan = [
            _step("publish", risky=True, gate=DEFAULT_GATE, record=calls),
            _step("after", record=calls),
        ]
        report = _runtime(
            audit=audit, store=store, gates=DenyAllGate(DEFAULT_GATE)
        ).run(plan)
        assert calls == [], "neither the refused step nor the one after it ran"
        assert report.status == "stopped"

    def test_the_refusal_is_checkpointed_so_a_resumer_knows(self, audit, store) -> None:
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        rt = _runtime(
            audit=audit, store=store, gates=DenyAllGate(DEFAULT_GATE, by="prof")
        )
        rt.run(plan)
        assert store.load(rt.thread_id).approvals["publish"] == "refused"
        assert store.load(rt.thread_id).audit_digest, (
            "the chain position must be durable, or a resume re-appends at a "
            "sequence the log already holds"
        )

    def test_a_refused_step_is_retried_on_resume_not_marked_done(
        self, audit, store
    ) -> None:
        """One 'no' is not 'no forever'; the human may answer differently."""
        plan = [_step("publish", risky=True, gate=DEFAULT_GATE)]
        rt = _runtime(
            audit=audit, store=store, gates=DenyAllGate(DEFAULT_GATE, by="prof")
        )
        rt.run(plan)
        assert store.load(rt.thread_id).completed == ()
        assert store.load(rt.thread_id).cursor == "publish"


class TestStepFailure:
    def test_a_raising_step_is_audited_not_swallowed(self, audit, store) -> None:
        step = _step("bad", raises=RuntimeError("boom"))
        report = _runtime(audit=audit, store=store).run([step])
        assert report.status == "failed"
        record = audit.for_step("bad")[-1]
        assert record.outcome == "failed"
        assert "boom" in record.error

    def test_the_run_stops_after_a_failure(self, audit, store) -> None:
        calls: list[str] = []
        plan = [
            _step("bad", raises=RuntimeError("boom"), record=calls),
            _step("after", record=calls),
        ]
        _runtime(audit=audit, store=store).run(plan)
        assert calls == ["bad"]

    def test_a_failure_checkpoints_the_audit_chain_position(self, audit, store) -> None:
        """Regression for bug 33.

        Without this, a resumed run would try to append at a sequence number the
        log already holds, and the runtime's own integrity check would refuse it
        as tampering -- on a log nobody tampered with.
        """
        step = _step("bad", raises=RuntimeError("boom"))
        rt = _runtime(audit=audit, store=store)
        rt.run([step])
        saved = store.load(rt.thread_id)
        assert saved.audit_digest == audit.last_digest()
        assert saved.audit_seq == len(audit) + 1

    def test_a_failed_step_is_not_marked_done(self, audit, store) -> None:
        rt = _runtime(audit=audit, store=store)
        rt.run([_step("bad", raises=RuntimeError("boom"))])
        assert store.load(rt.thread_id).completed == ()

    def test_a_step_returning_nothing_is_a_failure(self, audit, store) -> None:
        """An action with no recorded output cannot satisfy the criterion."""
        step = Step(
            name="silent",
            intent="i",
            action="a",
            kind="tool",
            run=lambda ctx: {},
        )
        report = _runtime(audit=audit, store=store).run([step])
        assert report.status == "failed"
        assert "no outputs" in audit.for_step("silent")[-1].error

    def test_a_step_may_opt_into_an_empty_output(self, audit, store) -> None:
        step = Step(
            name="noop",
            intent="i",
            action="a",
            kind="tool",
            run=lambda ctx: {},
            allow_empty_output=True,
        )
        report = _runtime(audit=audit, store=store).run([step])
        assert report.ok


class TestResume:
    def test_resume_runs_only_the_unfinished_steps(self, audit, store) -> None:
        calls: list[str] = []
        plan = [_step(n, record=calls) for n in ("a", "b", "c")]
        first = _runtime(audit=audit, store=store, thread_id="t")
        store.save(first.state)
        first.run(plan[:2])
        assert calls == ["a", "b"]

        calls.clear()
        second = _runtime(audit=audit, store=store, thread_id="t")
        second.resume()
        report = second.run(plan)
        assert calls == ["c"], "completed steps must not re-run"
        assert report.skipped == ("a", "b")

    def test_resume_does_not_repeat_the_gated_step(self, audit, store) -> None:
        """The regression test this lab exists for.

        A publish that runs twice is a duplicated side effect, not a slow run.
        """
        calls: list[str] = []
        gate = RecordingGate(PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY))
        plan = [
            _step("publish", risky=True, gate=DEFAULT_GATE, record=calls),
            _step("report", record=calls),
        ]
        first = _runtime(audit=audit, store=store, gates=gate, thread_id="t")
        store.save(first.state)
        first.run(plan[:1])
        assert calls == ["publish"]
        assert gate.steps_asked() == ("publish",)

        calls.clear()
        gate.requests.clear()
        gate.decisions.clear()
        second = _runtime(audit=audit, store=store, gates=gate, thread_id="t")
        second.resume()
        second.run(plan)
        assert calls == ["report"], "the gated publish must not run again"
        assert gate.steps_asked() == (), "the gate must not be asked again"

    def test_resume_carries_the_artifacts_forward(self, audit, store) -> None:
        plan = [_step("a", returns={"x": 1}), _step("b")]
        first = _runtime(audit=audit, store=store, thread_id="t")
        store.save(first.state)
        first.run(plan[:1])

        seen: dict = {}
        second_step = Step(
            name="b",
            intent="i",
            action="a",
            kind="tool",
            run=lambda ctx: (seen.update(ctx.artifacts), {"ok": 1})[1],
        )
        second = _runtime(audit=audit, store=store, thread_id="t")
        second.resume()
        second.run([second_step])
        assert seen["a"] == {"x": 1}

    def test_resume_carries_the_budget_spend_forward(self, audit, store) -> None:
        """Otherwise a resumed run can overspend, because it cannot see the past."""
        plan = [_step("a"), _step("b")]
        first = _runtime(audit=audit, store=store, budget=Budget.only(steps=3),
                         thread_id="t")
        store.save(first.state)
        first.run(plan[:2])
        assert first.ledger.spent()["steps"] == 2

        second = _runtime(audit=audit, store=store, budget=Budget.only(steps=3),
                          thread_id="t")
        second.resume()
        assert second.ledger.spent()["steps"] == 2
        report = second.run([_step("c")])
        assert report.ok
        assert report.status == "completed"

    def test_a_resumed_run_is_stopped_by_the_dead_runs_budget(
        self, audit, store
    ) -> None:
        """The spend is the crashed run's, so the ceiling still binds."""
        plan = [_step("a"), _step("b"), _step("c")]
        first = _runtime(audit=audit, store=store, budget=Budget.only(steps=2),
                         thread_id="t")
        store.save(first.state)
        first.run(plan[:2])

        second = _runtime(audit=audit, store=store, budget=Budget.only(steps=2),
                          thread_id="t")
        second.resume()
        report = second.run(plan)
        assert report.status == "stopped"
        assert report.escalated

    def test_resume_refuses_an_unknown_thread(self, audit, store) -> None:
        rt = _runtime(audit=audit, store=store, thread_id="t")
        with pytest.raises(CheckpointUnavailable):
            rt.resume("no-such-thread")

    def test_resume_refuses_a_checkpoint_for_a_different_course(
        self, audit, store
    ) -> None:
        first = _runtime(audit=audit, store=store, course_code="CS201", thread_id="t")
        store.save(first.state)
        second = _runtime(audit=audit, store=store, course_code="CS999", thread_id="t")
        with pytest.raises(ResumeDivergence) as exc:
            second.resume()
        assert exc.value.first_difference == "course_code"

    def test_resume_refuses_a_checkpoint_for_a_different_plan(
        self, audit, store
    ) -> None:
        first = _runtime(audit=audit, store=store, plan_sha256="aaaa", thread_id="t")
        store.save(first.state)
        second = _runtime(audit=audit, store=store, plan_sha256="bbbb", thread_id="t")
        with pytest.raises(ResumeDivergence) as exc:
            second.resume()
        assert exc.value.first_difference == "plan_sha256"

    def test_resume_refuses_an_audit_log_that_is_not_this_runs(
        self, audit, store
    ) -> None:
        """Regression for bug 34.

        Pointing the runtime at the wrong log used to fail on the first append,
        with a message about tampering. Nothing had been tampered with; the files
        simply did not belong together.
        """
        first = _runtime(audit=audit, store=store, thread_id="t")
        store.save(first.state)
        first.run([_step("a")])

        other_log = InMemoryAuditLog(run_id="other")
        second = Runtime(
            budget=Budget.defaults(),
            audit=other_log,
            checkpoints=store,
            thread_id="t",
        )
        with pytest.raises(ResumeDivergence) as exc:
            second.resume()
        assert exc.value.first_difference == "audit_digest"

    def test_resume_continues_the_same_hash_chain(self, audit, store) -> None:
        first = _runtime(audit=audit, store=store, thread_id="t")
        store.save(first.state)
        first.run([_step("a")])
        mid_digest = audit.last_digest()

        second = _runtime(audit=audit, store=store, thread_id="t")
        second.resume()
        second.run([_step("b")])
        assert audit.records()[1].prev_digest == mid_digest
        assert audit.verify() == 2, "one chain, not two halves in one log"

    def test_the_cursor_names_the_next_step_not_a_blank(self, audit, store) -> None:
        """A blank cursor is indistinguishable from 'the run finished'.

        Observed from *inside* the run: when step ``b`` is called, the state it was
        handed already says ``b``. Checking the cursor only after the plan ends
        would prove nothing, because a finished plan correctly leaves it blank.
        """
        seen: list[str] = []
        inner = _step("b").run

        def watching(ctx: StepContext):
            seen.append(ctx.state.cursor if ctx.state else "")
            return inner(ctx)

        watched = Step(**{**_step("b").__dict__, "run": watching})
        rt = _runtime(audit=audit, store=store, thread_id="t")
        store.save(rt.state)
        rt.run([_step("a"), watched])
        assert seen == ["b"], "step b should have been told it is next"
        assert store.load("t").cursor == "", "and blank once the plan is finished"

    def test_resume_reports_the_checkpoint_it_started_from(
        self, audit, store
    ) -> None:
        first = _runtime(audit=audit, store=store, thread_id="t")
        store.save(first.state)
        first.run([_step("a"), _step("b")])
        second = _runtime(audit=audit, store=store, thread_id="t")
        assert second.resumed_from() is None
        second.resume()
        assert second.resumed_from() is not None
        # The last checkpoint of a finished plan is blank-cursor; what matters is
        # that it is reported rather than silently absent.
        assert second.resumed_from().cursor == ""
        assert second.resumed_from().step == 2


class TestPlanDigest:
    def test_the_digest_is_stable_across_rebuilds(self) -> None:
        """A resuming process rebuilds its steps from source, as new objects."""

        def fresh():
            return tuple(
                Step(
                    name=n,
                    intent=f"i{n}",
                    action=f"a{n}",
                    kind="tool",
                    run=lambda ctx: {"x": 1},
                )
                for n in ("a", "b")
            )

        assert plan_digest(fresh()) == plan_digest(fresh())

    def test_a_changed_shape_changes_the_digest(self) -> None:
        base = [Step(name="a", intent="i", action="x", kind="tool",
                     run=lambda c: {"x": 1})]
        riskier = [Step(name="a", intent="i", action="x", kind="tool",
                        run=lambda c: {"x": 1}, risky=True, gate="publish")]
        assert plan_digest(base) != plan_digest(riskier)


class TestReport:
    def test_a_partial_report_is_available_before_the_run_ends(
        self, audit, store
    ) -> None:
        rt = _runtime(audit=audit, store=store)
        rt.run([_step("a")])
        report = rt.report()
        assert report.status == "partial"
        assert report.steps_run() == ("a",)

    def test_outcome_for_returns_none_for_a_step_that_never_ran(
        self, audit, store
    ) -> None:
        rt = _runtime(audit=audit, store=store)
        report = rt.run([_step("a")])
        assert report.outcome_for("a") is not None
        assert report.outcome_for("never") is None
        assert report.outputs_of("never") == {}

    def test_the_gate_names_are_reported(self, audit, store) -> None:
        """A run that gates nothing should say so, not claim a gate it does not have."""
        ungated = _runtime(audit=audit, store=store)
        assert ungated.gates == ()
        with pytest.raises(KeyError):
            ungated.gate(DEFAULT_GATE)

        gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)
        rt = _runtime(audit=audit, store=store, gates=gate)
        assert rt.gates == (DEFAULT_GATE,)
        assert rt.gate(DEFAULT_GATE).name == DEFAULT_GATE


# --------------------------------------------------------------------------
# the lab module
# --------------------------------------------------------------------------


class TestLabModule:
    def test_the_document_constants_are_verbatim(self) -> None:
        assert lab06.LAB == "Lab 6"
        assert lab06.DOC_GOAL == (
            "Run the agent inside a controlled runtime with a full audit log."
        )
        assert "budgets, gates, checkpoint/resume, audit log" in lab06.DOC_DELIVERABLE
        assert "logged with inputs/outputs" in lab06.DOC_ACCEPTANCE
        assert "crashed run resumes from its last checkpoint" in lab06.DOC_ACCEPTANCE
        assert "checkpoint before risky steps" in lab06.DOC_PITFALL
        assert lab06.DOC_NODES == "Coordinator runtime around all nodes."

    def test_the_pseudocode_is_the_documents_five_lines(self) -> None:
        lines = lab06.DOC_PSEUDOCODE.splitlines()
        assert len(lines) == 5
        assert lines[0] == "run = Runtime(budget=B, gates=[publish])"
        assert "audit.log(step.intent, step.action, step.result)" in lines[2]
        assert "if step.risky: wait_for_approval(step)" in lines[3]
        assert "checkpoint(run.state)" in lines[4]

    def test_the_runtime_order_applies_both_pitfall_corrections(self) -> None:
        joined = " ".join(lab06.RUNTIME_ORDER)
        assert joined.index("checkpoint(run.state)") < joined.index(
            "wait_for_approval(step)"
        ), "the pitfall requires checkpointing before the gate"
        assert "inputs, outputs" in joined

    def test_the_demo_plan_is_the_documented_shape(self) -> None:
        plan = lab06.build_demo_plan()
        assert tuple(s.name for s in plan) == lab06.DEMO_STEPS

    def test_only_the_publish_step_is_risky(self) -> None:
        plan = lab06.build_demo_plan()
        risky = [s.name for s in plan if s.risky]
        assert risky == ["publish"]
        assert all(s.gate == DEFAULT_GATE for s in plan if s.risky)

    def test_the_plan_spans_the_three_families_the_document_names(self) -> None:
        """Lab 6's inputs: skills (Lab 3), connector (Lab 5), memory (Lab 4)."""
        kinds = {s.kind for s in lab06.build_demo_plan()}
        assert {"skill", "connector", "tool"} <= kinds

    def test_the_plan_digest_is_stable(self) -> None:
        assert plan_digest(lab06.build_demo_plan()) == plan_digest(
            lab06.build_demo_plan()
        )

    def test_the_crashing_and_resuming_plans_share_a_digest(self) -> None:
        """Regression for the reason the crash proof works at all.

        ``crash_at`` changes what a step *body* does; it does not change the
        plan's shape. So the plan built to crash and the plan built to resume
        must digest identically -- if they did not, ``resume()`` would refuse
        the second run as ``plan_sha256`` divergence and the proof would be
        demonstrating a refusal rather than a resume.

        This is also why the digest covers names, order, gates and risk, and
        deliberately does not cover the callables.
        """
        clean = lab06.build_demo_plan()
        crashing = lab06.build_demo_plan(crash_at=lab06.CRASH_STEP)
        assert plan_digest(clean) == plan_digest(crashing)
        assert [s.name for s in clean] == [s.name for s in crashing]

    def test_the_crash_flag_reaches_the_step_it_names(self, audit, store) -> None:
        """...but the flag is not inert: it does change behaviour.

        ``report`` is last on purpose, so the crash lands *after* the risky
        publish. Any earlier placement would let a resume prove nothing about
        not repeating the publish.
        """
        plan = lab06.build_demo_plan(crash_at=lab06.CRASH_STEP)
        names = tuple(s.name for s in plan)
        assert names == lab06.DEMO_STEPS
        assert names[-1] == lab06.CRASH_STEP
        assert names.index(lab06.CRASH_STEP) > names.index("publish")

    def test_the_gated_step_reads_an_earlier_steps_output(self, audit, store) -> None:
        """Otherwise the step is order-independent and the resume is trivial."""
        plan = lab06.build_demo_plan()
        gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)
        rt = Runtime(
            budget=Budget.defaults(),
            gates={DEFAULT_GATE: gate},
            audit=audit,
            checkpoints=store,
        )
        report = rt.run(plan)
        assert report.outputs_of("publish")["drafts_in"] == 3


class TestLabAcceptance:
    """The acceptance criteria, executed."""

    def test_every_acceptance_check_passes(self, acceptance) -> None:
        result = acceptance
        failed = [c["name"] for c in result.checks if not c["passed"]]
        assert not failed, f"failing acceptance checks: {failed}"

    def test_the_run_covers_the_whole_lab(self, acceptance) -> None:
        result = acceptance
        assert result.lab == "Lab 6"
        assert result.pass_all
        assert len(result.checks) >= 16
        assert result.clean is not None and result.clean.ok
        assert result.gate is not None and result.gate.status == "stopped"
        assert result.budget is not None and result.budget.status == "stopped"

    def test_the_clean_run_reaches_its_last_step(self, acceptance) -> None:
        assert acceptance.clean.steps_run() == lab06.DEMO_STEPS

    def test_the_gate_run_did_not_publish(self, acceptance) -> None:
        assert "publish" not in acceptance.gate.steps_run()
        assert acceptance.gate.outputs_of("publish") == {}

    def test_the_budget_run_escalated(self, acceptance) -> None:
        """Doc 2.12: on exhaustion, escalate to course faculty with a diagnosis."""
        assert acceptance.budget.escalated

    def test_the_proof_says_the_publish_did_not_repeat(self, acceptance) -> None:
        proof = acceptance.proof
        assert proof.crashed
        assert proof.resumed_steps == (lab06.CRASH_STEP,)
        assert not proof.publish_reran
        assert not proof.gate_asked_again
        assert proof.pass_resume

    def test_the_proof_audited_every_action_with_both_halves(self, acceptance) -> None:
        proof = acceptance.proof
        assert proof.audit_records == len(lab06.DEMO_STEPS)
        assert proof.empty_ok_records == 0
        assert proof.audit_verified

    def test_as_dict_is_json_safe(self, acceptance) -> None:
        import json

        blob = json.dumps(acceptance.as_dict())
        assert json.loads(blob)["lab"] == "Lab 6"

    def test_every_check_has_a_name_and_a_readable_detail(self, acceptance) -> None:
        for check in acceptance.checks:
            assert check["name"] and check["detail"]
            assert isinstance(check["passed"], bool)


class TestCrashProof:
    """The subprocess half. Slow on purpose -- see this module's docstring.

    One proof is shared by every assertion here: it forks two real processes, so
    running it per-assertion would pay that cost five times over to re-read
    facts that cannot change between reads.
    """

    def test_the_crash_proof_survives_process_death(self, crash_proof) -> None:
        proof = crash_proof
        assert proof.crash_exit_code == 9, (
            f"the child should have died with os._exit(9); stderr was not empty "
            f"and the run did not reach the crashing step"
        )
        assert proof.crashed
        assert proof.resumed_steps == (lab06.CRASH_STEP,)
        assert proof.skipped == tuple(
            n for n in lab06.DEMO_STEPS if n != lab06.CRASH_STEP
        )

    def test_the_resumed_process_did_not_repeat_the_publish(
        self, crash_proof
    ) -> None:
        proof = crash_proof
        assert not proof.publish_reran
        assert not proof.gate_asked_again

    def test_one_audit_chain_spans_both_processes(self, crash_proof) -> None:
        """Two processes, one log, one unbroken hash chain."""
        proof = crash_proof
        assert proof.audit_records == len(lab06.DEMO_STEPS)
        assert proof.audit_verified
        assert proof.fingerprint

    def test_the_checkpoint_and_log_are_left_on_disk(self, crash_proof_root) -> None:
        root = crash_proof_root
        assert list(root.glob("*.sqlite")), "no checkpoint database was written"
        assert list(root.glob("*.jsonl")), "no audit log was written"

    def test_the_child_program_is_readable_on_disk(self, crash_proof_root) -> None:
        """The exact program that dies should be inspectable, not inlined."""
        child = crash_proof_root / "_lab06_crash_child.py"
        assert child.exists()
        text = child.read_text(encoding="utf-8")
        assert 'crash_at="report"' in text
        assert "CHILD=crash-returned-unexpectedly" in text, (
            "the child must be able to say the crash did not happen"
        )
        # `os._exit` appears only inside comments that explain the crash, never as
        # a call: the child asks build_demo_plan to crash, so both processes run
        # one code path rather than two. Strip comments before looking.
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        assert "os._exit" not in code, (
            "the child must reach the crash through build_demo_plan, so that the "
            "crashing run and the resuming run execute identical code"
        )
        assert "os._exit(9)" in text, "the comment should still name the exit"


class TestCli:
    """One subprocess run of the lab module, three things asserted about it."""

    def test_the_module_exits_zero_when_everything_passes(self, cli) -> None:
        assert cli.returncode == 0, cli.stdout + cli.stderr
        assert "OVERALL: PASS" in cli.stdout

    def test_the_cli_reports_the_document_goal(self, cli) -> None:
        assert lab06.DOC_GOAL in cli.stdout
        assert lab06.DOC_ACCEPTANCE in cli.stdout
        assert lab06.DOC_PITFALL in cli.stdout

    def test_the_output_is_ascii_only(self, cli) -> None:
        """Windows consoles mangle non-ASCII; a report must survive being read."""
        assert cli.stdout.isascii(), (
            "non-ascii in the report:\n"
            + "\n".join(
                line for line in cli.stdout.splitlines() if not line.isascii()
            )
        )


class TestIdempotence:
    """Re-running must not be confused by the evidence of a previous run.

    These two do run twice on purpose: the property under test *is* the second
    run, so there is nothing to share.
    """

    def test_two_runs_in_one_directory_both_pass(self, two_lab_runs) -> None:
        first, second = two_lab_runs
        assert first.pass_all and second.pass_all

    def test_the_generated_threads_do_not_collide(self, two_lab_runs) -> None:
        """Each demo must get its own resume key, or one run's state is another's."""
        first, second = two_lab_runs
        root = Path(first.root)
        names = {p.stem for p in root.glob("*.sqlite")}
        assert len(names) >= 4, f"expected several distinct checkpoint files, got {names}"
        assert Path(first.root) != Path(second.root), "the two runs shared a directory"


class TestSameDirectoryRerun:
    """Regression for the collision a second run into one directory used to cause.

    Found by executing the notebook: ``run_lab06`` twice into the same base
    directory raised ``AuditIntegrityError`` on the second run's first append --
    a fresh ``RunState`` claiming sequence 0 and the genesis digest, appended to a
    log that already held five records.

    The integrity check was right and the wiring was wrong, which is the worst
    order to find out in: the error named the audit log, which is the one thing
    in the system that had done nothing wrong.
    """

    def test_each_run_gets_its_own_evidence_directory(self, tmp_path: Path) -> None:
        first = lab06.run_lab06(tmp_path)
        second = lab06.run_lab06(tmp_path)
        assert first.pass_all and second.pass_all
        assert first.root != second.root
        assert Path(first.root).is_dir() and Path(second.root).is_dir()
        assert Path(first.root).parent == Path(second.root).parent == tmp_path

    def test_the_two_runs_logs_do_not_see_each_other(
        self, tmp_path: Path
    ) -> None:
        """Two invocations into one base directory must not merge their logs.

        The property is that each run owns its own file and its own chain, not
        that the two chains differ. ``fingerprint()`` is documented as "two logs
        with the same fingerprint contain the same chain of content", and the
        clean run's content is identical every time apart from the measured
        ``duration_ms`` -- so two runs that happened to measure the same
        microseconds have the *same* fingerprint, correctly. Asserting they
        differ was a timing claim dressed as a structural one, and it failed only
        under load, when two consecutive runs landed on the same durations.
        """
        first = lab06.run_lab06(tmp_path)
        second = lab06.run_lab06(tmp_path)
        a = JsonlAuditLog(Path(first.root) / "clean.jsonl", run_id="a")
        b = JsonlAuditLog(Path(second.root) / "clean.jsonl", run_id="b")

        # Separate files, each with one whole chain -- not 10 records in one.
        assert Path(first.root) != Path(second.root)
        assert (Path(first.root) / "clean.jsonl") != (Path(second.root) / "clean.jsonl")
        assert len(a.records()) == len(b.records()) == len(lab06.DEMO_STEPS)
        assert a.verify() and b.verify()
        assert [r.seq for r in a.records()] == [r.seq for r in b.records()] == [1, 2, 3, 4, 5]

        # A fingerprint is a function of content, so equal chains must agree and
        # that is the contract. The merge this bug log guards against would show
        # up as one log holding the other's records, which the counts rule out.
        assert (a.fingerprint() == b.fingerprint()) == (
            [r.digest for r in a.records()] == [r.digest for r in b.records()]
        )

    def test_a_fresh_run_on_an_occupied_thread_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The runtime-level guard, independent of ``run_lab06``'s directories."""
        gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)
        lab06.governed_run(
            tmp_path, thread="t", run_id="first", budget=Budget.defaults(), gate=gate
        )
        with pytest.raises(RuntimeBoundaryError, match="already holds a checkpoint"):
            lab06.governed_run(
                tmp_path, thread="t", run_id="second",
                budget=Budget.defaults(), gate=gate,
            )

    def test_the_guard_points_at_both_ways_out(self, tmp_path: Path) -> None:
        gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)
        lab06.governed_run(
            tmp_path, thread="t", run_id="first", budget=Budget.defaults(), gate=gate
        )
        with pytest.raises(RuntimeBoundaryError) as exc:
            lab06.governed_run(
                tmp_path, thread="t", run_id="second",
                budget=Budget.defaults(), gate=gate,
            )
        message = str(exc.value)
        assert "resume=True" in message
        assert "different thread" in message

    def test_the_refusal_leaves_the_first_runs_log_untouched(
        self, tmp_path: Path
    ) -> None:
        gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY)
        lab06.governed_run(
            tmp_path, thread="t", run_id="first", budget=Budget.defaults(), gate=gate
        )
        log = JsonlAuditLog(tmp_path / "t.jsonl", run_id="first")
        before = log.fingerprint()
        with pytest.raises(RuntimeBoundaryError):
            lab06.governed_run(
                tmp_path, thread="t", run_id="second",
                budget=Budget.defaults(), gate=gate,
            )
        assert log.fingerprint() == before
        assert log.verify() == len(lab06.DEMO_STEPS)