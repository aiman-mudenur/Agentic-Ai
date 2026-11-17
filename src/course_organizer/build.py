"""Lab 11: the tested build factory.

A build takes a validated, locked input plan plus the authorized course
context, executes the *existing* graph (never a duplicate pipeline), and
returns a typed verdict: ``ready_for_review``, ``blocked``, ``escalated`` or
``refused``. Only ``ready_for_review`` names a gate a faculty member may
answer; every other outcome cannot advance, by construction as well as by
status.

Three properties make the gate hard rather than decorative:

1. **Plan identity governs execution, not just the verdict.** The input plan
    is parsed, hash-verified and checked for contradiction *before anything
    runs*, then forwarded into the graph seed. The plan node re-verifies and
    adopts it before any worker exists, and distribution/collection filter to
    its scope. After the run, the field-by-field diff remains as defense in
    depth - a mismatch blocks, even when validation passed.
2. **Fail-closed acceptance.** ``ready_for_review`` requires the conjunction:
   valid locked untampered input plan, gate actually awaiting, all five
   ``DOC_CHECKS`` present and passing, evidence present for every graded
   candidate, plan identity verified, nothing published. Anything else is
   ``blocked``/``escalated``/``refused`` with the reason recorded.
3. **No resume path.** This module never imports ``resume_run`` (asserted by
   test): a passing build prepares a draft for a *separate* explicit faculty
   action. It cannot approve or publish, so a green build authorizes review
   and nothing further.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from course_organizer import HUMAN_AUTHORITY
from course_organizer.graph.runner import run_pipeline
from course_organizer.graph.state import DOC_CHECKS
from course_organizer.skills import LockedPlan
from course_organizer.skills.plan_skill import VOLATILE_FACT_KEYS

#: Where build runs live when the caller names no root. Sibling of the lab
#: run dirs, never inside them, so a build cannot be mistaken for a lab demo.
BUILD_ROOT_NAME = "lab11"

BuildStatus = Literal["ready_for_review", "blocked", "escalated", "refused"]


class BuildRefused(Exception):
    """The input plan was rejected before anything ran. ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BuildRequest(BaseModel):
    """What a build takes. The plan arrives locked; the build never locks one."""

    model_config = {"extra": "forbid"}

    locked_plan: dict[str, Any]
    course_code: str = Field(min_length=1)
    run_id: Optional[str] = None
    max_attempts: int = Field(default=3, ge=1)
    output_dir: Optional[str] = None
    #: Test-only fault injection, forwarded to the runner so acceptance can be
    #: exercised against failing runs. Never part of a real build request.
    fail_until_attempt: int = Field(default=0, ge=0)


class AcceptanceReport(BaseModel):
    """The acceptance verdict, every field checkable without re-running."""

    model_config = {"extra": "forbid"}

    run_id: str = ""
    plan_sha256: str = ""
    plan_identity_ok: bool = False
    plan_mismatch_detail: list[str] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    passed: bool = False
    evidence_present: bool = False
    gate_awaiting: bool = False
    published: bool = False
    audit_entries: int = 0
    attempts: int = 0
    scope_ok: bool = False


class BuildResult(BaseModel):
    """The typed build outcome. ``published`` is always None: builds never
    publish, so there is no field in which a publication could hide."""

    model_config = {"extra": "forbid"}

    status: BuildStatus
    reason: str = ""
    run_id: str = ""
    plan_sha256: str = ""
    artifact: dict[str, Any] = Field(default_factory=dict)
    acceptance_report: Optional[AcceptanceReport] = None
    failed_checks: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    gate_awaiting: bool = False
    can_advance: bool = False
    published: Optional[str] = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def verify_input_plan(data: Mapping[str, Any], course_code: str) -> LockedPlan:
    """Parse, authenticate and scope-check an input plan. Raises BuildRefused.

    Order matters: malformed first (cannot even read it), then tampered
    (reads but lies), then unlocked (no lock timestamp), then contradictory
    (locked for a different course). Each refusal names its cause.
    """
    if not isinstance(data, Mapping) or not dict(data):
        raise BuildRefused("malformed plan: not a mapping or empty")
    try:
        plan = LockedPlan(**dict(data))
    except Exception as exc:
        raise BuildRefused(f"malformed plan: {exc}") from exc
    try:
        plan.recompute_sha256()
    except Exception as exc:
        if getattr(exc, "code", "") == "plan_tampered":
            raise BuildRefused(f"tampered plan: {exc}") from exc
        raise BuildRefused(f"plan integrity check failed: {exc}") from exc
    if not (plan.locked_at or "").strip():
        raise BuildRefused("unlocked plan: no lock timestamp; lock it before building")
    if plan.human_authority != HUMAN_AUTHORITY:
        raise BuildRefused(
            f"unauthorized plan: approver {plan.human_authority!r} is not the trusted "
            f"authority {HUMAN_AUTHORITY!r}"
        )
    if plan.course_code != course_code:
        raise BuildRefused(
            f"contradictory plan: locked for course {plan.course_code!r}, "
            f"build requested for {course_code!r}"
        )
    return plan


def diff_plans(run_plan: Mapping[str, Any], input_plan: LockedPlan) -> list[str]:
    """Field-by-field differences between the run's locked plan and the input.

    Hash equality is checked by the caller; this names *what* differs when it
    does not match, so a mismatch is a diagnosis rather than a shrug. Order
    matters for ``units`` and ``assignment_ids`` (doc: reordered scope is a
    different plan); obligations compare as dumped content; volatile facts
    (clock readings) are excluded by the canonical set, not by hand.
    """
    run = dict(run_plan or {})
    expected = input_plan.model_dump(mode="json")
    diffs: list[str] = []
    if run.get("plan_sha256") != input_plan.plan_sha256:
        diffs.append(
            f"plan_sha256 differs: run {str(run.get('plan_sha256', ''))[:16]}... vs "
            f"input {input_plan.plan_sha256[:16]}..."
        )
    for field in ("course_code", "course_name", "human_authority", "include_milestones"):
        if run.get(field) != expected.get(field):
            diffs.append(f"{field} differs: run {run.get(field)!r} vs input {expected.get(field)!r}")
    if list(run.get("units", []) or []) != list(input_plan.units):
        diffs.append(
            f"units differ (order matters): run {list(run.get('units', []) or [])} vs "
            f"input {list(input_plan.units)}"
        )
    if list(run.get("assignment_ids", []) or []) != list(input_plan.assignment_ids):
        diffs.append("assignment_ids differ (order matters)")
    run_facts = {k: v for k, v in dict(run.get("verified_facts", {}) or {}).items()
                 if k not in VOLATILE_FACT_KEYS}
    in_facts = {k: v for k, v in dict(expected.get("verified_facts", {}) or {}).items()
                if k not in VOLATILE_FACT_KEYS}
    if run_facts != in_facts:
        diffs.append("verified_facts differ (excluding volatile clock readings)")
    if int(run.get("obligation_count", -1)) != int(input_plan.obligation_count):
        diffs.append("obligation_count differs")
    return diffs


def evaluate_acceptance(
    state: Mapping[str, Any],
    *,
    plan: LockedPlan,
    run_id: str,
    gate_awaiting: bool,
    published: bool,
) -> AcceptanceReport:
    """Decide acceptance from a finished run's state. Pure: no IO, no runs.

    Fail-closed on every incomplete shape: missing validation, a short check
    list, missing evidence, an unawaited gate, any publication, or a plan
    mismatch each independently block. Only the full conjunction passes.
    """
    report = AcceptanceReport(run_id=run_id, plan_sha256=plan.plan_sha256)
    validation = state.get("validation")
    if validation is None:
        return report
    if isinstance(validation, Mapping):
        checks_run = list(validation.get("checks_run", []) or [])
        failed = list(validation.get("failing", []) or [])
        passed = bool(validation.get("passed", False))
    else:
        checks_run = list(getattr(validation, "checks_run", []) or [])
        failed = list(getattr(validation, "failing", []) or [])
        passed = bool(getattr(validation, "passed", False))
    report.checks_run = [str(c) for c in checks_run]
    report.failed = [str(c) for c in failed]
    report.attempts = int(state.get("attempts", 0) or 0)
    report.audit_entries = len(state.get("audit_trail", []) or [])
    report.gate_awaiting = bool(gate_awaiting)
    report.published = bool(published)

    complete = report.checks_run == list(DOC_CHECKS)
    grades = state.get("draft_grades", []) or []
    tables = state.get("evidence_tables", {}) or {}
    if grades:
        sids = set()
        for g in grades:
            if isinstance(g, Mapping):
                sids.add(str(g.get("submission_id", "")))
            else:
                sids.add(str(getattr(g, "submission_id", "")))
        sids.discard("")
        report.evidence_present = bool(sids) and all(s in tables for s in sids)
    else:
        report.evidence_present = False

    mismatch = diff_plans(state.get("plan_locked", {}) or {}, plan)
    report.plan_mismatch_detail = mismatch
    report.plan_identity_ok = not mismatch
    report.scope_ok = (
        list(state.get("units_requested", []) or []) == list(plan.units)
        and list(state.get("scope_assignment_ids", []) or []) == list(plan.assignment_ids)
    )
    if not report.scope_ok:
        report.plan_mismatch_detail = list(report.plan_mismatch_detail) + [
            "executed scope differs from authorized scope"]

    report.passed = (
        passed
        and not failed
        and complete
        and report.evidence_present
        and gate_awaiting
        and not published
        and report.plan_identity_ok
        and report.scope_ok
    )
    return report


def run_build(request: BuildRequest, *, root: Optional[Path] = None) -> BuildResult:
    """Execute the existing graph for an approved locked plan and judge it.

    Never resumes, never approves, never publishes: a green result carries a
    gate another (human) action must answer. Refusals happen before any run
    directory exists.
    """
    try:
        plan = verify_input_plan(request.locked_plan, request.course_code)
    except BuildRefused as exc:
        return BuildResult(status="refused", reason=str(exc), run_id=request.run_id or "")

    run_id = request.run_id or f"lab11-{uuid.uuid4().hex[:8]}"
    base = Path(root) if root is not None else _repo_root() / "out" / BUILD_ROOT_NAME
    target = Path(request.output_dir) if request.output_dir else base / run_id
    authorized = plan.model_dump(mode="json")
    try:
        run = run_pipeline(
            course_code=request.course_code,
            run_id=run_id,
            max_attempts=request.max_attempts,
            root=base,
            output_dir=target,
            fail_until_attempt=request.fail_until_attempt,
            authorized_plan=authorized,
        )
    except ValueError as exc:
        if "authorized plan" in str(exc):
            return BuildResult(
                status="blocked", run_id=run_id, plan_sha256=plan.plan_sha256,
                reason=f"scope refused: {exc}",
                gate_awaiting=False, can_advance=False)
        return BuildResult(
            status="blocked", run_id=run_id, plan_sha256=plan.plan_sha256,
            reason=f"infrastructure: pipeline raised ValueError: {exc}",
            gate_awaiting=False, can_advance=False)
    except Exception as exc:
        return BuildResult(
            status="blocked", run_id=run_id, plan_sha256=plan.plan_sha256,
            reason=f"infrastructure: pipeline raised {type(exc).__name__}: {exc}",
            gate_awaiting=False, can_advance=False)
    try:
        state = dict(run.state or {})
        report = evaluate_acceptance(
            state, plan=plan, run_id=run_id,
            gate_awaiting=bool(run.awaiting_gate),
            published=bool(run.published))
        document = state.get("document", {}) or {}
        if not isinstance(document, Mapping):
            document = {}
        artifact = {
            "document_id": state.get("document_id", ""),
            "document_status": state.get("document_status", ""),
            "document_body": state.get("document_body", ""),
            "title": dict(document).get("title", ""),
        }
        evidence = {str(k): v for k, v in dict(state.get("evidence_tables", {}) or {}).items()}
        audit = [dict(e) for e in (state.get("audit_trail", []) or [])]
        if report.passed and report.gate_awaiting and not report.published:
            status: BuildStatus = "ready_for_review"
            reason = (
                f"5/5 checks passed in {report.attempts} attempt(s); plan identity "
                f"verified ({plan.plan_sha256[:12]}...); awaiting faculty review")
            advance = True
        elif run.status is not None and str(getattr(run.status, "value", run.status)) == "escalated":
            status = "escalated"
            reason = f"run escalated: {'; '.join(report.failed) or 'see validation report'}"
            advance = False
        else:
            status = "blocked"
            reason = "; ".join(report.failed) or "acceptance incomplete"
            if report.plan_mismatch_detail:
                reason += "; plan mismatch: " + "; ".join(report.plan_mismatch_detail)
            advance = False
        result = BuildResult(
            status=status, reason=reason, run_id=run_id, plan_sha256=plan.plan_sha256,
            artifact=artifact, acceptance_report=report, failed_checks=list(report.failed),
            evidence=evidence, audit_trail=audit, gate_awaiting=report.gate_awaiting,
            can_advance=advance, published=None)
        try:
            (target).mkdir(parents=True, exist_ok=True)
            (target / "build_report.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8")
        except Exception as exc:
            return BuildResult(
                status="blocked", run_id=run_id, plan_sha256=plan.plan_sha256,
                reason=f"infrastructure: could not record build report: {exc}",
                gate_awaiting=False, can_advance=False)
        return result
    except Exception as exc:
        return BuildResult(
            status="blocked", run_id=run_id, plan_sha256=plan.plan_sha256,
            reason=f"infrastructure: acceptance evaluation failed ({type(exc).__name__}: {exc})",
            gate_awaiting=False, can_advance=False)
