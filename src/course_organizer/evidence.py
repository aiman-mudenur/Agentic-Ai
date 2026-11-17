"""Lab 9: grounded evidence for every candidate/output.

Each draft grade (or any candidate) carries an evidence table: one item per
claim, each citing a locator that resolves to a real, authorized, in-scope,
current source record with a supporting excerpt quoted from that record.

Locator vocabulary (fixed — anything else is malformed, never guessed)::

    unit:<id>                          syllabus-backed unit facts (scope tool)
    note:<id>:version:<version>        a retained note version
    rubric:<assignment_id>:criterion:<index>   a rubric criterion by position
    submission:<id>                    a collected submission record

Rules, enforced in code rather than documented:

* A locator must parse, resolve to a record that exists, sit inside the run's
  course/unit scope, be current (notes), and be visible to the requester.
  Anything else is a rejected item with a named reason — never an exception
  out of the builder, so a bad candidate fails validation instead of crashing
  the pipeline.
* An excerpt must occur in its source's text. A similarity score is not a
  source kind at all, so "similarity proves copying" can never be grounded —
  the claim fails with its reason naming the missing support.
* Excerpts and reasons never carry another student's private text: an
  unauthorized submission resolves to "inaccessible" with no excerpt.
* This module does no IO. Callers supply a :class:`SourceIndex` built from
  Lab 4 retrieval / Lab 5 connector reads (in the graph: the tool-verified
  state contents). Nothing here duplicates those systems.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, Field

#: Source types an evidence item may cite. There is deliberately no
#: "similarity" kind: a score is metadata about a comparison, not a source,
#: so a claim backed only by a score is ungroundable by construction.
SourceKind = Literal["syllabus", "rubric", "note", "submission"]

_LOCATOR_RES = {
    "syllabus": re.compile(r"^unit:(?P<id>[A-Za-z0-9_.\-]+)$"),
    "note": re.compile(r"^note:(?P<id>[A-Za-z0-9_.\-]+):version:(?P<version>[0-9]+)$"),
    "rubric": re.compile(r"^rubric:(?P<aid>[A-Za-z0-9_.\-]+):criterion:(?P<index>[0-9]+)$"),
    "submission": re.compile(r"^submission:(?P<id>[A-Za-z0-9_.\-]+)$"),
}

#: Which locator form each source type uses. ``syllabus`` facts are addressed
#: per unit: the units a run may claim come from the scope tool, which checks
#: them against the faculty-approved syllabus — so a ``unit:`` locator *is* the
#: syllabus citation in this corpus, not a second invented vocabulary.
_KIND_FORMS = {"syllabus": ("unit",), "note": ("note",), "rubric": ("rubric",),
               "submission": ("submission",)}


class ParsedLocator(BaseModel):
    """A locator that parsed. Parsing never resolves: syntax first, store second."""

    model_config = {"frozen": True}

    kind: str
    target_id: str
    version: Optional[int] = None
    index: Optional[int] = None
    raw: str


def parse_locator(raw: str) -> ParsedLocator:
    """Parse a locator or raise ``ValueError`` naming why it is malformed.

    Args:
        raw: The locator string.

    Returns:
        The parsed locator.

    Raises:
        ValueError: If no vocabulary form matches. :func:`build_evidence_table`
            converts this into an ``unsupported`` item rather than raising, so
            the pipeline fails validation instead of crashing.
    """
    text = (raw or "").strip()
    for kind, pattern in _LOCATOR_RES.items():
        match = pattern.match(text)
        if match:
            groups = match.groupdict()
            return ParsedLocator(
                kind=kind,
                target_id=groups.get("id") or groups.get("aid") or "",
                version=int(groups["version"]) if groups.get("version") else None,
                index=int(groups["index"]) if groups.get("index") else None,
                raw=text,
            )
    raise ValueError(
        f"malformed locator {raw!r}: expected one of "
        "unit:<id>, note:<id>:version:<v>, rubric:<aid>:criterion:<i>, submission:<id>"
    )


class SourceIndex(BaseModel):
    """The authorized sources a table may cite, as plain data.

    Built by callers from Lab 4 retrieval / Lab 5 connector reads — in the
    graph, from the tool-verified state contents. Keys are real ids; texts are
    real record contents, never summaries written for the occasion.
    """

    model_config = {"extra": "forbid"}

    #: unit_id -> {"course_code": str, "note_count": int}; units come from the
    #: scope tool's syllabus check, so they are the syllabus-backed records.
    units: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: note_id -> {"course_code", "unit", "current_version": int,
    #:             "versions": {version: text}, "hashes": {version: sha256 hex (optional)}}
    notes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: assignment_id -> {"course_code", "criteria": [{"description", "max_marks"}]}
    rubrics: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: submission_id -> {"student_id", "course_code", "unit", "status", "submitted_at"}
    submissions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Requester(BaseModel):
    """Who the evidence is resolved for. Students see only their own submissions."""

    model_config = {"extra": "forbid"}

    role: Literal["faculty", "student", "hod"] = "faculty"
    student_id: str = ""


class ClaimEvidence(BaseModel):
    """One claim plus the locator and excerpt offered in support."""

    model_config = {"extra": "forbid"}

    candidate_id: str
    claim: str
    locator: str
    excerpt: str = ""


class ItemVerdict(BaseModel):
    model_config = {"extra": "forbid"}

    supported: bool
    reason: str


class EvidenceItem(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_id: str
    claim: str
    source_type: str
    locator: str
    excerpt: str = ""
    source_version: Optional[str] = None
    content_hash: Optional[str] = None
    verdict: ItemVerdict


class CandidateEvidence(BaseModel):
    """The evidence table for one candidate. Supported iff every item is."""

    model_config = {"extra": "forbid"}

    candidate_id: str
    course_code: str
    items: list[EvidenceItem] = Field(default_factory=list)

    @property
    def supported(self) -> bool:
        return bool(self.items) and all(it.verdict.supported for it in self.items)

    def reasons(self) -> list[str]:
        return [it.verdict.reason for it in self.items if not it.verdict.supported]


def content_hash_of(text: str) -> str:
    """sha256 hex of a text. Recorded only for hashes that arrived *with* a
    source — never minted here and presented as provenance."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_text(kind: str, record: Mapping[str, Any], target_id: str) -> str:
    """The exact source text an excerpt is checked against.

    Notes and rubric criteria quote record contents. Units and submissions
    have no body text in the store, so their canonical text is the factual
    rendering of the record fields — defined once here so the builder and the
    graph wiring cannot disagree about what counts as quoted.
    """
    if kind == "syllabus":
        return (
            f"unit {target_id} in {record.get('course_code', '')} with "
            f"{int(record.get('note_count', 0) or 0)} retained note(s)"
        )
    if kind == "submission":
        return (
            f"submission {target_id} by student {record.get('student_id', '')} "
            f"status {record.get('status', '')} submitted_at {record.get('submitted_at', '')}"
        )
    return str(record.get("text", ""))


def _resolve(parsed: ParsedLocator, index: SourceIndex
             ) -> tuple[Optional[dict[str, Any]], str, Optional[str], Optional[str]]:
    """Resolve a parsed locator.

    Returns ``(record, source_text, version, error)`` — exactly one of record
    / error is set. Four positions because "which version" and "why not" are
    different answers sharing no representation.
    """
    kind, tid = parsed.kind, parsed.target_id
    if kind == "syllabus":
        rec = index.units.get(tid)
        if rec is None:
            return None, "", None, f"unknown unit {tid!r}: not in the syllabus-verified scope"
        return dict(rec), _canonical_text(kind, rec, tid), None, None
    if kind == "note":
        rec = index.notes.get(tid)
        if rec is None:
            return None, "", None, f"unknown note {tid!r}: no such source"
        versions = rec.get("versions") or {}
        if parsed.version not in versions:
            return None, "", None, (
                f"invalid reference note:{tid}:version:{parsed.version}: "
                "that version was never retained"
            )
        text = versions[parsed.version]
        return dict(rec), str(text), str(parsed.version), None
    if kind == "rubric":
        rec = index.rubrics.get(tid)
        if rec is None:
            return None, "", None, f"unknown assignment {tid!r}: no approved rubric"
        criteria = rec.get("criteria") or []
        if parsed.index is None or not 0 <= parsed.index < len(criteria):
            return None, "", None, (
                f"invalid reference rubric:{tid}:criterion:{parsed.index}: "
                f"rubric has {len(criteria)} criterion/criteria"
            )
        return dict(rec), str(criteria[parsed.index].get("description", "")), None, None
    # submission
    rec = index.submissions.get(tid)
    if rec is None:
        return None, "", None, f"unknown submission {tid!r}: not collected from the store"
    return dict(rec), _canonical_text(kind, rec, tid), None, None


def _record_course(kind: str, record: Mapping[str, Any]) -> str:
    return str(record.get("course_code", "") or "")


def _record_unit(kind: str, record: Mapping[str, Any]) -> str:
    if kind == "syllabus":
        return ""  # the unit IS the address; scope-checked against unit_scope directly
    return str(record.get("unit", "") or "")


def build_evidence_table(
    candidate_id: str,
    claims: list[ClaimEvidence],
    index: SourceIndex,
    *,
    requester: Requester,
    course_scope: str,
    unit_scope: Optional[list[str]] = None,
    require_current: bool = True,
) -> CandidateEvidence:
    """Build (never raise for) the evidence table for one candidate.

    Every item is adjudicated: malformed locators, missing/invalid references,
    out-of-scope records, stale versions, inaccessible submissions, and
    excerpts that do not occur in their source each produce an ``unsupported``
    item naming the reason. The table is supported iff every item is.

    Args:
        candidate_id: The candidate/output under test (a submission id here).
        claims: The claims with offered locators and excerpts.
        index: Authorized sources, built from connector/retrieval reads.
        requester: Whose eyes resolve submission records (privacy boundary).
        course_scope: The run's course; records from another course are out
            of scope even when they exist.
        unit_scope: When non-empty, records outside it are out of scope
            (the narrowing test). Empty/None means no unit constraint.
        require_current: Reject superseded note versions as stale.

    Returns:
        The table. Never raises for bad input — bad input is the verdict.
    """
    table = CandidateEvidence(candidate_id=candidate_id, course_code=course_scope)
    scope_units = set(unit_scope or [])
    for claim_ev in claims:
        try:
            parsed = parse_locator(claim_ev.locator)
        except ValueError as exc:
            table.items.append(EvidenceItem(
                candidate_id=candidate_id, claim=claim_ev.claim, source_type="unknown",
                locator=claim_ev.locator, excerpt=claim_ev.excerpt,
                verdict=ItemVerdict(supported=False, reason=str(exc))))
            continue
        record, source_text, version, error = _resolve(parsed, index)
        if record is None:
            table.items.append(EvidenceItem(
                candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                locator=claim_ev.locator, excerpt=claim_ev.excerpt,
                verdict=ItemVerdict(supported=False, reason=error or "unresolvable")))
            continue
        if _record_course(parsed.kind, record) != course_scope:
            table.items.append(EvidenceItem(
                candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                locator=claim_ev.locator, excerpt=claim_ev.excerpt, source_version=version,
                verdict=ItemVerdict(
                    supported=False,
                    reason=f"out of scope: {parsed.kind} {parsed.target_id!r} belongs to "
                           f"{_record_course(parsed.kind, record)!r}, run scope is {course_scope!r}")))
            continue
        if scope_units:
            unit_id = parsed.target_id if parsed.kind == "syllabus" else _record_unit(parsed.kind, record)
            if unit_id not in scope_units:
                table.items.append(EvidenceItem(
                    candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                    locator=claim_ev.locator, excerpt=claim_ev.excerpt, source_version=version,
                    verdict=ItemVerdict(
                        supported=False,
                        reason=f"out of scope: unit {unit_id!r} is outside the locked scope "
                               f"{sorted(scope_units)}")))
                continue
        if parsed.kind == "note" and require_current:
            current = (record.get("current_version")
                       if isinstance(record.get("current_version"), int) else None)
            if current is not None and parsed.version != current:
                table.items.append(EvidenceItem(
                    candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                    locator=claim_ev.locator, excerpt=claim_ev.excerpt, source_version=version,
                    verdict=ItemVerdict(
                        supported=False,
                        reason=f"stale version: note {parsed.target_id!r} is at version "
                               f"{current}, evidence cites version {parsed.version}")))
                continue
        if parsed.kind == "submission":
            owner = str(record.get("student_id", "") or "")
            if requester.role == "student" and requester.student_id != owner:
                # Privacy: no excerpt, no owner text — inaccessibility only.
                table.items.append(EvidenceItem(
                    candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                    locator=claim_ev.locator, excerpt="",
                    verdict=ItemVerdict(
                        supported=False,
                        reason=f"inaccessible: submission {parsed.target_id!r} belongs to "
                               "another student")))
                continue
        excerpt = (claim_ev.excerpt or "").strip()
        if not excerpt or excerpt.lower() not in source_text.lower():
            table.items.append(EvidenceItem(
                candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
                locator=claim_ev.locator, excerpt=claim_ev.excerpt, source_version=version,
                verdict=ItemVerdict(
                    supported=False,
                    reason=f"unsupported: excerpt does not occur in {parsed.kind} "
                           f"{parsed.target_id!r}; a score or paraphrase is not evidence")))
            continue
        content_hash = None
        if parsed.kind == "note":
            hashes = record.get("hashes") or {}
            content_hash = hashes.get(parsed.version)
        table.items.append(EvidenceItem(
            candidate_id=candidate_id, claim=claim_ev.claim, source_type=parsed.kind,
            locator=claim_ev.locator, excerpt=claim_ev.excerpt, source_version=version,
            content_hash=content_hash,
            verdict=ItemVerdict(supported=True, reason="grounded")))
    if not claims:
        table.items.append(EvidenceItem(
            candidate_id=candidate_id, claim="(no claims offered)", source_type="none",
            locator="", excerpt="",
            verdict=ItemVerdict(supported=False, reason="missing evidence: no claims offered")))
    return table


def build_grade_evidence(
    grade: Mapping[str, Any],
    index: SourceIndex,
    *,
    course_scope: str,
    unit_scope: Optional[list[str]] = None,
) -> CandidateEvidence:
    """Evidence for one draft grade, from records the graph already holds.

    Claims, all factual and resolvable from the index (nothing invented):

    * the submission was collected (``submission:<id>``),
    * each rubric criterion carries its maximum (``rubric:<aid>:criterion:<i>``,
      positional indices into the assignment's approved rubric list),
    * the assignment's unit is in scope with retained notes (``unit:<id>`` —
      the syllabus-backed fact).

    Runs under faculty authority (the coordinator's ``HUMAN_AUTHORITY``), so
    submission records resolve. Excerpts quote the records they cite.
    """
    sid = str(grade.get("submission_id", ""))
    aid = str(grade.get("assignment_id", ""))
    claims: list[ClaimEvidence] = []
    sub = index.submissions.get(sid)
    if sub is not None:
        excerpt = f"status {sub.get('status', '')}"
        claims.append(ClaimEvidence(
            candidate_id=sid,
            claim=f"submission {sid} collected with status {sub.get('status', '')}",
            locator=f"submission:{sid}", excerpt=excerpt))
    else:
        claims.append(ClaimEvidence(
            candidate_id=sid, claim=f"submission {sid} collected",
            locator=f"submission:{sid}", excerpt="collected"))
    rubric = (index.rubrics.get(aid) or {}).get("criteria") or []
    for i, criterion in enumerate(rubric):
        desc = str(criterion.get("description", ""))
        claims.append(ClaimEvidence(
            candidate_id=sid,
            claim=f"criterion {criterion.get('criterion_id', i)} carries max "
                  f"{criterion.get('max_marks', '?')}",
            locator=f"rubric:{aid}:criterion:{i}", excerpt=desc))
    assignment_units = {
        (index.rubrics.get(aid) or {}).get("unit", "")} if aid in index.rubrics else set()
    for unit in sorted(u for u in assignment_units if u):
        count = int((index.units.get(unit) or {}).get("note_count", 0) or 0)
        excerpt = f"unit {unit} in {course_scope} with {count} retained note(s)"
        claims.append(ClaimEvidence(
            candidate_id=sid, claim=excerpt,
            locator=f"unit:{unit}", excerpt=excerpt))
    return build_evidence_table(
        sid, claims, index,
        requester=Requester(role="faculty"),
        course_scope=course_scope, unit_scope=unit_scope)
