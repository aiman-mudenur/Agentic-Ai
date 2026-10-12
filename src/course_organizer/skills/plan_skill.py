"""The `plan` skill: expand parsed inputs into obligations, then lock them.

Document 2.10, Lab 3 pseudocode, verbatim:

    skill plan(inputs) -> Plan:  return expand(inputs).lock()

So this module implements exactly two steps in that order, and the split is
the whole design. `expand` produces a `PlanDraft` -- still editable, still
wrong until it is complete. `.lock()` produces a `LockedPlan` that cannot be
changed afterwards. Document 2.12 calls the result a "blueprint / plan" and
defines it as "The locked specification the pipeline obeys", which is why the
locked form has to be genuinely immutable rather than conventionally
immutable: an unlocked plan is a suggestion, and 2.12's risk table names the
failure mode this exists to prevent:

    Plan drift | individual results deviate from the locked plan
               | Lock the plan (Lab 3/7); validation rejects any deviation
                 before the human sees it.

Three design decisions carry most of the weight, and each was forced by
something that would otherwise have gone wrong quietly:

**Obligations, not prose.** The Lab 1 plan is an outline a person reads. You
cannot mechanically detect drift against an outline -- "deviates from the plan"
is a judgement call. So `expand` lowers the outline into `Obligation` objects,
each a typed, id'd statement the rest of the system can compare against
mechanically. Drift is then a set difference, not an opinion.

**Tuples, not lists.** Pydantic's `frozen=True` blocks attribute *rebinding*
but does nothing about `locked.steps.append(...)`. A "locked" plan built on
list fields is a plan anyone can still edit, and the failure is silent. Every
collection on `LockedPlan` is therefore a `tuple`, which Pydantic coerces on
validation and which has no mutating methods at all. See
`tests/test_lab03_skill_creation.py::test_a_locked_plan_cannot_be_edited`.

**A deep copy at lock time.** `lock()` serialises rather than aliasing. If the
locked plan shared its obligation objects with the draft, a later edit to the
draft would rewrite history through the lock -- the single most important bug
this design exists to make impossible.

Nothing here knows what a course is. The module contains no course code and no
course name; `PlanInputs` carries them as data. That is the document's pitfall
("hard-coding one case breaks reuse") and it is enforced by a test that parses
this file, not by reviewer diligence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from course_organizer.logging_setup import log_event
from course_organizer.skills.base import (
    SkillContext,
    SkillError,
    SkillRegistry,
    SkillSpec,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class PlanInputs(BaseModel):
    """Everything the `plan` skill is given. Document Input: "Parsed inputs".

    Every field is data. There is no field, and no default, that names a
    particular course -- that is what lets one skill serve two cases.

    Attributes:
        course_code: The course the plan is for. Data, not a constant.
        course_name: Display name for the same course.
        units: Units in scope. Order is preserved; it is the teaching order.
        assignment_ids: Assignments in scope. Empty is legitimate and means
            the plan explicitly covers none.
        include_milestones: Whether project milestone tracking is in scope.
        steps: Lab 1's outline steps, carried through so their acceptance
            criteria can become verification obligations.
        verified_facts: The numbers Lab 2's tools computed. Carried so that
            the plan can *require* that every reported number is one of
            these, which is how Lab 2's rule survives into the document.
        notes: Parameters that were set rather than asked about. Carried into
            the locked plan and rendered into the document, because a scope
            that narrowed without saying so is exactly the drift 2.12 names.
        as_of: Anchor date, so a plan is reproducible on a later day.
        human_authority: Who must approve. 2.12's hard stop.
    """

    model_config = ConfigDict(extra="forbid")

    course_code: str = Field(min_length=1)
    course_name: str = Field(min_length=1)
    units: tuple[str, ...] = Field(min_length=1)
    assignment_ids: tuple[str, ...] = ()
    include_milestones: bool = True
    steps: tuple["LabStep", ...] = ()
    verified_facts: Mapping[str, Any] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
    as_of: Optional[str] = None
    human_authority: str = "course_faculty"


class LabStep(BaseModel):
    """A Lab 1 outline step, reduced to the fields a plan needs.

    This shape is declared here rather than importing `PlanStep` from
    `course_organizer.schemas`, and the reason is worth stating because it
    looks like duplication: it is *not* the same model. `schemas.PlanStep` is
    the Lab 1 artefact, which carries Lab 1 concerns (priority, dependency
    edges, a list `depends_on`). A skill's input model should be the smallest
    thing that satisfies the contract, because every extra field is a
    requirement the skill imposes on its callers and a coupling that survives
    the lab that needed it. Lab 1 changed its step shape at least once during
    its own build; a skill importing it would have broken with no skill change.

    Attributes:

    Attributes:
        step_id: Stable id, e.g. ``S1``.
        name: Human name.
        agent: The node that will run it.
        intent: What it is for.
        acceptance_criteria: How we will know it worked. Each becomes a
            verification obligation, so the criterion stops being a comment in
            a plan and becomes something drift can be measured against.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = ()


PlanInputs.model_rebuild()


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


class Obligation(BaseModel):
    """One mechanical, checkable commitment the pipeline must honour.

    An obligation is the unit drift is measured in. It is deliberately *not*
    prose: it has a kind, a subject it is about, and a predicate that either
    holds or does not.

    Attributes:
        obligation_id: Stable and content-derived, so two runs of the same
            case produce the same ids and a diff between runs is meaningful.
        kind: One of the `KIND_*` constants.
        subject: What the obligation is about -- a unit id, an assignment id,
            or a capability name.
        statement: Prose form, for the human reading the plan.
        criterion: The acceptance criterion this came from, if any.
    """

    model_config = ConfigDict(extra="forbid")

    obligation_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    criterion: str = ""


#: The obligation vocabulary. Closed on purpose: an open vocabulary cannot be
#: checked for completeness, and "we might have missed one" is the exact state
#: the document's plan-drift row exists to rule out.
KIND_COVER_UNIT = "cover_unit"
KIND_DISTRIBUTE_ASSIGNMENT = "distribute_assignment"
KIND_TRACK_MILESTONES = "track_milestones"
KIND_VERIFY_STEP = "verify_step"
KIND_GROUND_FACTS = "ground_facts"

OBLIGATION_KINDS = (
    KIND_COVER_UNIT,
    KIND_DISTRIBUTE_ASSIGNMENT,
    KIND_TRACK_MILESTONES,
    KIND_VERIFY_STEP,
    KIND_GROUND_FACTS,
)

#: Obligation kinds that cannot be settled until the pipeline actually runs.
#:
#: `verify_step` obligations come from Lab 1's acceptance criteria, and Lab 3
#: runs no steps -- it locks a blueprint and renders a shell. Marking them
#: satisfied, or marking them failed, would both be fabrications: the first
#: claims execution that did not happen, the second reports drift for a step
#: that was never given a chance to run.
#:
#: So they are deferred. `check_against` counts them and names them, but holds
#: them out of the verdict. Lab 7, which wires the node graph and therefore
#: runs the steps, is the lab that settles them. Recording this explicitly is
#: what stops "no drift detected" from quietly meaning "nothing was checked".
DEFERRED_KINDS: frozenset[str] = frozenset({KIND_VERIFY_STEP})


#: Matches the numeric part of an id that carries a prefix, e.g. the "10" in
#: "S10" or "unit-3". Compiled once at module level; this runs for every
#: obligation on every lock and every drift check.
_NUMERIC_PART = re.compile(r"(\d+)")


def _sort_key(subject: str) -> tuple[Any, ...]:
    """Order an id the way a person reads a numbered list.

    Natural ordering, so `S10` follows `S2` rather than preceding it, and unit
    "10" follows unit "9". Plain string comparison gets both backwards, and
    since a locked plan's obligation order feeds its content hash, that is not
    a cosmetic bug: two runs that listed the same scope in a different order
    would produce different hashes for an identical plan, and the drift check
    could never tell a reordered plan from a changed one.

    Any run of digits sorts numerically; text between them sorts as text. So
    `ASG-X2` sorts before `ASG-X10`, and `U1a` before `U1b`.

    Args:
        subject: An obligation subject: a unit id, assignment id or step id.

    Returns:
        A tuple of alternating text/number components, suitable for `sorted`.
        Comparisons never mix types, because text and number components
        alternate deterministically for any two ids of the same shape.
    """
    parts = _NUMERIC_PART.split(subject)
    # `re.split` with a capture group yields text, number, text, number...
    # Numbers must be ints for 10 > 2; empty strings must be normalised so a
    # missing text part never compares against a number.
    return tuple(
        (0, int(part)) if index % 2 else (1, part)
        for index, part in enumerate(parts)
    )


def obligation_id(kind: str, subject: str) -> str:
    """Build a stable, content-derived obligation id.

    Args:
        kind: One of the `KIND_*` constants.
        subject: The obligation's subject.

    Returns:
        A short id like ``OBL-cover_unit/U1``. Content-derived rather than
        positional, so inserting an obligation does not renumber the others.
    """
    return f"OBL-{kind}/{subject}"


#: Facts whose value changes on every single run -- timestamps, durations,
#: generated ids. They are excluded from a locked plan entirely.
#:
#: This is not tidiness. Lab 2's `verified_facts` includes `computed_at`, a
#: UTC timestamp. Feeding that into `PlanInputs.verified_facts` would put a
#: clock reading into the plan's identity, so two runs of an identical case
#: would produce different `plan_sha256` values. That destroys the
#: reproducibility a lock exists to provide -- a blueprint whose hash changes
#: every time you look at it cannot be used to detect drift, because drift
#: would be indistinguishable from time passing. It would also make every
#: subsequent drift check report a `value_mismatch` on `computed_at` that is
#: really just a clock tick.
VOLATILE_FACT_KEYS: frozenset[str] = frozenset(
    {"computed_at", "generated_at", "elapsed_ms", "run_id", "call_id", "timestamp"}
)


def stable_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the facts that vary per run.

    Args:
        facts: Tool-computed facts as Lab 2 produced them.

    Returns:
        The same facts minus `VOLATILE_FACT_KEYS`. Keys are sorted so the
        result is itself reproducible.
    """
    return {
        key: value
        for key, value in sorted(facts.items())
        if key not in VOLATILE_FACT_KEYS
    }


#: Returned by `resolve_fact` when the key names no verified fact. A sentinel
#: rather than None, because None is a legitimate fact value and would make
#: "no tool verified this" indistinguishable from "the tool said None".
MISSING: Any = object()


def resolve_fact(facts: Mapping[str, Any], key: str) -> Any:
    """Look a fact up by dotted path, through nested mappings.

    Lab 2 records per-assignment marks as a nested mapping --

        marks_by_assignment = {"ASG-X1": 20.0, ...}

    -- and names an individual value by a dotted key,
    ``marks_by_assignment.ASG-X1``. Both shapes have to be checkable against
    the same `verified_facts`, or the grounding obligation would reject a
    correct number it simply could not follow a level down.

    Args:
        facts: The plan's verified facts.
        key: A top-level key, or a dotted path into nested mappings.

    Returns:
        The value found, or `MISSING` if any segment of the path is absent.
        An empty path segment is treated as an ordinary key, so a fact whose
        own name contains a dot stays addressable.
    """
    current: Any = facts
    for segment in key.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return MISSING
        current = current[segment]
    return current


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------


class PlanDraft(BaseModel):
    """An expandable, still-editable plan. `lock()` turns it into a `LockedPlan`.

    Mutable by design. Lab 1's whole point was an *editable* outline, and a
    pipeline whose blueprint cannot be revised before it is signed off cannot
    be used for real work. Editability ends at `.lock()`, and nowhere earlier.

    Attributes:
        course_code: The course, as data.
        course_name: Display name.
        units: Units in scope.
        assignment_ids: Assignments in scope.
        include_milestones: Whether milestones are in scope.
        obligations: The expanded obligations, in insertion order.
        verified_facts: Lab 2's tool-computed facts.
        notes: Free-text notes carried from the inputs.
        as_of: Anchor date.
        human_authority: Who must approve.
        locked_hash: Set once locked; guards against the aliasing bug.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    course_code: str = Field(min_length=1)
    course_name: str = Field(min_length=1)
    units: tuple[str, ...] = Field(min_length=1)
    assignment_ids: tuple[str, ...] = ()
    include_milestones: bool = True
    obligations: list[Obligation] = Field(default_factory=list)
    verified_facts: Mapping[str, Any] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
    as_of: Optional[str] = None
    human_authority: str = "course_faculty"

    # -- editing -----------------------------------------------------------

    def add_obligation(self, obligation: Obligation) -> Obligation:
        """Add an obligation, refusing a duplicate id.

        Args:
            obligation: The obligation to add.

        Returns:
            The same obligation.

        Raises:
            SkillError: If an obligation with that id already exists. Two
                obligations with one id make the set difference used for drift
                detection ambiguous, which is worse than not adding it.
        """
        if any(o.obligation_id == obligation.obligation_id for o in self.obligations):
            raise SkillError(
                f"obligation {obligation.obligation_id!r} is already present",
                code="duplicate_obligation",
            )
        self.obligations.append(obligation)
        return obligation

    def drop_obligation(self, obligation_id: str) -> Obligation:
        """Remove an obligation by id.

        Args:
            obligation_id: The id to remove.

        Returns:
            The removed obligation.

        Raises:
            KeyError: If no such obligation exists.
        """
        for index, obligation in enumerate(self.obligations):
            if obligation.obligation_id == obligation_id:
                return self.obligations.pop(index)
        raise KeyError(f"no obligation {obligation_id!r}")

    # -- content hash ------------------------------------------------------

    def content_payload(self) -> dict[str, Any]:
        """The exact structure the lock hash is computed over.

        Delegates to the shared `_content_payload`, which
        `LockedPlan.recompute_sha256` also uses, so the two cannot disagree.
        """
        return _content_payload(
            course_code=self.course_code,
            units=self.units,
            assignment_ids=self.assignment_ids,
            include_milestones=self.include_milestones,
            obligations=self.obligations,
            verified_facts=self.verified_facts,
            notes=self.notes,
            human_authority=self.human_authority,
        )

    def plan_sha256(self) -> str:
        """Content hash of the draft, over `content_payload()`."""
        return _sha256(self.content_payload())

    # -- lock --------------------------------------------------------------

    def lock(self) -> "LockedPlan":
        """Freeze this draft. Document: `expand(inputs).lock()`.

        Deep-copies by serialisation rather than aliasing. If the locked plan
        shared its `Obligation` objects with the draft, a later
        `draft.add_obligation(...)` would appear inside the locked plan --
        history rewritten through the lock, silently. Serialising costs a
        round-trip and removes the possibility entirely.

        Returns:
            An immutable `LockedPlan`.
        """
        payload = self.content_payload()
        # Stamped after the hash is computed, and deliberately absent from
        # `content_payload`. A lock time is a fact about *this* locking, not
        # about the plan's content, so including it would make two runs of an
        # identical case hash differently -- the defect `VOLATILE_FACT_KEYS`
        # exists to prevent, arriving from the other direction.
        locked_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        return LockedPlan(
            course_code=payload["course_code"],
            course_name=self.course_name,
            units=tuple(payload["units"]),
            assignment_ids=tuple(payload["assignment_ids"]),
            include_milestones=payload["include_milestones"],
            obligations=tuple(
                Obligation.model_validate(item) for item in payload["obligations"]
            ),
            verified_facts=dict(payload["verified_facts"]),
            notes=tuple(self.notes),
            as_of=self.as_of,
            human_authority=payload["human_authority"],
            plan_sha256=_sha256(payload),
            obligation_count=len(payload["obligations"]),
            locked_at=locked_at,
        )


#: The five content fields of an obligation, in hash order. Named once so
#: `_obligation_payload` cannot be extended in one place and not the other.
_OBLIGATION_FIELDS = (
    "obligation_id",
    "kind",
    "subject",
    "statement",
    "criterion",
)


def _content_payload(
    *,
    course_code: str,
    units: "Sequence[str]",
    assignment_ids: "Sequence[str]",
    include_milestones: bool,
    obligations: "Sequence[Any]",
    verified_facts: Mapping[str, Any],
    notes: "Sequence[str]",
    human_authority: str,
) -> dict[str, Any]:
    """Build the one structure the lock hash is computed over.

    Args:
        course_code: The course the plan is for.
        units: In-scope units, in teaching order.
        assignment_ids: In-scope assignments, in teaching order.
        include_milestones: Whether milestones are in scope.
        obligations: The commitments. Sorted here, so the caller's order does
            not affect the hash.
        verified_facts: Tool-computed facts. Filtered through `stable_facts`.
        notes: Recorded assumptions. Included, because the document shows them.
        human_authority: Who must approve the work.

    Returns:
        A JSON-compatible mapping, ready for `_sha256`.

    One function, not two. `PlanDraft.content_payload` and
    `LockedPlan.recompute_sha256` are the same computation over the same
    fields at two points in a model's life, and an earlier version built the
    dict in each. That is a latent fault, not a style question: adding a field
    to one and not the other would make every integrity check fail, or -- far
    worse -- make them pass while agreeing on something other than the whole
    plan, which is a check that reports "no tampering" while unable to detect
    it.

    `verified_facts` is filtered through `stable_facts` so a per-run timestamp
    cannot enter the plan's identity. `locked_at` is absent for the same
    reason from the other side: a clock reading would make two identical runs
    hash differently. Both exclusions are about reproducibility; the test for
    inclusion is simply whether the document shows the field.
    """
    return {
        "course_code": course_code,
        "units": list(units),
        "assignment_ids": list(assignment_ids),
        "include_milestones": include_milestones,
        "obligations": [
            _obligation_payload(o) for o in sorted(obligations, key=_obligation_sort_key)
        ],
        "verified_facts": stable_facts(verified_facts),
        "notes": list(notes),
        "human_authority": human_authority,
    }


def _obligation_field(obligation: "Obligation | Mapping[str, Any]", name: str) -> Any:
    """Read one field of an obligation, model or raw mapping alike.

    `recompute_sha256` exists to detect tampering, so it has to run on objects
    that may well be malformed -- a `model_construct` bypasses validation and
    leaves whatever was passed in, so `obligations` can legitimately hold plain
    dicts. An integrity check that raised `AttributeError` on those would be
    useless against exactly the adversary it exists for: it would crash instead
    of reporting, and the crash would look like an unrelated bug.

    Args:
        obligation: An `Obligation`, or a mapping with the same field names.
        name: The field to read.

    Returns:
        The field value, or `""` when absent. Missing and empty hash alike only
        if the field was genuinely absent, which is itself a change from the
        locked state and therefore still detected.
    """
    if isinstance(obligation, Mapping):
        return obligation.get(name, "")
    return getattr(obligation, name, "")


def _obligation_payload(
    obligation: "Obligation | Mapping[str, Any]",
) -> dict[str, Any]:
    """The JSON shape of one obligation, as the lock hash sees it.

    Args:
        obligation: The obligation to serialise.

    Returns:
        Its five content fields, in a fixed order.

    Split out so `PlanDraft.content_payload` and
    `LockedPlan.recompute_sha256` cannot diverge. Built separately in each, a
    later edit that added a field to one and forgot the other would either make
    every integrity check fail, or -- worse -- make them pass while agreeing on
    something other than the whole plan.
    """
    return {
        name: _obligation_field(obligation, name)
        for name in _OBLIGATION_FIELDS
    }


def _obligation_sort_key(
    obligation: "Obligation | Mapping[str, Any]",
) -> tuple[Any, ...]:
    """Canonical order for obligations: by kind, then naturally by subject.

    Plain string ordering puts step "S10" before "S2" and unit "10" before
    unit "2", so a plan would list its steps and units in an order no syllabus
    uses. The order is also part of the plan's *identity*, since it feeds the
    hash, so it has to be deterministic and legible at the same time.

    Args:
        obligation: The obligation to place.

    Returns:
        A sort key: kind, then the subject's natural key, then the criterion.
    """
    return (
        _obligation_field(obligation, "kind"),
        _sort_key(str(_obligation_field(obligation, "subject"))),
        str(_obligation_field(obligation, "criterion")),
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    """Stable hash of a JSON-compatible mapping.

    `sort_keys` and the tight separators are what make this reproducible: the
    same plan must hash the same on a different machine, in a different
    process, with dict insertion order reversed.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The locked plan
# ---------------------------------------------------------------------------


class LockedPlan(BaseModel):
    """An immutable blueprint. Document: "A locked plan object."

    2.12 defines the blueprint as "The locked specification the pipeline
    obeys", so immutability is not decoration here -- it is the property the
    rest of the pipeline is allowed to rely on.

    Every collection is a `tuple`. Pydantic's `frozen=True` blocks attribute
    rebinding but leaves `list` contents mutable, so a "locked" plan with list
    fields could still be edited via `append`. Tuples have no mutating methods,
    which closes that hole by construction rather than by convention.

    Attributes:
        course_code: The course, as data.
        course_name: Display name.
        units: Units in scope.
        assignment_ids: Assignments in scope.
        include_milestones: Whether milestones are in scope.
        obligations: The expanded obligations, sorted by id.
        verified_facts: Lab 2's tool-computed facts.
        notes: Carried notes.
        as_of: Anchor date.
        human_authority: Who must approve.
        plan_sha256: Hash over `PlanDraft.content_payload()` at lock time.
        obligation_count: Redundant with `len(obligations)`, recorded so a
            tampered plan is obvious from the header alone.

            Note on what the hash does and does not cover: `units` and
            `assignment_ids` are hashed *in the order given*, because that
            order is teaching order and a plan that teaches unit 2 before unit
            1 is a different plan. The obligation list is hashed sorted,
            because it is a set that merely has an order for display. So
            reordering scope changes the hash, while reordering obligations
            does not -- deliberately, and the two fields are read differently
            for that reason.
        locked_at: UTC timestamp of the lock. Recorded for the audit trail but
            excluded from `plan_sha256` -- see `lock()`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    course_code: str = Field(min_length=1)
    course_name: str = Field(min_length=1)
    units: tuple[str, ...] = Field(min_length=1)
    assignment_ids: tuple[str, ...] = ()
    include_milestones: bool = True
    obligations: tuple[Obligation, ...] = ()
    verified_facts: Mapping[str, Any] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
    as_of: Optional[str] = None
    human_authority: str = "course_faculty"
    plan_sha256: str = Field(min_length=64, max_length=64)
    obligation_count: int = Field(ge=0)
    locked_at: str = ""

    # -- queries -----------------------------------------------------------

    def obligation_ids(self) -> tuple[str, ...]:
        """Every obligation id, in the plan's canonical order."""
        return tuple(o.obligation_id for o in self.obligations)

    def obligations_of(self, kind: str) -> tuple[Obligation, ...]:
        """Obligations of one kind, in canonical order.

        Args:
            kind: One of the `KIND_*` constants.

        Returns:
            Matching obligations. Empty if none, which is a legitimate answer
            rather than an error.

        Re-sorted rather than filtered in place, because the caller reads this
        as a list -- it renders into the document's acceptance-checks section,
        where S10 appearing before S2 would be wrong however it was stored.
        """
        return tuple(
            sorted(
                (o for o in self.obligations if o.kind == kind),
                key=_obligation_sort_key,
            )
        )

    def subjects_of(self, kind: str) -> tuple[str, ...]:
        """The subjects of every obligation of one kind."""
        return tuple(o.subject for o in self.obligations_of(kind))

    def recompute_sha256(self) -> str:
        """Hash this plan's own content, to compare against `plan_sha256`.

        Returns:
            The hash recomputed from `LockedPlan`'s current fields.

        Raises:
            SkillError: If the locked payload no longer matches its recorded
                hash, which means the plan was altered after locking. This is
                the check that makes tampering detectable even if a caller
                reaches past the frozen model.
        """
        actual = _sha256(
            _content_payload(
                course_code=self.course_code,
                units=self.units,
                assignment_ids=self.assignment_ids,
                include_milestones=self.include_milestones,
                obligations=self.obligations,
                verified_facts=self.verified_facts,
                notes=self.notes,
                human_authority=self.human_authority,
            )
        )
        if actual != self.plan_sha256:
            raise SkillError(
                f"locked plan {self.course_code} fails its own integrity check. "
                f"recorded {self.plan_sha256[:16]}..., recomputed {actual[:16]}...",
                code="plan_tampered",
            )
        return actual

    def outline(self) -> str:
        """Render the blueprint for a human reader.

        Returns:
            A readable block: header, hash, then every obligation with its
            kind and statement. Column widths come from the data, so a long
            unit id cannot push the statement column out of alignment.
        """
        header = [
            f"LOCKED PLAN  {self.course_code} ({self.course_name})",
            f"  units        : {', '.join(self.units)}",
            f"  assignments  : {', '.join(self.assignment_ids) or 'none'}",
            f"  milestones   : {'in scope' if self.include_milestones else 'out of scope'}",
            f"  obligations  : {len(self.obligations)}",
            f"  approver     : {self.human_authority}",
            f"  plan sha256  : {self.plan_sha256}",
            "",
        ]

        if not self.obligations:
            return "\n".join(header + ["  (no obligations)"])

        id_width = max(len("OBLIGATION"), max(len(o.obligation_id) for o in self.obligations))
        kind_width = max(len("KIND"), max(len(o.kind) for o in self.obligations))
        rows = [
            f"  {o.obligation_id.ljust(id_width)}  {o.kind.ljust(kind_width)}  "
            f"{o.statement}"
            for o in self.obligations
        ]
        column_header = "  " + "OBLIGATION".ljust(id_width) + "  " + "KIND".ljust(kind_width)
        return "\n".join(header + [column_header, "-" * (len(column_header) + 2)] + rows)


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


class DriftFinding(BaseModel):
    """One deviation between a locked plan and what was actually produced.

    Attributes:
        kind: ``missing`` (promised, not produced), ``unexpected`` (produced,
            not promised), or ``value_mismatch`` (produced with the wrong
            value).
        obligation_id: The obligation at fault, or ``-`` for `unexpected`.
        subject: What the deviation is about.
        detail: Prose explanation, with the numbers in it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    obligation_id: str = "-"
    subject: str = "-"
    detail: str = ""


class DriftReport(BaseModel):
    """The result of checking a produced result against a locked plan.

    Document 2.12: "validation rejects any deviation before the human sees
    it." This is the object that makes that sentence executable.

    Attributes:
        conforms: True only when there are no findings at all.
        plan_sha256: The plan this was checked against, so the verdict cannot
            be quoted without its subject.
        findings: Every deviation found.
        obligations_checked: How many obligations were evaluated for drift.
        obligations_deferred: How many were held out of the verdict because
            nothing has run yet. Reported alongside `obligations_checked`
            because "no drift detected" must never be able to mean "nothing
            was actually checked".
        step_criteria_total: How many Lab 1 acceptance criteria the locked
            plan carried, which is what `obligations_deferred` counts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    conforms: bool = True
    plan_sha256: str = ""
    findings: tuple[DriftFinding, ...] = ()
    obligations_checked: int = 0
    obligations_deferred: int = 0
    step_criteria_total: int = 0

    @property
    def blocking(self) -> bool:
        """Whether a human should be shown this at all.

        Naming the guardrail rather than inlining `not conforms`, because this
        is the single sentence of 2.12 that Lab 3 exists to implement.
        """
        return not self.conforms

    @property
    def coverage_is_complete(self) -> bool:
        """Whether *every* obligation was actually evaluated.

        False whenever obligations were deferred. This is the honest form of
        the verdict: `conforms` says nothing was found, this says whether
        anything was looked for.
        """
        return self.obligations_deferred == 0

    def summary(self) -> str:
        """One line: the verdict and its count."""
        deferred_note = (
            f", {self.obligations_deferred} deferred (no step has run yet)"
            if self.obligations_deferred
            else ""
        )
        if self.conforms:
            return (
                f"conforms to locked plan {self.plan_sha256[:12]}... "
                f"({self.obligations_checked} obligation(s) checked"
                f"{deferred_note}, no drift)"
            )
        return (
            f"DRIFT: {len(self.findings)} deviation(s) from locked plan "
            f"{self.plan_sha256[:12]}... "
            f"({self.obligations_checked} obligation(s) checked{deferred_note})"
        )

    def table(self) -> str:
        """A readable findings table."""
        if self.conforms:
            return "no drift: the result matches the locked plan exactly"
        headers = ("kind", "obligation", "subject", "detail")
        rows = [
            (f.kind, f.obligation_id, f.subject, f.detail) for f in self.findings
        ]
        widths = [
            max(len(headers[i]), max(len(row[i]) for row in rows))
            for i in range(len(headers))
        ]

        def line(values: tuple[str, ...]) -> str:
            return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

        out = [line(headers), "-" * (sum(widths) + 2 * (len(widths) - 1))]
        out.extend(line(row) for row in rows)
        return "\n".join(out)


class WorkspaceResult(BaseModel):
    """What a run actually produced. The `result` of the format skill.

    Document 2.10 Lab 3 Output: "A locked plan object; a formatted organised
    course workspace (notes, assignments, grades) shell."

    Attributes:
        course_code: Which course this describes.
        units_covered: Units that were actually covered.
        assignments_included: Assignments actually distributed.
        milestones_emitted: Whether milestones were actually emitted.
        completed_steps: Step ids that ran.
        reported_facts: Every number the result reports, each traceable to a
            Lab 2 verified fact.
        notes_by_unit: Note counts per unit, so the document can show coverage.
        ground_fact_keys: Which `verified_facts` keys were consumed.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    course_code: str = Field(min_length=1)
    units_covered: tuple[str, ...] = ()
    assignments_included: tuple[str, ...] = ()
    milestones_emitted: bool = False
    completed_steps: tuple[str, ...] = ()
    reported_facts: Mapping[str, Any] = Field(default_factory=dict)
    notes_by_unit: Mapping[str, int] = Field(default_factory=dict)
    ground_fact_keys: tuple[str, ...] = ()

    def check_against(self, plan: LockedPlan) -> DriftReport:
        """Find every deviation between this result and `plan`.

        This is the mechanical half of 2.12's "validation rejects any
        deviation before the human sees it". It is a set comparison, not a
        judgement: obligations the result did not satisfy, results the plan
        did not promise, and facts the result reported that the plan never
        declared.

        Args:
            plan: The locked blueprint to check against.

        Returns:
            A `DriftReport`. `conforms` is True only when `findings` is empty.
        """
        plan.recompute_sha256()
        findings: list[DriftFinding] = []
        deferred = 0

        for obligation in plan.obligations:
            if obligation.kind in DEFERRED_KINDS:
                # Held out of the verdict, not silently passed. See
                # `DEFERRED_KINDS` for why reporting these as failures would
                # be as much of a fabrication as marking them satisfied.
                deferred += 1
                continue
            satisfied, detail = self._satisfied(obligation)
            if not satisfied:
                findings.append(
                    DriftFinding(
                        kind="missing",
                        obligation_id=obligation.obligation_id,
                        subject=obligation.subject,
                        detail=detail,
                    )
                )

        for subject in self.units_covered:
            if subject not in plan.units:
                findings.append(
                    DriftFinding(
                        kind="unexpected",
                        subject=subject,
                        detail=(
                            f"unit {subject} was covered but is not in the "
                            f"locked plan's scope"
                        ),
                    )
                )

        for assignment_id in self.assignments_included:
            if assignment_id not in plan.assignment_ids:
                findings.append(
                    DriftFinding(
                        kind="unexpected",
                        subject=assignment_id,
                        detail=(
                            f"assignment {assignment_id} was distributed but "
                            f"is not in the locked plan's scope"
                        ),
                    )
                )

        for key in sorted(self.reported_facts):
            verified = resolve_fact(plan.verified_facts, key)
            if verified is MISSING:
                findings.append(
                    DriftFinding(
                        kind="value_mismatch",
                        subject=key,
                        detail=(
                            f"result reports {key!r}={self.reported_facts[key]!r}, "
                            f"which no tool verified for this plan"
                        ),
                    )
                )
            elif verified != self.reported_facts[key]:
                findings.append(
                    DriftFinding(
                        kind="value_mismatch",
                        subject=key,
                        detail=(
                            f"result reports {key!r}={self.reported_facts[key]!r} "
                            f"but the tool verified {verified!r}"
                        ),
                    )
                )

        return DriftReport(
            conforms=not findings,
            plan_sha256=plan.plan_sha256,
            findings=tuple(findings),
            obligations_checked=len(plan.obligations) - deferred,
            obligations_deferred=deferred,
            step_criteria_total=len(plan.obligations_of(KIND_VERIFY_STEP)),
        )

    def _satisfied(self, obligation: Obligation) -> tuple[bool, str]:
        """Evaluate one obligation against this result.

        Args:
            obligation: The obligation to test.

        Returns:
            ``(satisfied, detail)``. `detail` explains a failure and is empty
            on success, so a report never has to invent a reason.
        """
        if obligation.kind == KIND_COVER_UNIT:
            covered = obligation.subject in self.units_covered
            return covered, (
                ""
                if covered
                else f"unit {obligation.subject} is in the locked plan's scope "
                f"but was not covered"
            )

        if obligation.kind == KIND_DISTRIBUTE_ASSIGNMENT:
            included = obligation.subject in self.assignments_included
            return included, (
                ""
                if included
                else f"assignment {obligation.subject} is in the locked plan's "
                f"scope but was not distributed"
            )

        if obligation.kind == KIND_TRACK_MILESTONES:
            return self.milestones_emitted, (
                ""
                if self.milestones_emitted
                else "the locked plan requires milestone tracking but the "
                "result emitted none"
            )

        if obligation.kind == KIND_VERIFY_STEP:
            ran = obligation.subject in self.completed_steps
            return ran, (
                ""
                if ran
                else f"step {obligation.subject} is in the locked plan but did "
                f"not run"
            )

        if obligation.kind == KIND_GROUND_FACTS:
            ungrounded = [
                key
                for key in sorted(self.reported_facts)
                if key not in self.ground_fact_keys
            ]
            return not ungrounded, (
                ""
                if not ungrounded
                else f"these reported facts were never traced to a tool: "
                f"{ungrounded}"
            )

        # An unrecognised kind cannot be satisfied or cleared. Refusing to
        # pass it is the safe direction: an obligation nobody can evaluate is
        # an obligation nobody is enforcing.
        return False, (
            f"obligation {obligation.obligation_id} has unknown kind "
            f"{obligation.kind!r}, so it cannot be verified"
        )


# ---------------------------------------------------------------------------
# expand() and the skill
# ---------------------------------------------------------------------------


def expand(inputs: PlanInputs) -> PlanDraft:
    """Lower parsed inputs into explicit obligations.

    This is the `expand` in the document's pseudocode. It turns a plan a
    person reads into commitments a program can check, which is what makes
    drift detectable rather than arguable.

    Args:
        inputs: The parsed inputs.

    Returns:
        An editable `PlanDraft`.

    Raises:
        SkillError: If a unit appears twice, which would make one obligation
            stand for two and quietly weaken the check.
    """
    seen_units: set[str] = set()
    for unit in inputs.units:
        if unit in seen_units:
            raise SkillError(
                f"unit {unit!r} appears twice in the parsed inputs; the plan "
                f"would carry one obligation for two units",
                code="duplicate_unit",
            )
        seen_units.add(unit)

    draft = PlanDraft(
        course_code=inputs.course_code,
        course_name=inputs.course_name,
        units=tuple(inputs.units),
        assignment_ids=tuple(inputs.assignment_ids),
        include_milestones=inputs.include_milestones,
        verified_facts=dict(inputs.verified_facts),
        # Carried through rather than dropped. A plan that changed a parameter
        # without asking has to say so on the artefact, because that record is
        # the only thing a later reader can compare the plan against.
        notes=tuple(inputs.notes),
        as_of=inputs.as_of,
        human_authority=inputs.human_authority,
    )

    # Units, in teaching order. Sorted later by id, so the canonical order of
    # the locked plan is stable no matter what order the caller supplied.
    for unit in inputs.units:
        draft.add_obligation(
            Obligation(
                obligation_id=obligation_id(KIND_COVER_UNIT, unit),
                kind=KIND_COVER_UNIT,
                subject=unit,
                statement=f"cover unit {unit} with notes from the materials store",
            )
        )

    for assignment_id in inputs.assignment_ids:
        draft.add_obligation(
            Obligation(
                obligation_id=obligation_id(
                    KIND_DISTRIBUTE_ASSIGNMENT, assignment_id
                ),
                kind=KIND_DISTRIBUTE_ASSIGNMENT,
                subject=assignment_id,
                statement=(
                    f"distribute {assignment_id} with its rubric, marks "
                    f"computed by the tool layer"
                ),
            )
        )

    if inputs.include_milestones:
        draft.add_obligation(
            Obligation(
                obligation_id=obligation_id(KIND_TRACK_MILESTONES, "course"),
                kind=KIND_TRACK_MILESTONES,
                subject="course",
                statement="track project milestones for every project group",
            )
        )

    for step in inputs.steps:
        for index, criterion in enumerate(step.acceptance_criteria, start=1):
            draft.add_obligation(
                Obligation(
                    obligation_id=obligation_id(
                        KIND_VERIFY_STEP, f"{step.step_id}#{index}"
                    ),
                    kind=KIND_VERIFY_STEP,
                    subject=step.step_id,
                    statement=criterion,
                    criterion=criterion,
                )
            )

    draft.add_obligation(
        Obligation(
            obligation_id=obligation_id(KIND_GROUND_FACTS, "all"),
            kind=KIND_GROUND_FACTS,
            subject="all",
            statement=(
                "every reported number is one a tool verified for this plan, "
                "never one supplied in the prompt"
            ),
        )
    )

    log_event(
        logger,
        "skill_plan_expanded",
        course_code=inputs.course_code,
        units=len(inputs.units),
        assignments=len(inputs.assignment_ids),
        obligations=len(draft.obligations),
    )
    return draft


def plan_skill(inputs: PlanInputs, context: SkillContext) -> LockedPlan:
    """Document: `skill plan(inputs) -> Plan: return expand(inputs).lock()`.

    Args:
        inputs: The parsed inputs.
        context: Run context; used for the ledger, not otherwise.

    Returns:
        The locked blueprint.
    """
    return expand(inputs).lock()


PLAN_SKILL_SPEC = SkillSpec(
    name="plan",
    description=(
        "expand parsed inputs into checkable obligations and lock them into "
        "an immutable blueprint (doc 2.10 Lab 3: expand(inputs).lock())"
    ),
    input_model=PlanInputs,
    output_model=LockedPlan,
    handler=plan_skill,
    case_agnostic=True,
    tags=("planning", "blueprint", "phase-a"),
)


def register_plan_skill(registry: SkillRegistry) -> SkillRegistry:
    """Register the `plan` skill on a registry.

    Args:
        registry: The registry to add to.

    Returns:
        The same registry, so calls chain.
    """
    registry.register(PLAN_SKILL_SPEC)
    return registry
