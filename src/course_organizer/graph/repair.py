"""Lab 10: self-healing validation — typed findings and targeted repair.

The graph already loops ``validate --fail--> workers`` under ``MAX_ATTEMPTS``
and escalates on exhaustion. What it lacked was *targeting*: every retry
rebuilt everything, and nothing named which item broke which check. This
module adds exactly that, and nothing else:

* :class:`Finding` — the failing check, the affected submission ids, the
  rule, the diagnosis, whether any safe automatic repair exists, and which
  specialist steps would regenerate the item.
* Deterministic, judgment-free repairs (:func:`sync_grade_metadata`) that
  re-derive flags from store values. They never touch marks, feedback,
  status, or timestamps.
* :func:`splice_grades` — regenerated rows replace only failing ids; passing
  rows are carried verbatim, so valid work survives a retry provably.
* :func:`steps_for` — the union of repair steps in roster order, for the
  existing ``run_specialists(only=[...])`` seam. No second pipeline.

What is deliberately NOT auto-repaired: rubric breaches beyond a re-draft
(regeneration handles those), unapproved publication (only a human decision
repairs that — no worker may grant faculty approval), and unidentifiable
items (no guessing; escalate). When no finding has a safe repair, the run
escalates immediately instead of burning retries — see ``unrepairable`` in
:func:`~course_organizer.graph.validation.next_route`.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

#: Repair step ids must follow the specialist roster order (w1..w6); a test
#: asserts this against SPECIALISTS so the two cannot drift.
ROSTER_ORDER: tuple[str, ...] = ("w1", "w2", "w3", "w4", "w5", "w6")

#: Which specialist steps regenerate an item failing each check. ``[]`` means
#: no worker can repair it — the finding escalates. Approval is absent on
#: purpose: granting faculty approval is never a worker's job.
CHECK_REPAIR_STEPS: dict[str, tuple[str, ...]] = {
    "draft_grade_within_rubric_maximum": ("w5",),
    "late_submissions_flagged_accurately": ("w3",),
    "similarity_score_attached": ("w4",),
    "no_grade_published_without_faculty_approval": (),
    "evidence_grounded": ("w5",),
}

#: One-line rule quotes for findings. The sentences are the doc's, shortened;
#: the full diagnosis comes from the check that failed.
CHECK_RULES: dict[str, str] = {
    "draft_grade_within_rubric_maximum":
        "a draft grade never exceeds the rubric maximum (doc 2.7)",
    "late_submissions_flagged_accurately":
        "late submissions are flagged accurately; timestamps are never altered (doc 2.4/2.7)",
    "similarity_score_attached":
        "a plagiarism/similarity score is attached; flags never decide (doc 2.4/2.7)",
    "no_grade_published_without_faculty_approval":
        "no grade is published without faculty approval (doc 2.12)",
    "evidence_grounded":
        "every candidate is grounded in faculty-approved sources (Lab 9)",
}


class Finding(BaseModel):
    """One failing check, located at the affected items or honestly lost."""

    model_config = {"extra": "forbid"}

    check: str
    submission_ids: list[str] = Field(default_factory=list)
    rule: str = ""
    diagnosis: str = ""
    repairable: Literal["regenerate", "deterministic", "none"] = "none"
    repair_steps: list[str] = Field(default_factory=list)
    repair_target: Literal["grade", "late_flags", "similarity", "rubric", "gate", "unknown"] = "unknown"


def _grade_dicts(grades: Any) -> list[dict[str, Any]]:
    out = []
    for g in grades or []:
        if isinstance(g, Mapping):
            out.append(dict(g))
        elif hasattr(g, "model_dump"):
            out.append(g.model_dump())
        else:
            out.append({})
    return out


def _ceilings_of(grade: Mapping[str, Any], ceilings: Mapping[str, float]) -> float:
    aid = str(grade.get("assignment_id", ""))
    if aid in ceilings:
        return float(ceilings[aid])
    try:
        return float(grade.get("max_marks", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def diagnose(
    failing: Sequence[str],
    *,
    grades: Any,
    ceilings: Mapping[str, float],
    late_by: Mapping[str, int],
    tables: Optional[Mapping[str, Any]] = None,
) -> list[Finding]:
    """Turn failing check names into located findings. Pure and total.

    Never guesses: when the affected item cannot be identified reliably the
    finding carries empty ids and ``repairable="none"`` so the run escalates
    instead of mutating data on a hunch.
    """
    rows = _grade_dicts(grades)
    by_id = {str(g.get("submission_id", "")): g for g in rows if g.get("submission_id")}
    tables = tables or {}
    out: list[Finding] = []
    for name in failing:
        if name == "draft_grade_within_rubric_maximum":
            ids = [sid for sid, g in by_id.items()
                   if _marks_of(g) > _ceilings_of(g, ceilings) >= 0
                   and _ceilings_of(g, ceilings) > 0]
            # A breach with no identifiable row (e.g. empty grades) escalates.
            out.append(Finding(
                check=name, submission_ids=ids, rule=CHECK_RULES[name],
                diagnosis=(f"{len(ids)} grade(s) above their rubric maximum" if ids
                           else "check failed with no grade rows to locate"),
                repairable="regenerate" if ids else "none",
                repair_steps=list(CHECK_REPAIR_STEPS[name]) if ids else [],
                repair_target="grade" if ids else "unknown"))
        elif name == "late_submissions_flagged_accurately":
            mismatched = [sid for sid, sec in late_by.items()
                          if sid in by_id and bool(by_id[sid].get("late", False)) != (int(sec) > 0)]
            missing = [sid for sid in late_by if sid not in by_id]
            ids = sorted(set(mismatched) | set(missing))
            if not ids:
                out.append(Finding(
                    check=name, submission_ids=[], rule=CHECK_RULES[name],
                    diagnosis="check failed with no locatable late rows; escalating",
                    repairable="none", repair_steps=[], repair_target="unknown"))
            else:
                out.append(Finding(
                    check=name, submission_ids=ids, rule=CHECK_RULES[name],
                    diagnosis=(f"{len(mismatched)} flag(s) disagree with the store"
                               + (f"; {len(missing)} store submission(s) have no grade row" if missing else "")),
                    repairable="deterministic" if mismatched else "none",
                    repair_steps=list(CHECK_REPAIR_STEPS[name]),
                    repair_target="late_flags"))
        elif name == "similarity_score_attached":
            ids = [sid for sid, g in by_id.items() if g.get("similarity_score") is None]
            out.append(Finding(
                check=name, submission_ids=ids, rule=CHECK_RULES[name],
                diagnosis=(f"{len(ids)} grade(s) carry no similarity score" if ids
                           else "check failed with no grade rows to locate"),
                repairable="regenerate" if ids else "none",
                repair_steps=list(CHECK_REPAIR_STEPS[name]) if ids else [],
                repair_target="similarity" if ids else "unknown"))
        elif name == "no_grade_published_without_faculty_approval":
            ids = [sid for sid, g in by_id.items()
                   if str(g.get("status", "draft")) != "draft" and not g.get("approved_by")]
            out.append(Finding(
                check=name, submission_ids=ids, rule=CHECK_RULES[name],
                diagnosis=(f"{len(ids)} published grade(s) name no approver; only a human "
                           f"decision repairs this" if ids else
                           "approval check failed with no locatable rows"),
                repairable="none", repair_steps=[], repair_target="gate"))
        elif name == "evidence_grounded":
            bad: list[str] = []
            for sid in by_id:
                raw = tables.get(sid)
                if raw is None:
                    bad.append(sid)
                    continue
                supported = raw.get("supported") if isinstance(raw, Mapping) else getattr(raw, "supported", None)
                if supported is not True:
                    bad.append(sid)
            out.append(Finding(
                check=name, submission_ids=sorted(bad), rule=CHECK_RULES[name],
                diagnosis=(f"{len(bad)} candidate(s) ungrounded" if bad
                           else "evidence check failed with no locatable rows"),
                repairable="regenerate" if bad else "none",
                repair_steps=list(CHECK_REPAIR_STEPS[name]) if bad else [],
                repair_target="grade" if bad else "unknown"))
        else:
            out.append(Finding(
                check=name, submission_ids=[], rule="unknown check",
                diagnosis=f"unrecognized check {name!r}; escalating rather than guessing",
                repairable="none", repair_steps=[], repair_target="unknown"))
    return out


def _marks_of(grade: Mapping[str, Any]) -> float:
    try:
        return float(grade.get("awarded_marks", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def sync_grade_metadata(
    grades: list[dict[str, Any]],
    *,
    scores: Mapping[str, float],
    against: Mapping[str, str],
    flagged: set[str] | frozenset[str],
    late_by: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-derive flags/metadata on carried rows. Pure: inputs never mutated.

    Sets only ``late``, ``similarity_score``, ``similarity_against`` and
    ``similarity_flagged`` from authoritative maps. Marks, feedback, status
    and (elsewhere) timestamps are never touched — a metadata sync that moved
    a mark would be a grade, not a repair.

    Returns ``(new_rows, repaired_ids)``.
    """
    flagged_set = set(flagged or ())
    synced: list[dict[str, Any]] = []
    repaired: list[str] = []
    for g in grades:
        row = dict(g)
        sid = str(row.get("submission_id", ""))
        changed = False
        if sid in late_by:
            want_late = int(late_by[sid]) > 0
            if bool(row.get("late", False)) != want_late:
                row["late"] = want_late
                changed = True
        if sid in scores and row.get("similarity_score") is None:
            row["similarity_score"] = float(scores[sid])
            changed = True
        if sid in against and not row.get("similarity_against"):
            row["similarity_against"] = str(against[sid])
            changed = True
        if sid in flagged_set and not row.get("similarity_flagged"):
            row["similarity_flagged"] = True
            changed = True
        if changed and sid:
            repaired.append(sid)
        synced.append(row)
    return synced, repaired


def splice_grades(
    carried: list[dict[str, Any]],
    regenerated: list[dict[str, Any]],
    failing_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Replace only failing rows with regenerated ones. Pure.

    Passing rows are carried verbatim (proven by equality tests); regenerated
    rows for previously-missing ids are added. Order follows the carried list,
    additions appended — order is presentation, never semantics.
    """
    failing = set(failing_ids)
    regen_by_id = {str(g.get("submission_id", "")): dict(g) for g in regenerated}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in carried:
        sid = str(g.get("submission_id", ""))
        if sid in failing and sid in regen_by_id:
            out.append(regen_by_id[sid])
        else:
            out.append(dict(g))
        seen.add(sid)
    for sid in failing:
        if sid not in seen and sid in regen_by_id:
            out.append(regen_by_id[sid])
    return out


def steps_for(findings: Sequence[Finding]) -> list[str]:
    """Union of repair steps in roster order. Empty means: nothing any worker
    may regenerate — the run must escalate, not retry blindly."""
    wanted: set[str] = set()
    for f in findings:
        wanted.update(f.repair_steps)
    return [s for s in ROSTER_ORDER if s in wanted]


def auto_handled(findings: Sequence[Finding]) -> bool:
    """True when at least one finding has any safe automatic repair."""
    return any(f.repairable != "none" for f in findings)
