"""Lab 6 -- Governed runtime: budgets, gates, checkpoint/resume, audit log

Document requirement (2.10, Lab 6)
---------------------------------
* **Phase:** A - Foundations
* **Goal:** "Run the agent inside a controlled runtime with a full audit log."
* **What it adds:** "A runtime binding skills + tools + memory behind the loop,
  enforcing budgets and approval gates and logging every step -- the backbone
  later labs plug into."
* **Inputs:** Skills (Lab 3), connector (Lab 5), memory (Lab 4).
* **Outputs:** A governed run with audit + checkpoints.
* **Agents/nodes:** Coordinator hardened into the runtime controller.
* **Deliverable:** "Runtime wrapping the agent: budgets, gates, checkpoint/resume,
  audit log."
* **Accept:** "Every action is logged with inputs/outputs; a crashed run resumes
  from its last checkpoint."
* **Nodes activated:** Coordinator runtime around all nodes.
* **Pitfall to avoid:** "Log inputs and outputs, not just messages; checkpoint
  before risky steps or resume loses state."
* **Pseudocode:**

      run = Runtime(budget=B, gates=[publish])
      for step in agent.loop():
          audit.log(step.intent, step.action, step.result)
          if step.risky: wait_for_approval(step)
          checkpoint(run.state)

Why the pitfall is the deliverable
----------------------------------
The pseudocode and the pitfall disagree about order, and the disagreement is the
whole lab. Read literally, the loop audits *before* it gates -- so a refused
risky step is never logged with the inputs proposed for it -- and it checkpoints
*after* the gate -- so a run that dies inside ``wait_for_approval`` returns to a
state that does not record having asked. The pitfall line is the correction to
both, and :class:`~course_organizer.runtime.runtime.Runtime` implements the
corrected order.

This module builds a governed run over the demo plan, shows the budget and the
gate refusing *before* an action, proves a crashed run resumes in a **separate
process** without repeating a publish, and encodes the acceptance criterion as
executable checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from course_organizer.runtime import (
    DEFAULT_GATE,
    HUMAN_AUTHORITY,
    Budget,
    DenyAllGate,
    JsonlAuditLog,
    PreapprovedGate,
    RecordingGate,
    RunReport,
    Runtime,
    RuntimeBoundaryError,
    SqliteCheckpointStore,
    Step,
    StepContext,
    plan_digest,
)

LAB = "Lab 6"
DOC_GOAL = "Run the agent inside a controlled runtime with a full audit log."
DOC_DELIVERABLE = (
    "Runtime wrapping the agent: budgets, gates, checkpoint/resume, audit log."
)
DOC_ACCEPTANCE = (
    "Every action is logged with inputs/outputs; a crashed run resumes from its "
    "last checkpoint."
)
DOC_NODES = "Coordinator runtime around all nodes."
DOC_PITFALL = (
    "Log inputs and outputs, not just messages; checkpoint before risky steps or "
    "resume loses state."
)

#: The document's pseudocode, one line per statement.
DOC_PSEUDOCODE = (
    "run = Runtime(budget=B, gates=[publish])\n"
    "for step in agent.loop():\n"
    "    audit.log(step.intent, step.action, step.result)\n"
    "    if step.risky: wait_for_approval(step)\n"
    "    checkpoint(run.state)"
)

#: The order the runtime actually uses, for contrast with :data:`DOC_PSEUDOCODE`.
#: The two differences are the pitfall's corrections, marked.
RUNTIME_ORDER: tuple[str, ...] = (
    "reserve budget (refuse before the action)",
    "check wall clock for an overrun already spent",
    "if step.risky: checkpoint(run.state)  <- pitfall: before, not after",
    "if step.risky: wait_for_approval(step)",
    "run the step",
    "charge what it actually cost",
    "audit.log(inputs, outputs)  <- pitfall: inputs AND outputs",
    "checkpoint(run.state)",
)

#: The plan this lab runs. Five steps, one of them risky, spanning the three
#: families the document names as Lab 6's inputs: a skill (Lab 3), memory-like
#: tool work, and the connector (Lab 5).
DEMO_STEPS: tuple[str, ...] = (
    "read_notes",
    "draft_plan",
    "draft_grades",
    "publish",
    "report",
)

#: The step a crash is injected into for the resume proof. The last one, so the
#: crash lands *after* the risky publish -- which is the only placement that can
#: show the publish was not repeated.
CRASH_STEP = "report"

__all__ = [
    "CRASH_STEP",
    "DEMO_STEPS",
    "DOC_ACCEPTANCE",
    "DOC_DELIVERABLE",
    "DOC_GOAL",
    "DOC_NODES",
    "DOC_PITFALL",
    "DOC_PSEUDOCODE",
    "LAB",
    "RUNTIME_ORDER",
    "CheckpointProof",
    "LabRunResult",
    "build_demo_plan",
    "crash_and_resume",
    "demo_budget",
    "demo_gate_refusal",
    "governed_run",
    "run_lab06",
]


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointProof:
    """Evidence for "a crashed run resumes from its last checkpoint".

    Built from two real subprocesses, not from an in-process simulation, because
    the claim is about surviving *process death*. ``InMemoryCheckpointStore``
    cannot demonstrate it and is not used here.

    Attributes:
        crash_exit_code: What the crashing process exited with.
        crashed: Did it die the way it was told to?
        resumed_steps: Which steps the resuming process executed.
        skipped: Which steps it did not, having found them complete.
        publish_reran: Did the risky ``publish`` step execute twice in total?
        gate_asked_again: Did resume re-consult the gate for a step already done?
        ok_records: How many ``ok`` audit records carry both inputs and outputs.
        empty_ok_records: How many ``ok`` records are missing one of the two.
        audit_records: The chain length across both processes.
        audit_verified: Did the whole chain verify after the resume.
        fingerprint: The chain's fingerprint, for the report.
        path: Where the log and checkpoint were written.
    """

    crash_exit_code: int
    crashed: bool
    resumed_steps: tuple[str, ...]
    skipped: tuple[str, ...]
    publish_reran: bool
    gate_asked_again: bool
    ok_records: int
    empty_ok_records: int
    audit_records: int
    audit_verified: bool
    fingerprint: str
    path: str = ""

    @property
    def pass_resume(self) -> bool:
        """Does this satisfy the second half of the acceptance criterion?"""
        return (
            self.crashed
            and self.resumed_steps == (CRASH_STEP,)
            and not self.publish_reran
            and not self.gate_asked_again
            and self.empty_ok_records == 0
            and self.audit_verified
        )

    def rendered(self) -> str:
        """A four-line summary for the run log."""
        return (
            f"crash exit={self.crash_exit_code} (expected 9)\n"
            f"  resumed ran    : {self.resumed_steps}\n"
            f"  resumed skipped: {self.skipped}\n"
            f"  publish reran  : {self.publish_reran}; gate asked again: "
            f"{self.gate_asked_again}\n"
            f"  ok records with inputs+outputs: {self.ok_records}; missing: "
            f"{self.empty_ok_records}\n"
            f"  audit chain    : {self.audit_records} record(s), verified="
            f"{self.audit_verified}, fingerprint {self.fingerprint}"
        )


@dataclass(frozen=True)
class LabRunResult:
    """The self-checking outcome of one Lab 6 run.

    Attributes:
        lab: Always ``"Lab 6"``.
        pass_all: Did every acceptance check pass.
        checks: One dict per check: ``name``, ``passed``, ``detail``.
        clean: The uninterrupted governed run.
        gate: The run that stopped at the gate.
        budget: The run that stopped at the budget.
        proof: The crash-and-resume evidence.
        root: Where the evidence was written, as a string. Carried so a caller
            (the notebook, a test) can go and read the log and checkpoint without
            having to guess where ``run_lab06`` put them.
        notes: ASCII-only remarks worth printing.
    """

    lab: str
    pass_all: bool
    checks: tuple[Mapping[str, Any], ...]
    clean: RunReport | None
    gate: RunReport | None
    budget: RunReport | None
    proof: CheckpointProof | None
    root: str = ""
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view, for the notebook and the evidence file."""
        proof = self.proof
        return {
            "lab": self.lab,
            "pass_all": self.pass_all,
            "root": self.root,
            "checks": [dict(c) for c in self.checks],
            "clean_status": self.clean.status if self.clean else "",
            "clean_steps": list(self.clean.steps_run()) if self.clean else [],
            "gate_status": self.gate.status if self.gate else "",
            "budget_status": self.budget.status if self.budget else "",
            "proof": {
                "crashed": proof.crashed,
                "resumed_steps": list(proof.resumed_steps),
                "skipped": list(proof.skipped),
                "publish_reran": proof.publish_reran,
                "audit_records": proof.audit_records,
                "audit_verified": proof.audit_verified,
            }
            if proof
            else None,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# The demo plan
# --------------------------------------------------------------------------


def build_demo_plan(*, crash_at: str = "") -> tuple[Step, ...]:
    """The five-step demo plan, spanning skill / connector / llm / tool work.

    The step names and actions are the document's concepts rather than invented
    ones: ``read_notes_store`` is Lab 2's tool as exposed through Lab 5's
    connector, ``plan_skill`` is Lab 3, ``grade_draft`` is the draft-grade step
    that Lab 10 will validate, and ``publish`` is the doc's own gate name from
    ``Runtime(budget=B, gates=[publish])``.

    Args:
        crash_at: The name of the step that calls ``os._exit(9)`` when reached.
            Empty, the default, means no step dies.

    Returns:
        The plan, as an immutable sequence.
    """

    def read_notes(ctx: StepContext) -> Mapping[str, Any]:
        """Stand in for Lab 5's ``read_notes_store`` through the connector."""
        return {
            "course_code": "CS201",
            "records": 8,
            "titles": ["Trees", "Graphs", "Hashing", "Heaps"],
        }

    def expand_plan(ctx: StepContext) -> Mapping[str, Any]:
        """Stand in for Lab 3's plan skill."""
        return {
            "plan_id": "PLAN-CS201-L06",
            "steps_planned": 3,
            "units": 5,
            "used_read_records": ctx.prior("read_notes", {}).get("records", 0),
        }

    def draft_grades(ctx: StepContext) -> Mapping[str, Any]:
        """Stand in for the draft-grade step; Lab 10 adds the validation gate."""
        return {"drafts": 3, "max_marks": 100, "status": "draft"}

    def publish_workspace(ctx: StepContext) -> Mapping[str, Any]:
        """The risky, gated step. Never runs without approval."""
        return {
            "published": 3,
            "version": 1,
            "graded_by": HUMAN_AUTHORITY,
            "drafts_in": ctx.prior("draft_grades", {}).get("drafts", 0),
        }

    def render_report(ctx: StepContext) -> Mapping[str, Any]:
        """A final reporting step, so the run has an end state worth reporting."""
        return {
            "summary": "governed run complete",
            "published": ctx.prior("publish", {}).get("published", 0),
        }

    def wrap(name: str, fn: Any) -> Any:
        """Wrap a step body so the chosen step can die for real."""

        def body(ctx: StepContext) -> Mapping[str, Any]:
            if crash_at and name == crash_at:
                # A genuine, uncatchable process death: no exception, no
                # atexit, no finally. This is what makes the resume proof a
                # proof rather than a simulation.
                os._exit(9)
            return fn(ctx)

        body.__name__ = f"{name}_body"
        return body

    return (
        Step(
            name="read_notes",
            intent="Read the notes store through the connector, not from memory",
            action="read_notes_store",
            kind="connector",
            run=wrap("read_notes", read_notes),
            cost={"records_read": 8},
        ),
        Step(
            name="draft_plan",
            intent="Expand the clarified request into a locked plan",
            action="plan_skill",
            kind="skill",
            run=wrap("draft_plan", expand_plan),
        ),
        Step(
            name="draft_grades",
            intent="Compute draft grades against the faculty rubric",
            action="grade_draft",
            kind="llm",
            run=wrap("draft_grades", draft_grades),
        ),
        Step(
            name="publish",
            intent="Publish the graded workspace to the faculty-visible gradebook",
            action="publish_workspace",
            kind="tool",
            run=wrap("publish", publish_workspace),
            risky=True,
            gate=DEFAULT_GATE,
            summary=(
                "Publish 3 draft grades to the faculty-visible gradebook. This "
                "is irreversible without a new version."
            ),
        ),
        Step(
            name="report",
            intent="Write the run report",
            action="render_report",
            kind="tool",
            run=wrap("report", render_report),
        ),
    )


# --------------------------------------------------------------------------
# The governed runs
# --------------------------------------------------------------------------


def governed_run(
    root: Path,
    *,
    thread: str,
    run_id: str,
    budget: Budget,
    gate,
    plan: Sequence[Step] | None = None,
    course_code: str = "CS201",
    resume: bool = False,
    gates: Mapping[str, Any] | None = None,
) -> tuple[Runtime, RunReport, JsonlAuditLog, SqliteCheckpointStore]:
    """Run (or resume) the demo plan under one budget, gate pair and log.

    Written once so the three demos and the crash child all construct a runtime
    the same way. An audit log, a checkpoint file and a runtime built slightly
    differently per demo would make any difference between them ambiguous.

    Args:
        root: Directory holding the log and checkpoint.
        thread: The resume key. Must match across the crash and the resume.
        run_id: This process's run id. Differs from the crashed process's, on
            purpose: two processes, two runs, one log.
        budget: The ceilings to enforce.
        gate: The gate instance registered under :data:`DEFAULT_GATE`.
        plan: The plan. Defaults to :func:`build_demo_plan`.
        course_code: The course, so resume can prove it is the same work.
        resume: Whether to adopt the existing checkpoint first.
        gates: Overrides the gate registry, for the crash-proof child which needs
            a named gate it can inspect.

    Returns:
        The runtime, its report, its audit log and its checkpoint store.
    """
    steps = tuple(plan) if plan is not None else build_demo_plan()
    audit = JsonlAuditLog(root / f"{thread}.jsonl", run_id=run_id)
    store = SqliteCheckpointStore(root / f"{thread}.sqlite", run_id=run_id)
    runtime = Runtime(
        budget=budget,
        gates=dict(gates) if gates is not None else {DEFAULT_GATE: gate},
        audit=audit,
        checkpoints=store,
        run_id=run_id,
        thread_id=thread,
        course_code=course_code,
        plan_sha256=plan_digest(steps),
    )
    with store.session():
        if resume:
            runtime.resume()
        elif store.exists(thread):
            # Refuse rather than silently adopt. `thread` is the resume key: a
            # fresh run pointed at a thread that already holds a checkpoint would
            # append to somebody else's audit chain, and the chain check would
            # then blame the log for a wiring mistake. This is the same class of
            # bug as resuming the wrong thread, and the same fix: say so.
            raise RuntimeBoundaryError(
                f"thread {thread!r} already holds a checkpoint under {store.path}. "
                f"Pass resume=True to continue that run, or a different thread to "
                f"start a new one. A fresh run cannot share a resume key with an "
                f"existing one."
            )
        report = runtime.run(steps)
    return runtime, report, audit, store


def demo_gate_refusal(root: Path, *, thread: str | None = None) -> tuple[RunReport, JsonlAuditLog]:
    """A run whose ``publish`` step is refused, so it never runs.

    The demonstration is the *absence* of the side effect: ``publish`` produced
    no output and no ``ok`` record, while the refusal itself is on the log with
    the approver's identity and the approval request that was presented.
    """
    thread = thread or f"gate-{uuid.uuid4().hex[:8]}"
    gate = RecordingGate(
        DenyAllGate(
            DEFAULT_GATE,
            by=HUMAN_AUTHORITY,
            reason="the CS201 rubric needs a second look before grades go live",
        )
    )
    _, report, audit, _ = governed_run(
        root,
        thread=thread,
        run_id="lab06-gate",
        budget=Budget.defaults(),
        gate=gate,
    )
    return report, audit


def demo_budget(root: Path, *, thread: str | None = None) -> tuple[RunReport, JsonlAuditLog]:
    """A run that stops because a budget dimension is exhausted.

    ``steps`` is set to 3, so the fourth step is refused. The refusal happens
    before that step's body runs, which is the property worth showing: the step
    after the one that would have overspent is simply never entered, and
    :attr:`RunReport.escalated` is set because doc 2.12 requires exhaustion to
    reach course faculty with a diagnosis rather than to continue.
    """
    thread = thread or f"budget-{uuid.uuid4().hex[:8]}"
    tight = Budget(
        limits={
            "steps": 3,
            "risky_steps": 4,
            "tool_calls": 12,
            "skill_calls": 8,
            "connector_calls": 12,
            "llm_calls": 8,
            "records_read": 200,
            "wall_clock_ms": 120_000,
        }
    )
    _, report, audit, _ = governed_run(
        root,
        thread=thread,
        run_id="lab06-budget",
        budget=tight,
        gate=PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY),
    )
    return report, audit


# --------------------------------------------------------------------------
# Crash and resume, in two real processes
# --------------------------------------------------------------------------

#: The child program. Written to disk rather than passed with ``-c`` so that no
#: quoting layer sits between this module and the crash, and so the exact
#: program that dies can be read and cited in the lab documentation.
_CRASH_CHILD = textwrap.dedent(
    '''
    """Lab 6 crash/resume child. argv: phase root thread.

    phase="crash"  runs the plan and dies inside the ``report`` step.
    phase="resume" adopts the checkpoint and finishes, printing KEY=VALUE lines.
    """
    import sys
    from pathlib import Path

    from course_organizer.labs.lab06_governed_runtime import build_demo_plan
    from course_organizer.runtime import (
        Budget,
        JsonlAuditLog,
        PreapprovedGate,
        RecordingGate,
        Runtime,
        RunState,
        SqliteCheckpointStore,
        plan_digest,
    )

    phase, root, thread = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    plan = build_demo_plan(crash_at="report" if phase == "crash" else "")
    audit = JsonlAuditLog(root / f"{thread}.jsonl", run_id=phase)
    store = SqliteCheckpointStore(root / f"{thread}.sqlite", run_id=phase)
    inner = PreapprovedGate("publish", by="course_faculty")
    gate = RecordingGate(inner)
    runtime = Runtime(
        budget=Budget.defaults(),
        gates={"publish": gate},
        audit=audit,
        checkpoints=store,
        run_id=phase,
        thread_id=thread,
        course_code="CS201",
        plan_sha256=plan_digest(plan),
    )

    with store.session():
        if phase == "crash":
            store.save(
                RunState(thread_id=thread, run_id=phase, course_code="CS201")
            )
            runtime.resume()
            runtime.run(plan)
            # The plan's report step calls os._exit(9) and never returns, so
            # reaching this line would mean the crash did not happen.
            print("CHILD=crash-returned-unexpectedly")
        else:
            runtime.resume()
            report = runtime.run(plan)
            print(f"STEPS_RUN={','.join(report.steps_run())}")
            print(f"SKIPPED={','.join(report.skipped)}")
            print(f"GATE_ASKED={','.join(gate.steps_asked())}")
            print(f"AUDIT_RECORDS={report.audit_records}")
            print(f"AUDIT_VERIFIED={report.audit_fingerprint}")
            print(f"STATUS={report.status}")
    '''
).lstrip()


def _write_child(directory: Path) -> Path:
    """Write the child program and return its path."""
    path = directory / "_lab06_crash_child.py"
    path.write_text(_CRASH_CHILD, encoding="utf-8")
    return path


def _run_child(child: Path, phase: str, root: Path, thread: str) -> subprocess.CompletedProcess:
    """Run the child with the project ``src`` importable and no inherited state."""
    env = dict(os.environ)
    src = str((Path(__file__).resolve().parents[1]))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, str(child), phase, str(root), thread],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _parse_kv(stdout: str) -> dict[str, str]:
    """Parse the child's ``KEY=VALUE`` lines, ignoring anything else."""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line and not line.startswith(" "):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def crash_and_resume(root: Path, *, thread: str | None = None) -> CheckpointProof:
    """Kill a run mid-flight and finish it in a separate process.

    Two subprocesses, sharing one audit log file and one checkpoint database.
    The first really dies -- ``os._exit(9)`` inside the ``report`` step, which is
    after the risky ``publish`` -- and the second adopts the checkpoint. The
    property measured is not that the second process ran, but that ``publish``
    did **not** run a second time and the gate was not asked a second time.

    Args:
        root: Where the log and checkpoint are written.
        thread: The resume key. Generated if not given.

    Returns:
        The evidence, including the child's raw exit code.
    """
    root.mkdir(parents=True, exist_ok=True)
    thread = thread or f"crash-{uuid.uuid4().hex[:8]}"
    child = _write_child(root)

    crashed_proc = _run_child(child, "crash", root, thread)
    resumed_proc = _run_child(child, "resume", root, thread)
    values = _parse_kv(resumed_proc.stdout)

    resumed_steps = tuple(v for v in values.get("STEPS_RUN", "").split(",") if v)
    skipped = tuple(v for v in values.get("SKIPPED", "").split(",") if v)
    gate_asked = tuple(v for v in values.get("GATE_ASKED", "").split(",") if v)

    audit = JsonlAuditLog(root / f"{thread}.jsonl", run_id="verify")
    records = audit.records()
    ok_records = [r for r in records if r.outcome == "ok"]
    empty_ok = [r for r in ok_records if not r.inputs or not r.outputs]
    verified = False
    fingerprint = ""
    try:
        verified = audit.verify() == len(records)
        fingerprint = audit.fingerprint()
    except Exception:  # noqa: BLE001 - a broken chain is the finding
        verified = False

    return CheckpointProof(
        crash_exit_code=crashed_proc.returncode,
        crashed=crashed_proc.returncode == 9,
        resumed_steps=resumed_steps,
        skipped=skipped,
        publish_reran="publish" in resumed_steps,
        gate_asked_again=bool(gate_asked),
        ok_records=len(ok_records),
        empty_ok_records=len(empty_ok),
        audit_records=len(records),
        audit_verified=verified,
        fingerprint=fingerprint,
        path=str(root),
    )


# --------------------------------------------------------------------------
# Acceptance checks
# --------------------------------------------------------------------------


def _check(name: str, passed: bool, detail: str) -> Mapping[str, Any]:
    """One acceptance check, in the shape the report and the tests both read."""
    return {"name": name, "passed": bool(passed), "detail": detail}


def acceptance_checks(
    clean: RunReport,
    gate: RunReport,
    budget: RunReport,
    proof: CheckpointProof,
    *,
    clean_audit,
    gate_audit,
    budget_audit,
) -> tuple[Mapping[str, Any], ...]:
    """Every acceptance check Lab 6 claims, evaluated against real runs.

    Each check is a claim the document makes or the pitfall states, tested
    against something that actually happened rather than against the runtime's
    own description of itself. The count is reported; it is not a target.

    The two halves of the acceptance criterion are split deliberately:

    * *"Every action is logged with inputs/outputs"* is checked on the
      uninterrupted run, where every step succeeded, and separately on the
      gate-refusal run, where a *refused* action must still be logged -- with the
      approval request in ``inputs`` and empty ``outputs``, because it produced
      nothing. Both are required; a log that only covers successes is exactly the
      half-written log the pitfall warns about.
    * *"A crashed run resumes from its last checkpoint"* is checked by the two
      subprocesses in :func:`crash_and_resume`, not by this function's
      reasoning.
    """
    checks: list[Mapping[str, Any]] = []

    # --- structure -------------------------------------------------------
    ran = clean.steps_run()
    checks.append(
        _check(
            "clean_run_completes_every_step",
            clean.ok and ran == DEMO_STEPS,
            f"status={clean.status}, ran={ran}",
        )
    )
    checks.append(
        _check(
            "the_publish_step_is_the_gated_one",
            any(g.startswith(DEFAULT_GATE) for g in clean.gate_answers),
            f"gate answers: {clean.gate_answers}",
        )
    )

    # --- acceptance, first half: every action logged with inputs+outputs ---
    ok_records = [r for r in clean_audit.records() if r.outcome == "ok"]
    both = [r for r in ok_records if r.inputs and r.outputs]
    checks.append(
        _check(
            "every_action_logged_with_inputs_and_outputs",
            len(ok_records) == len(DEMO_STEPS) and len(both) == len(ok_records),
            f"{len(both)}/{len(ok_records)} ok record(s) carry both; "
            f"{len(DEMO_STEPS)} step(s) expected",
        )
    )
    has_intent_action = [
        r for r in clean_audit.records() if r.intent and r.action
    ]
    checks.append(
        _check(
            "every_record_carries_the_document_triple",
            len(has_intent_action) == len(clean_audit.records()),
            "audit.log(step.intent, step.action, step.result): intent and action "
            f"present on {len(has_intent_action)}/{len(clean_audit.records())} record(s)",
        )
    )
    with_message_only = [
        r
        for r in clean_audit.records()
        if not r.inputs or not r.outputs
    ]
    checks.append(
        _check(
            "the_log_is_not_merely_messages",
            not with_message_only,
            "no record is a bare message; inputs and outputs are required fields",
        )
    )

    # --- acceptance, second half: crashed run resumes ----------------------
    checks.append(
        _check(
            "crash_really_killed_the_process",
            proof.crashed,
            f"child exit code {proof.crash_exit_code} (os._exit(9) expected)",
        )
    )
    checks.append(
        _check(
            "resume_runs_only_the_unfinished_step",
            proof.resumed_steps == (CRASH_STEP,),
            f"resumed ran {proof.resumed_steps}; skipped {proof.skipped}",
        )
    )
    checks.append(
        _check(
            "resume_does_not_repeat_the_risky_publish",
            not proof.publish_reran,
            f"publish executed in the resumed process: {proof.publish_reran}",
        )
    )
    checks.append(
        _check(
            "resume_does_not_re_ask_the_gate",
            not proof.gate_asked_again,
            f"gate consulted again on resume: {proof.gate_asked_again}",
        )
    )
    checks.append(
        _check(
            "audit_chain_survives_the_crash",
            proof.audit_verified and proof.audit_records == len(DEMO_STEPS),
            f"{proof.audit_records} record(s) across both processes, chain "
            f"verified={proof.audit_verified}, fingerprint {proof.fingerprint}",
        )
    )

    # --- budget ----------------------------------------------------------
    step_snap = {s.dimension: s for s in budget.budget}.get("steps")
    checks.append(
        _check(
            "budget_is_enforced_before_the_action",
            budget.status == "stopped"
            and step_snap is not None
            and step_snap.spent == 3
            and step_snap.refused == 1
            and budget.escalated,
            f"status={budget.status}, steps spent={step_snap.spent if step_snap else '?'}"
            f"/{step_snap.limit if step_snap else '?'}, refusals="
            f"{step_snap.refused if step_snap else '?'}, escalated={budget.escalated}",
        )
    )
    budget_step_names = budget.steps_run()
    checks.append(
        _check(
            "budget_refusal_stops_before_the_over_limit_step",
            len(budget_step_names) == 3
            and "publish" not in budget_step_names
            and "report" not in budget_step_names,
            f"budgeted run executed {budget_step_names}",
        )
    )

    # --- gate ------------------------------------------------------------
    gate_publish = gate_audit.for_step("publish")
    gate_refused_record = gate_publish[-1] if gate_publish else None
    checks.append(
        _check(
            "gate_is_a_hard_stop",
            gate.status == "stopped"
            and "publish" not in gate.steps_run()
            and gate.outputs_of("publish") == {},
            f"status={gate.status}, ran={gate.steps_run()}, publish outputs="
            f"{gate.outputs_of('publish')}",
        )
    )
    checks.append(
        _check(
            "refusal_is_logged_with_who_and_what",
            gate_refused_record is not None
            and gate_refused_record.outcome in {"refused", "deferred"}
            and gate_refused_record.decided_by == HUMAN_AUTHORITY
            and bool(gate_refused_record.inputs),
            (
                f"record outcome={gate_refused_record.outcome}, "
                f"decided_by={gate_refused_record.decided_by}, inputs recorded="
                f"{bool(gate_refused_record.inputs)}"
            )
            if gate_refused_record
            else "no audit record for the publish step",
        )
    )
    # The pitfall's second clause: a risky step is checkpointed *before* the gate
    # is consulted. Observable from the audit log -- the record for a gated step
    # carries the id of the checkpoint that was written before it ran, which is
    # only true if the write happened first.
    gated_records = [r for r in clean_audit.records() if r.risky]
    checks.append(
        _check(
            "checkpoint_written_before_the_risky_step",
            bool(gated_records)
            and all(r.checkpoint_id for r in gated_records)
            and clean.checkpoints >= len(DEMO_STEPS),
            (
                f"{len(gated_records)} gated record(s), each naming the "
                f"pre-step checkpoint: "
                f"{[r.checkpoint_id for r in gated_records]}; "
                f"{clean.checkpoints} checkpoint(s) written by the run"
            ),
        )
    )

    # --- audit integrity -------------------------------------------------
    try:
        verified = clean_audit.verify() == len(clean_audit.records())
    except Exception:  # noqa: BLE001
        verified = False
    checks.append(
        _check(
            "audit_chain_verifies",
            verified,
            f"fingerprint {clean_audit.fingerprint()} over "
            f"{len(clean_audit.records())} record(s)",
        )
    )
    return tuple(checks)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_lab06(root: Path | str | None = None) -> LabRunResult:
    """Run every Lab 6 demonstration and return the self-checking result.

    Args:
        root: Where evidence is written. A temporary directory is created and
            removed if not given.

    Returns:
        The result. ``pass_all`` is the single value to branch on.

    Note:
        Evidence lands in a per-invocation subdirectory of ``root``, named for
        this run. Running the lab twice into one directory is a thing people do
        -- the notebook does it deliberately, to show the run is idempotent --
        and writing both runs' audit logs to the same file would either fail the
        chain check or, worse, merge two runs into one log. The resume key is
        per-thread and unique, so two runs must not share it; giving each run its
        own directory is the simplest way to guarantee that.
    """
    import tempfile

    temporary = root is None
    base = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="lab06_"))
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / f"run-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. the uninterrupted governed run
    clean_gate = PreapprovedGate(DEFAULT_GATE, by=HUMAN_AUTHORITY, reason="demo approval")
    _, clean, clean_audit, _ = governed_run(
        run_dir,
        thread="clean",
        run_id="lab06-clean",
        budget=Budget.defaults(),
        gate=clean_gate,
    )

    # 2. the gate refusal
    gate_report, gate_audit = demo_gate_refusal(run_dir)

    # 3. the budget refusal
    budget_report, budget_audit = demo_budget(run_dir)

    # 4. the crash and resume
    proof = crash_and_resume(run_dir)

    checks = acceptance_checks(
        clean,
        gate_report,
        budget_report,
        proof,
        clean_audit=clean_audit,
        gate_audit=gate_audit,
        budget_audit=budget_audit,
    )
    passed = all(c["passed"] for c in checks)
    notes = (
        (
            f"clean run fingerprint {clean_audit.fingerprint()} over "
            f"{clean.audit_records} record(s)"
        ),
        (
            f"crash/resume fingerprint {proof.fingerprint} over "
            f"{proof.audit_records} record(s)"
        ),
        f"acceptance: {sum(1 for c in checks if c['passed'])}/{len(checks)} checks passed",
    )
    return LabRunResult(
        lab=LAB,
        pass_all=passed,
        checks=checks,
        clean=clean,
        gate=gate_report,
        budget=budget_report,
        proof=proof,
        root=str(run_dir),
        notes=notes,
    )


def _main(argv: Sequence[str] | None = None) -> int:
    """Print the Lab 6 report and return 0 on pass, 1 on fail.

    Exit code is the point: the lab is verified by running it, and a lab that
    prints PASS while exiting non-zero is worse than one that fails loudly.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    root = args[0] if args else None

    result = run_lab06(root)

    print(f"{LAB}: {DOC_GOAL}")
    print(f"Deliverable: {DOC_DELIVERABLE}")
    print(f"Accept: {DOC_ACCEPTANCE}")
    print(f"Pitfall: {DOC_PITFALL}")
    print("=" * 78)

    print("\n-- Pseudocode the runtime implements (doc order vs corrected order) --")
    print("DOC:")
    for line in DOC_PSEUDOCODE.splitlines():
        print("   " + line)
    print("RUNTIME (pitfall applied):")
    for i, line in enumerate(RUNTIME_ORDER, 1):
        print(f"   {i}. {line}")

    print("\n-- Uninterrupted governed run --")
    c = result.clean
    assert c is not None
    print(f"  status      : {c.status}")
    print(f"  steps       : {c.steps_run()}")
    print(f"  gate answers: {c.gate_answers}")
    print(f"  audit       : {c.audit_records} record(s), fingerprint {c.audit_fingerprint}")
    print(f"  checkpoints : {c.checkpoints}")
    print("  budget      :")
    for snap in c.budget:
        print(f"     {snap.dimension:<18} {snap.rendered():>10}  refused={snap.refused}")

    print("\n-- Gate refusal (publish is a hard stop) --")
    g = result.gate
    assert g is not None
    print(f"  status      : {g.status}")
    print(f"  steps       : {g.steps_run()}")
    print(f"  publish ran : {'publish' in g.steps_run()}")
    print(f"  gate answer : {g.gate_answers}")

    print("\n-- Budget refusal (steps=3) --")
    b = result.budget
    assert b is not None
    print(f"  status      : {b.status}")
    print(f"  steps       : {b.steps_run()}")
    print(f"  escalated   : {b.escalated}")

    print("\n-- Crash and resume (two real processes) --")
    p = result.proof
    assert p is not None
    for line in p.rendered().splitlines():
        print("  " + line)

    print("\n-- Acceptance checks --")
    for i, c in enumerate(checks_display := list(result.checks), 1):
        mark = "PASS" if c["passed"] else "FAIL"
        print(f"  [{mark}] {i:>2}. {c['name']}")
        print(f"         {c['detail']}")

    print("\n" + "=" * 78)
    verdict = "OVERALL: PASS" if result.pass_all else "OVERALL: FAIL"
    print(
        f"{verdict} ({sum(1 for c in checks_display if c['passed'])}"
        f"/{len(checks_display)} checks)"
    )
    return 0 if result.pass_all else 1


if __name__ == "__main__":
    raise SystemExit(_main())