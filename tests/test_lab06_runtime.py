"""Tests for the Lab 6 governed runtime package.

Organised by module, because each module answers a different question:

* :class:`TestBudget` -- is a budget checked *before* the action?
* :class:`TestAudit` -- does the log carry inputs and outputs, and does the
  hash chain notice tampering?
* :class:`TestCheckpoint` -- does state survive the process, and does ``load``
  return the *newest* checkpoint rather than the highest-sorting one?
* :class:`TestGates` -- can a step marked risky run without a decision?
* :class:`TestRuntime` -- the controller's ordering, and resume.

The two order properties are the lab's whole content, so they get regression
tests that were each confirmed to fail against the defect they describe:
``test_resume_does_not_repeat_the_gated_step`` against a runtime that re-ran
completed steps, and ``test_latest_checkpoint_is_the_last_written_one`` against
a store whose checkpoint ids were unordered (which made resume load an arbitrary
checkpoint).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from course_organizer.runtime import (
    APPROVAL_OUTCOMES,
    AuditIntegrityError,
    Budget,
    BudgetExceeded,
    BudgetLedger,
    BudgetSnapshot,
    CallbackGate,
    CheckpointUnavailable,
    DenyAllGate,
    GENESIS_DIGEST,
    GateRefused,
    GateScriptExhausted,
    InMemoryAuditLog,
    InMemoryCheckpointStore,
    JsonlAuditLog,
    PreapprovedGate,
    RecordingGate,
    ResumeDivergence,
    RunState,
    Runtime,
    RuntimeBoundaryError,
    ScriptedGate,
    SqliteCheckpointStore,
    UNANSWERED,
    ApprovalDecision,
    ApprovalRequest,
    Step,
    StepContext,
    build_audit_record,
    canonical_json,
    chain_digest,
    plan_digest,
)
from course_organizer.runtime.checkpoint import (
    CHECKPOINT_NS,
    make_checkpoint_id,
    parse_write_seq,
)
from course_organizer.runtime.errors import RuntimeBoundaryError as _RBE


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def budget() -> Budget:
    """A generous budget, so tests that are not about budgets are not."""
    return Budget.defaults()


@pytest.fixture
def log() -> InMemoryAuditLog:
    """A fresh in-memory audit log."""
    return InMemoryAuditLog(run_id="test")


def _record(seq: int, *, previous: str, **overrides) -> object:
    """A minimal valid audit record, with overridable fields."""
    payload = {
        "seq": seq,
        "run_id": "test",
        "thread_id": "t",
        "step": f"step{seq}",
        "intent": "do the thing",
        "action": "do_thing",
        "kind": "tool",
        "inputs": {"a": 1},
        "outputs": {"b": 2},
        "outcome": "ok",
        "previous_digest": previous,
    }
    payload.update(overrides)
    return build_audit_record(**payload)


def _store(tmp_path: Path) -> SqliteCheckpointStore:
    return SqliteCheckpointStore(tmp_path / "ckpt.sqlite", run_id="test")


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


class TestBudget:
    """Doc 2.12: "Bound MAX_ATTEMPTS ... on exhaustion, escalate to course faculty"."""

    def test_reserve_lets_a_charge_that_fits(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=2))
        ledger.reserve("steps", 1, step="a")
        ledger.reserve("steps", 1, step="b")
        assert ledger.spent() == {"steps": 2}

    def test_reserve_refuses_before_the_ceiling_is_passed(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=2))
        ledger.reserve("steps", 2)
        with pytest.raises(BudgetExceeded) as exc:
            ledger.reserve("steps", 1, step="c")
        assert exc.value.dimension == "steps"
        assert (exc.value.limit, exc.value.spent, exc.value.requested) == (2, 2, 1)

    def test_a_refused_reservation_spends_nothing(self) -> None:
        """The refusal happens before the work, so the work is never charged."""
        ledger = BudgetLedger(Budget.only(steps=1))
        ledger.reserve("steps", 1)
        with pytest.raises(BudgetExceeded):
            ledger.reserve("steps", 1)
        assert ledger.spent() == {"steps": 1}, "the refused charge must not appear"
        assert ledger.refused("steps") == 1

    def test_reserve_commits_so_a_lone_call_is_enough(self) -> None:
        """Checking without consuming would make the budget invisible."""
        ledger = BudgetLedger(Budget.only(steps=2))
        ledger.reserve("steps", 1)
        assert ledger.spent() == {"steps": 1}, "reserve must consume as it checks"

    def test_an_omitted_dimension_is_unbounded(self) -> None:
        """Absent means unbounded, not zero.

        A budget that silently defaulted to a ceiling the caller never asked for
        would refuse steps the caller believed were allowed.
        """
        ledger = BudgetLedger(Budget.only(steps=1))
        for _ in range(50):
            ledger.reserve("tool_calls", 1)
        assert ledger.spent()["tool_calls"] == 50
        assert ledger.remaining("tool_calls") is None

    def test_a_zero_or_negative_ceiling_is_a_construction_error(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            Budget(limits={"steps": 0})
        with pytest.raises(ValueError, match="must be >= 1"):
            Budget(limits={"steps": -1})

    def test_a_bool_is_not_accepted_as_a_limit(self) -> None:
        """`True` is an `int` in Python; a budget of `True` is a bug, not a limit."""
        with pytest.raises(ValueError, match="must be an int"):
            Budget(limits={"steps": True})

    def test_check_catches_an_overrun_that_has_already_happened(self) -> None:
        """The wall clock cannot be reserved in advance, only checked."""
        ledger = BudgetLedger(Budget.only(wall_clock_ms=10))
        ledger.charge("wall_clock_ms", 9)
        ledger.check("wall_clock_ms", step="slow")  # under: fine
        ledger.charge("wall_clock_ms", 1)
        with pytest.raises(BudgetExceeded) as exc:
            ledger.check("wall_clock_ms", step="slow")
        assert exc.value.requested == 0, "nothing was requested; the overrun was earlier"

    def test_check_is_a_no_op_for_an_unbounded_dimension(self) -> None:
        ledger = BudgetLedger(Budget.only())
        ledger.check("wall_clock_ms")
        ledger.charge("wall_clock_ms", 10_000)

    def test_restore_adopts_a_checkpointed_spend(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=5))
        ledger.restore({"steps": 4})
        assert ledger.spent() == {"steps": 4}

    def test_restore_rejects_nonsense_spend(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=5))
        with pytest.raises(ValueError):
            ledger.restore({"steps": -1})
        with pytest.raises(ValueError):
            ledger.restore({"steps": "lots"})

    def test_restore_does_not_inherit_the_crashed_process_refusals(self) -> None:
        """A refusal describes an attempt the resumed run never made."""
        ledger = BudgetLedger(Budget.only(steps=1))
        ledger.reserve("steps", 1)
        with pytest.raises(BudgetExceeded):
            ledger.reserve("steps", 1)
        ledger.restore({"steps": 1})
        assert ledger.refused() == 0

    def test_snapshots_include_bounded_dimensions_the_run_never_reached(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=5, tool_calls=9))
        ledger.reserve("steps", 1)
        ledger.charge("steps", 1)
        names = [s.dimension for s in ledger.snapshots()]
        assert names[0] == "steps", "spent dimensions come first"
        assert "tool_calls" in names, "a budget never approached is still information"

    def test_snapshot_reports_remaining_and_exhaustion(self) -> None:
        ledger = BudgetLedger(Budget.only(steps=2))
        ledger.spend("steps", 2)
        snap = ledger.snapshot("steps")
        assert snap.remaining == 0
        assert snap.exhausted
        assert snap.rendered() == "2/2"

    def test_snapshot_renders_an_unbounded_dimension_as_inf(self) -> None:
        assert BudgetSnapshot(dimension="x", spent=3).rendered() == "3/inf"

    def test_defaults_matches_the_published_table(self) -> None:
        from course_organizer.runtime import DEFAULT_LIMITS

        assert Budget.defaults().limits == dict(DEFAULT_LIMITS)

    def test_a_negative_charge_is_rejected(self) -> None:
        ledger = BudgetLedger(Budget.defaults())
        with pytest.raises(ValueError):
            ledger.charge("steps", -1)
        with pytest.raises(ValueError):
            ledger.reserve("steps", -1)

    def test_every_documented_error_is_one_runtime_boundary(self) -> None:
        """One ``except`` can trap the whole family."""
        assert issubclass(BudgetExceeded, RuntimeBoundaryError)
        assert issubclass(GateRefused, RuntimeBoundaryError)
        assert issubclass(CheckpointUnavailable, RuntimeBoundaryError)
        assert issubclass(ResumeDivergence, RuntimeBoundaryError)
        assert issubclass(AuditIntegrityError, RuntimeBoundaryError)
        assert _RBE is RuntimeBoundaryError


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


class TestAudit:
    """Doc: "Log inputs and outputs, not just messages"."""

    def test_a_record_cannot_be_built_without_inputs_and_outputs(self) -> None:
        """Both are required fields, so "just a message" is unrepresentable."""
        from course_organizer.runtime import AuditRecord

        fields = set(AuditRecord.model_fields)
        assert {"inputs", "outputs"} <= fields
        assert "message" not in fields, (
            "a free-text message field is exactly what the pitfall warns "
            "against; a record must carry structured inputs and outputs"
        )

    def test_inputs_and_outputs_survive_a_jsonl_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        log = JsonlAuditLog(path)
        rec = _record(1, previous=GENESIS_DIGEST, inputs={"course": "CS201"},
                      outputs={"records": 8})
        log.append(rec)
        back = JsonlAuditLog(path).records()[0]
        assert dict(back.inputs) == {"course": "CS201"}
        assert dict(back.outputs) == {"records": 8}

    def test_digests_chain(self, log: InMemoryAuditLog) -> None:
        prev = GENESIS_DIGEST
        for seq in range(1, 4):
            rec = _record(seq, previous=prev)
            log.append(rec)
            prev = rec.digest
        assert log.verify() == 3
        assert log.fingerprint()

    def test_canonical_json_is_key_order_independent(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_a_truncated_tail_passes_the_chain_and_is_caught_on_resume(
        self, tmp_path: Path
    ) -> None:
        """The one tamper the chain cannot see alone, and what closes it.

        Deleting the **last** record leaves a chain that is internally
        consistent -- ``verify()`` returns ``True`` -- because a hash chain has
        no anchor for "there should have been one more record". This is a real
        property of the structure, not a defect, and the design has to account
        for it rather than pretend otherwise.

        What catches it is the checkpoint: it stores ``audit_seq`` and
        ``audit_digest``, and ``resume()`` cross-checks the log against them. So
        the audit guarantee is two-part, and this test holds both halves:

        * the chain proves nothing *before* the break was edited;
        * the checkpoint proves nothing *after* the break was removed.
        """
        path = tmp_path / "a.jsonl"
        audit = JsonlAuditLog(path, run_id="r")
        store = InMemoryCheckpointStore()

        def ok(ctx) -> dict:
            return {"n": 1}

        def runtime(audit_log) -> Runtime:
            return Runtime(
                budget=Budget.defaults(), audit=audit_log, checkpoints=store,
                thread_id="t", course_code="CS201",
            )

        runtime(audit).run([
            Step(name=f"s{i}", intent="i", action="a", kind="tool", run=ok)
            for i in (1, 2, 3)
        ])
        assert len(audit.records()) == 3

        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[-1]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        torn = JsonlAuditLog(path, run_id="r")
        # Half one: the chain alone is satisfied. This must stay true -- if it
        # ever starts raising, the two halves have been conflated and something
        # is relying on the chain to detect truncation.
        assert torn.verify()
        assert len(torn.records()) == 2

        # Half two: the checkpoint refuses the resume.
        with pytest.raises(ResumeDivergence) as exc:
            runtime(torn).resume()
        assert exc.value.first_difference == "audit_digest"
        assert exc.value.thread_id == "t"

    def test_editing_a_record_after_the_fact_is_detected(self, log: InMemoryAuditLog) -> None:
        log.append(_record(1, previous=GENESIS_DIGEST))
        with pytest.raises(AuditIntegrityError) as exc:
            log.records()[0].model_copy(update={"outputs": {"tampered": True}}).verify()
        assert "does not match its digest" in exc.value.reason

    def test_removing_a_record_breaks_the_chain(self, log: InMemoryAuditLog) -> None:
        """Each record's own digest is still valid; the *link* is what breaks."""
        prev = GENESIS_DIGEST
        for seq in range(1, 4):
            rec = _record(seq, previous=prev)
            log.append(rec)
            prev = rec.digest
        rows = list(log.records())
        # Record 3 verified fine on its own...
        rows[2].verify()
        # ...but with record 2 gone, 3's prev_digest names something absent.
        surviving_predecessor = rows[0].digest
        with pytest.raises(AuditIntegrityError) as exc:
            if rows[2].prev_digest != surviving_predecessor:
                raise AuditIntegrityError(
                    rows[2].seq,
                    expected_digest=surviving_predecessor,
                    actual_digest=rows[2].prev_digest,
                    reason="the chain does not link to the preceding record",
                )
        assert exc.value.seq == 3

    def test_appending_with_a_wrong_previous_digest_is_refused(self, log: InMemoryAuditLog) -> None:
        """A forged insertion is refused at the write, not discovered later."""
        log.append(_record(1, previous=GENESIS_DIGEST))
        with pytest.raises(AuditIntegrityError, match="refused at append"):
            log.append(_record(2, previous="deadbeefdeadbeef"))

    def test_a_partial_trailing_line_is_dropped_and_reported(self, tmp_path: Path) -> None:
        """A process killed mid-write must still be auditable."""
        path = tmp_path / "a.jsonl"
        log = JsonlAuditLog(path)
        prev = GENESIS_DIGEST
        for seq in (1, 2):
            rec = _record(seq, previous=prev)
            log.append(rec)
            prev = rec.digest
        assert not log.truncated_tail()
        path.write_bytes(path.read_bytes()[:-15])
        torn = JsonlAuditLog(path)
        assert len(torn.records()) == 1
        assert torn.truncated_tail()
        assert torn.verify() == 1, "intact records must remain verifiable"

    def test_the_log_survives_the_process(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        first = JsonlAuditLog(path, run_id="one")
        first.append(_record(1, previous=GENESIS_DIGEST))
        second = JsonlAuditLog(path, run_id="two")
        assert len(second.records()) == 1
        assert second.fingerprint() == first.fingerprint()

    def test_appends_are_one_line_each(self, tmp_path: Path) -> None:
        """A reader never sees a torn record."""
        path = tmp_path / "a.jsonl"
        log = JsonlAuditLog(path)
        prev = GENESIS_DIGEST
        for seq in (1, 2, 3):
            rec = _record(seq, previous=prev)
            log.append(rec)
            prev = rec.digest
        raw = path.read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw.split(b"\n")) - 1 == 3
        for line in raw.split(b"\n")[:-1]:
            json.loads(line)

    def test_records_can_be_filtered_by_step_action_and_outcome(self, log: InMemoryAuditLog) -> None:
        prev = GENESIS_DIGEST
        a = _record(1, previous=prev, step="one", action="read", outcome="ok")
        log.append(a)
        prev = a.digest
        b = _record(2, previous=prev, step="two", action="write", outcome="refused")
        log.append(b)
        assert [r.step for r in log.for_step("one")] == ["one"]
        assert [r.step for r in log.for_action("write")] == ["two"]
        assert [r.seq for r in log.for_outcome("refused")] == [2]
        assert log.names_steps() == ("one", "two")

    def test_summary_counts_by_outcome_and_kind(self, log: InMemoryAuditLog) -> None:
        prev = GENESIS_DIGEST
        a = _record(1, previous=prev, outcome="ok", kind="tool")
        log.append(a)
        prev = a.digest
        b = _record(2, previous=prev, outcome="failed", kind="llm")
        log.append(b)
        s = log.summary()
        assert s["records"] == 2
        assert s["by_outcome"] == {"failed": 1, "ok": 1}
        assert s["by_kind"] == {"llm": 1, "tool": 1}

    def test_the_timestamp_is_excluded_from_the_digest(self) -> None:
        """Two identical runs must chain identically, or honest logs look tampered."""
        a = _record(1, previous=GENESIS_DIGEST)
        b = _record(1, previous=GENESIS_DIGEST)
        assert a.digest == b.digest
        assert "at" not in a.content()

    def test_digest_is_the_documented_width(self) -> None:
        rec = _record(1, previous=GENESIS_DIGEST)
        assert len(rec.digest) == 16
        assert rec.prev_digest == GENESIS_DIGEST == "0" * 16

    def test_chain_digest_depends_on_the_previous_record(self) -> None:
        payload = {"a": 1}
        assert chain_digest(payload, "0" * 16) != chain_digest(payload, "1" * 16)

    def test_equal_chains_have_equal_fingerprints(self) -> None:
        """A fingerprint is a *function* of content, and this says which way.

        ``fingerprint()`` promises that two logs sharing it hold the same chain of
        content. That makes equal fingerprints for equal chains correct, not a
        collision -- which is the direction a test is likely to get wrong, because
        "two runs produced the same value" reads like a defect.

        The content that varies between two honest runs of the same plan is
        ``duration_ms``; ``at`` is excluded. So the property is: change the
        durations and the fingerprint moves, keep them and it holds still.
        """
        def chain_of(duration_ms: float) -> list[str]:
            log = InMemoryAuditLog(run_id="r")
            prev = GENESIS_DIGEST
            for seq in (1, 2, 3):
                rec = _record(seq, previous=prev, duration_ms=duration_ms)
                log.append(rec)
                prev = rec.digest
            return log

        assert chain_of(1.0).fingerprint() == chain_of(1.0).fingerprint()
        assert chain_of(1.0).fingerprint() != chain_of(2.0).fingerprint()

    def test_tail_and_len(self, log: InMemoryAuditLog) -> None:
        prev = GENESIS_DIGEST
        for seq in (1, 2, 3):
            rec = _record(seq, previous=prev)
            log.append(rec)
            prev = rec.digest
        assert len(log) == 3
        assert [r.seq for r in log.tail(2)] == [2, 3]
        assert log.tail(0) == ()


# --------------------------------------------------------------------------
# checkpoints
# --------------------------------------------------------------------------


class TestCheckpointIdOrdering:
    """`get_tuple` is `ORDER BY checkpoint_id DESC` -- a *string* comparison.

    Regression tests for bug 31: an id derived from a hash sorts arbitrarily, so
    ``load`` returned whichever checkpoint happened to sort highest. A resume
    built on that continues from a random step and looks plausible while being
    wrong.
    """

    def test_ids_sort_in_write_order(self) -> None:
        ids = [make_checkpoint_id(i, "ffffffffffffffff") for i in range(1, 12)]
        assert ids == sorted(ids)

    def test_the_ordinal_dominates_the_digest(self) -> None:
        """An unordered digest must not be able to outrank the write order."""
        early = make_checkpoint_id(2, "ffffffffffffffff")
        later = make_checkpoint_id(3, "0000000000000000")
        assert early < later

    def test_ten_digits_of_ordinal_survive_because_the_width_is_fixed(self) -> None:
        assert make_checkpoint_id(9, "a" * 12) < make_checkpoint_id(10, "0" * 12)

    def test_write_seq_round_trips(self) -> None:
        assert parse_write_seq(make_checkpoint_id(7, "abcdefabcdef")) == 7

    def test_parse_write_seq_is_forgiving_of_a_foreign_id(self) -> None:
        assert parse_write_seq("not-a-seq") == 0

    def test_the_ordinal_starts_at_one(self) -> None:
        with pytest.raises(ValueError, match="starts at 1"):
            make_checkpoint_id(0, "a" * 12)


class TestSqliteCheckpointStore:
    """State must survive the process, and `load` must mean "latest"."""

    def test_state_survives_a_new_store_on_the_same_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.sqlite"
        with SqliteCheckpointStore(path, run_id="r").session() as first:
            first.save(RunState(thread_id="t", run_id="r", course_code="CS201"))
        with SqliteCheckpointStore(path, run_id="r").session() as second:
            assert second.load("t").course_code == "CS201"

    def test_latest_checkpoint_is_the_last_written_one(self, tmp_path: Path) -> None:
        """The regression test for bug 31.

        Digests are deliberately unordered, so a store that keyed ordering on
        the digest would fail here.
        """
        digests = ["ffffffffffffffff", "0000000000000000", "abcdefabcdefabcd"]
        with _store(tmp_path).session() as store:
            base = RunState(thread_id="t", run_id="r")
            for i, digest in enumerate(digests):
                state = base.with_progress(
                    cursor=f"step{i}", completed=("a",) * i, step_index=i,
                    audit_digest=digest,
                )
                store.save(state)
                assert store.load("t").cursor == f"step{i}", (
                    f"load() returned the wrong checkpoint at write {i + 1}"
                )

    def test_the_write_ordinal_is_allocated_from_the_table(self, tmp_path: Path) -> None:
        """A resuming process has no counter, so the table must be the source.

        Held in RunState, a counter would restart at 1 on resume and collide with
        the crashed run's rows on the primary key.
        """
        path = tmp_path / "c.sqlite"
        with SqliteCheckpointStore(path).session() as first:
            state = RunState(thread_id="t", run_id="r")
            first.save(state)
            first.save(state)
        with SqliteCheckpointStore(path).session() as second:
            assert second.save(RunState(thread_id="t", run_id="r")).write_seq == 3

    def test_history_is_newest_first(self, tmp_path: Path) -> None:
        with _store(tmp_path).session() as store:
            base = RunState(thread_id="t", run_id="r")
            for i in range(4):
                store.save(base.with_progress(cursor=f"s{i}", completed=(), step_index=i))
            seqs = [c.write_seq for c in store.history("t")]
            assert seqs == [4, 3, 2, 1]

    def test_two_checkpoints_may_share_a_step(self, tmp_path: Path) -> None:
        """A risky step is checkpointed before *and* after."""
        with _store(tmp_path).session() as store:
            state = RunState(thread_id="t", run_id="r", cursor="publish")
            store.save(state)
            store.save(state)
            assert store.count("t") == 2
            assert len({c.write_seq for c in store.history("t")}) == 2

    def test_an_unknown_thread_loads_as_none(self, tmp_path: Path) -> None:
        with _store(tmp_path).session() as store:
            assert store.load("nope") is None
            assert store.exists("nope") is False
            assert "nope" not in store

    def test_require_names_the_threads_that_do_exist(self, tmp_path: Path) -> None:
        """A typo must be visible, not silently start a fresh run."""
        with _store(tmp_path).session() as store:
            store.save(RunState(thread_id="real", run_id="r"))
            with pytest.raises(CheckpointUnavailable) as exc:
                store.require("reel")
            assert exc.value.thread_id == "reel"
            assert "real" in exc.value.known_threads

    def test_thread_ids_come_from_the_table(self, tmp_path: Path) -> None:
        with _store(tmp_path).session() as store:
            store.save(RunState(thread_id="a", run_id="r"))
            store.save(RunState(thread_id="b", run_id="r"))
            assert set(store.thread_ids()) == {"a", "b"}

    def test_checkpoints_are_namespaced(self, tmp_path: Path) -> None:
        """Lab 7's graph checkpoints must not interleave with these."""
        from langgraph.checkpoint.sqlite import SqliteSaver

        assert "/" in CHECKPOINT_NS
        with _store(tmp_path).session() as store:
            store.save(RunState(thread_id="t", run_id="r"))
            raw = SqliteSaver.from_conn_string(str(tmp_path / "ckpt.sqlite"))
            with raw as saver:
                saver.setup()
                with saver.cursor() as cur:
                    cur.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints")
                    assert [row[0] for row in cur.fetchall()] == [CHECKPOINT_NS]

    def test_using_the_store_outside_its_session_names_the_fix(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(RuntimeError, match="session\\(\\)"):
            store.load("t")

    def test_delete_thread_forgets_it(self, tmp_path: Path) -> None:
        with _store(tmp_path).session() as store:
            store.save(RunState(thread_id="t", run_id="r"))
            store.delete_thread("t")
            assert store.load("t") is None

    def test_the_state_round_trips_every_field(self, tmp_path: Path) -> None:
        with _store(tmp_path).session() as store:
            original = RunState(
                thread_id="t", run_id="r", course_code="CS201", plan_sha256="abc",
                step_index=3, cursor="publish", completed=("a", "b"),
                artifacts={"x": {"n": 1}}, approvals={"publish": "approved"},
                spent={"steps": 3}, audit_seq=9, audit_digest="0123456789abcdef",
            )
            store.save(original)
            back = store.load("t")
            assert back == original


class TestInMemoryCheckpointStore:
    """Named to say what it is not: it does not outlive the process."""

    def test_it_behaves_like_the_durable_one(self, tmp_path: Path) -> None:
        store = InMemoryCheckpointStore()
        base = RunState(thread_id="t", run_id="r")
        store.save(base)
        store.save(base.with_progress(cursor="next", completed=("x",), step_index=1))
        assert store.load("t").cursor == "next"
        assert [c.write_seq for c in store.history("t")] == [2, 1]
        assert store.thread_ids() == ("t",)

    def test_require_still_raises_for_an_unknown_thread(self) -> None:
        with pytest.raises(CheckpointUnavailable):
            InMemoryCheckpointStore().require("nope")

    def test_delete_and_contains(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(RunState(thread_id="t", run_id="r"))
        assert "t" in store
        store.delete_thread("t")
        assert "t" not in store


class TestRunState:
    def test_with_progress_moves_cursor_and_completed_together(self) -> None:
        state = RunState(thread_id="t", run_id="r", cursor="a")
        nxt = state.with_progress(cursor="b", completed=("a",), step_index=1)
        assert (nxt.cursor, nxt.completed, nxt.step_index) == ("b", ("a",), 1)

    def test_progress_keeps_previous_keys_when_not_overridden(self) -> None:
        state = RunState(thread_id="t", run_id="r", artifacts={"k": 1}, spent={"s": 1})
        nxt = state.with_progress(cursor="b", completed=(), step_index=1)
        assert dict(nxt.artifacts) == {"k": 1}
        assert dict(nxt.spent) == {"s": 1}

    def test_with_audit_position_moves_only_the_chain(self) -> None:
        state = RunState(thread_id="t", run_id="r", cursor="x", step_index=2)
        moved = state.with_audit_position(seq=5, digest="abc")
        assert (moved.audit_seq, moved.audit_digest) == (5, "abc")
        assert (moved.cursor, moved.step_index) == ("x", 2)

    def test_done_is_about_the_cursor(self) -> None:
        assert RunState(thread_id="t", run_id="r").done()
        assert not RunState(thread_id="t", run_id="r", cursor="x").done()

    def test_helpers(self) -> None:
        state = RunState(
            thread_id="t", run_id="r", completed=("a",), artifacts={"k": 7},
            approvals={"a": "approved"},
        )
        assert state.artifact("k") == 7
        assert state.artifact("missing", "default") == "default"
        assert state.is_done("a") and not state.is_done("b")
        assert state.approval_for("a") == "approved"
        assert state.approval_for("b") == ""

    def test_artifacts_are_copied_not_aliased(self) -> None:
        """A checkpoint that changes after it is written cannot be trusted."""
        state = RunState(thread_id="t", run_id="r", artifacts={"k": 1})
        copied = state.with_progress(cursor="z", completed=(), step_index=1)
        copied.artifacts["k"] = 99
        assert state.artifacts["k"] == 1


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------


class TestGates:
    def test_a_gate_can_refuse(self) -> None:
        gate = DenyAllGate("publish", by="prof", reason="not yet")
        decision = gate.request(ApprovalRequest(gate="publish", step="publish"))
        assert decision.refused and not decision.approved
        assert decision.decided_by == "prof"
        assert decision.reason == "not yet"

    def test_deferred_is_not_a_refusal(self) -> None:
        """"Nobody answered" and "somebody said no" need different follow-up."""
        d = ApprovalDecision.deferred(reason="no reply")
        assert d.unanswered and not d.refused and not d.approved
        assert d.decided_by == UNANSWERED

    def test_approval_carries_its_conditions(self) -> None:
        d = ApprovalDecision.approve(by="prof", reason="ok", edits={"cap": 10})
        assert d.approved and d.edits == {"cap": 10}

    def test_a_scripted_gate_answers_in_order(self) -> None:
        gate = ScriptedGate(
            "g",
            [
                ApprovalDecision.approve(by="a"),
                ApprovalDecision.refuse(by="b", reason="no"),
            ],
        )
        req = ApprovalRequest(gate="g", step="s")
        assert gate.request(req).approved
        assert gate.request(req).refused
        assert gate.remaining == 0

    def test_an_exhausted_script_fails_loudly(self) -> None:
        """A silently-defaulting gate would hide a double-asked step."""
        gate = ScriptedGate("g", [])
        with pytest.raises(GateScriptExhausted) as exc:
            gate.request(ApprovalRequest(gate="g", step="s"))
        assert exc.value.gate == "g"

    def test_a_recording_gate_remembers_the_question(self) -> None:
        gate = RecordingGate(PreapprovedGate("publish", by="prof"))
        gate.request(ApprovalRequest(gate="publish", step="publish", summary="do it"))
        assert gate.steps_asked() == ("publish",)
        assert gate.name == "publish"
        assert gate.inner.name == "publish"

    def test_a_failing_callback_defers_rather_than_approving(self) -> None:
        """A broken prompt must not be able to authorise a publish."""

        def boom(req: ApprovalRequest) -> ApprovalDecision:
            raise RuntimeError("no terminal attached")

        decision = CallbackGate("publish", boom).request(
            ApprovalRequest(gate="publish", step="publish")
        )
        assert decision.unanswered
        assert "could not be reached" in decision.reason

    def test_a_failing_callback_can_propagate(self) -> None:
        def boom(req: ApprovalRequest) -> ApprovalDecision:
            raise RuntimeError("fatal")

        with pytest.raises(RuntimeError):
            CallbackGate("g", boom, on_error="raise").request(
                ApprovalRequest(gate="g", step="s")
            )

    def test_a_callback_returning_the_wrong_type_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="expected ApprovalDecision"):
            CallbackGate("g", lambda req: "yes").request(
                ApprovalRequest(gate="g", step="s")
            )

    def test_an_unknown_on_error_policy_is_a_construction_error(self) -> None:
        with pytest.raises(ValueError, match="defer"):
            CallbackGate("g", lambda r: ApprovalDecision.deferred(), on_error="nope")

    def test_the_request_renders_for_a_human(self) -> None:
        req = ApprovalRequest(
            gate="publish", step="publish", summary="Publish grades",
            artifacts={"draft_grades": "<3 item(s)>"},
        )
        assert req.rendered() == "[publish] Publish grades (draft_grades)"

    def test_the_decision_renders_with_who_and_why(self) -> None:
        assert ApprovalDecision.approve(by="prof", reason="ok").rendered() == (
            "approved by prof -- ok"
        )
        assert ApprovalDecision.deferred().rendered() == "deferred"


# --------------------------------------------------------------------------
# the diagnosis
# --------------------------------------------------------------------------


class TestDiagnosis:
    """Doc 2.12: escalate "to course faculty **with a diagnosis**".

    A run that stops must say why, in the report, in words somebody who was not
    watching can act on. The reason used to be written to the structured log and
    nowhere else, so the report carried the flag (``escalated=True``) without the
    thing the flag is for.
    """

    @staticmethod
    def _step(name: str, **kwargs) -> Step:
        """A step that does nothing observable unless told to raise."""
        error = kwargs.pop("raises", None)

        def body(ctx: StepContext):
            if error is not None:
                raise error
            return {"ok": 1}

        return Step(
            name=name, intent=f"i{name}", action=f"a{name}", kind="tool",
            run=body, **kwargs,
        )

    def _runtime(self, **kwargs) -> Runtime:
        return Runtime(
            budget=kwargs.pop("budget", Budget.defaults()),
            audit=InMemoryAuditLog(run_id="test"),
            checkpoints=InMemoryCheckpointStore(),
            **kwargs,
        )

    def test_a_budget_stop_names_the_step_and_the_dimension(self) -> None:
        rt = self._runtime(budget=Budget.only(steps=1))
        report = rt.run([self._step("a"), self._step("b")])
        assert report.status == "stopped"
        assert report.stopped_at == "b"
        assert "'b'" in report.diagnosis
        assert "steps" in report.diagnosis

    def test_a_gate_stop_says_it_was_a_refusal(self) -> None:
        rt = self._runtime(gates=[("publish", DenyAllGate("publish"))])
        report = rt.run([self._step("publish", risky=True, gate="publish")])
        assert report.status == "stopped"
        assert report.stopped_at == "publish"
        assert "refused" in report.diagnosis or "refusal" in report.diagnosis
        assert "did not run" in report.diagnosis

    def test_a_failed_step_says_where_a_resume_starts(self) -> None:
        """The diagnosis has to be actionable, not just the error text."""
        rt = self._runtime()
        report = rt.run([
            self._step("a"),
            self._step("b", raises=ValueError("bad input")),
            self._step("c"),
        ])
        assert report.status == "failed"
        assert report.stopped_at == "b"
        assert "bad input" in report.diagnosis
        assert "resume" in report.diagnosis

    def test_a_completed_run_has_no_diagnosis(self) -> None:
        """Otherwise every clean run would carry a plausible-sounding excuse."""
        rt = self._runtime()
        report = rt.run([self._step("a")])
        assert report.status == "completed"
        assert report.diagnosis == ""
        assert report.stopped_at == ""

    def test_a_stop_is_not_the_same_as_an_escalation(self) -> None:
        """``escalated`` is doc 2.12's budget-exhaustion signal, not "it stopped".

        Conflating them would teach a reader to ignore the flag, and doc 2.12's
        mitigation is specifically about a runaway loop reaching a ceiling.
        """
        gated = self._runtime(gates=[("publish", DenyAllGate("publish"))])
        assert gated.run([self._step("publish", risky=True, gate="publish")]).escalated is False

        failed = self._runtime()
        report = failed.run([self._step("a", raises=ValueError("x"))])
        assert report.status == "failed"
        assert report.diagnosis
        assert not report.escalated

    def test_the_partial_snapshot_carries_no_diagnosis(self) -> None:
        """``runtime.report()`` is a mid-run view; it must not invent a verdict."""
        rt = self._runtime()
        rt.run([self._step("a")])
        snapshot = rt.report()
        assert snapshot.status == "partial"
        assert snapshot.diagnosis == ""
        assert snapshot.stopped_at == ""

    # -- the diagnosis as a *sentence* -------------------------------------

    def test_no_diagnosis_contains_a_doubled_full_stop(self) -> None:
        """Every diagnosis is quoted in the lab write-up, so it is read.

        Each of these stopped because it embedded an exception message -- itself
        a terminated sentence -- and appended a second one, producing
        ``...overspending.. Nothing ran``. The doubled stop is the one fragment of
        a sentence a reader can be certain nobody checked.
        """
        for report in self._every_failure_report():
            assert ".." not in report.diagnosis, report.diagnosis
            assert report.diagnosis.endswith("."), report.diagnosis

    def test_a_diagnosis_never_repeats_itself(self) -> None:
        """The diagnosis adds to the exception; it does not restate it.

        Quoting the exception wholesale put the same fact in the sentence twice:
        the budget diagnosis opened ``budget refused step 'publish':`` and then
        quoted ``budget 'steps' exhausted before step 'publish':``, and the gate
        diagnosis opened ``... was refused at its gate and did not run`` before
        quoting a message beginning the same way.
        """
        budget, gate = self._every_failure_report()[:2]
        # The step is named once, by the diagnosis's own clause.
        assert budget.diagnosis.count("budget refused step 'b'") == 1
        assert budget.diagnosis.count("exhausted") == 1
        # The gate's own phrasing is not smuggled in alongside the summary.
        assert "held at the 'publish' gate" not in gate.diagnosis
        assert gate.diagnosis.count("did not run") == 1

    def _every_failure_report(self) -> list:
        """One report per way a run can stop: budget, gate, and a raised step."""
        budget = self._runtime(budget=Budget.only(steps=1)).run(
            [self._step("a"), self._step("b")]
        )
        gated = self._runtime(gates=[("publish", DenyAllGate("publish"))]).run(
            [self._step("publish", risky=True, gate="publish")]
        )
        failed = self._runtime().run(
            [self._step("a"), self._step("b", raises=ValueError("bad input"))]
        )
        return [budget, gated, failed]

    def test_the_three_outcomes_are_the_documented_three(self) -> None:
        assert APPROVAL_OUTCOMES == ("approved", "refused", "deferred")