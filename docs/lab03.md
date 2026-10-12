# Lab 3 — Skill Creation: reusable plan & format skills

**Phase:** A — Foundations
**Goal (doc §2.10):** *"Package repeatable capability as reusable, schema-typed skills."*
**Nodes activated (doc §2.10):** *"Plan node + Assembly node (as skills)."*

---

## a) Goal and the document's requirements

Every requirement below is quoted from the Worklet 2 document. Nothing in this
lab is inferred beyond these lines.

| # | Doc requirement (verbatim) | Where it is met |
|---|---|---|
| R1 | *"Package repeatable capability as reusable, schema-typed skills."* | `SkillSpec` carries a `description`, an `input_model`, an `output_model` and a `handler`. `SkillRegistry.invoke` validates the input **against the declared model** before the handler runs and checks the returned type against the declared output model after. |
| R2 | *"A plan/blueprint skill and a formatting skill that renders the organised course workspace (notes, assignments, grades) — both reusable across cases."* | Exactly two skills: `plan` and `format_organised`. `plan` produces the blueprint; `format_organised` renders it. Reuse across cases is §8 below. |
| R3 | **Inputs:** *"Parsed inputs; template spec."* | `PlanInputs` (parsed inputs) and `FormatInputs(result, plan, template)`, where `template` is a `TemplateSpec`. |
| R4 | **Outputs:** *"A locked plan object; a formatted organised course workspace (notes, assignments, grades) shell."* | `LockedPlan` and `Document`. The word **shell** is load-bearing: Lab 3 renders the structure, it does not execute the pipeline. See §h. |
| R5 | **Agents / nodes:** *"Planning and assembly emerge as skills."* | `plan_skill.py` and `format_skill.py`. Neither imports a lab module, which a test enforces — "emerge as skills" means they are standalone capabilities, not lab code. |
| R6 | **Deliverable:** *"Two callable skills with schemas, invoked by the agent."* | `SKILL_NAMES = ("plan", "format_organised")`. The agent invokes both per case, through `SkillRegistry`, which records every call in a ledger. |
| R7 | **Acceptance:** *"The same skill works for two different cases without code changes."* | **Four** cases, not two — see below. Proven in §8, and enforced structurally in §9. |
| R8 | **Pitfall:** *"Keep skills case-agnostic and typed; hard-coding one case breaks reuse."* | An AST scan over `src/course_organizer/skills/` fails if any course code or name appears anywhere in the package, **including docstrings and comments**. See §9. |
| R9 | §2.10 **skill interface**, verbatim:<br>`skill plan(inputs) -> Plan:  return expand(inputs).lock()`<br>`skill format_organised(result, template) -> Document` | Carried in the source as `DOC_PSEUDOCODE` and printed by the CLI. `expand()` and `.lock()` are the two halves of the `plan` handler; the second line is the `format_organised` handler. |
| R10 | §2.12 risk table, plan-drift row: *"Lock the plan (Lab 3/7)"* | The lock **and** the comparison against it. A lock nobody checks against is a document, not a control. `WorkspaceResult.check_against(plan) -> DriftReport`. |
| R11 | §2.12: *"The blueprint/plan is locked before work begins; validation is a hard gate; the human approves before publish."* | Enforced in the type. `DocumentStatus` has **no** `final` or `published` member: a clean document is `ready_for_review`, never more. See §c and §f. |
| R12 | Glossary: *"Blueprint / plan \| The locked specification the pipeline obeys."* | This is why drift detection is Lab 3's real job, and why the `verify_step` obligations Lab 3 cannot settle are reported as **deferred** rather than passed. |
| R13 | §2.7 validation checks (the four that gate Labs 7–8) | Every one of the four is a `verify_step` obligation on the locked plan, quoted from Lab 1's own acceptance criteria. See the obligation list in §b. |

### Why four cases and not two

The acceptance criterion says "two different cases". A two-case demo using one
course twice would pass a skill written as `if course == "CS201"`. The demo
therefore varies **two independent axes**:

| case | course | scope shape | milestones |
|---|---|---|---|
| `cs-broad` | CS201 | all 5 units, all 3 assignments | in scope |
| `cs-narrow` | CS201 | **one** unit, **one** assignment | **out** of scope |
| `db-broad` | DBMS402 | all 5 units, all 3 assignments | in scope |
| `db-narrow` | DBMS402 | a 2-unit, 2-assignment **subset** | **out** of scope |

A skill that only handles "the whole course" fails on `cs-narrow`. A skill that
hard-codes a course fails on `db-*`. Four cases still satisfy "two different
cases" — they satisfy it more strictly than the minimum. The narrow shapes
differ from each other too (one unit vs a two-unit window), so a skill that
assumes every subset is a single item fails on `db-narrow`.

---

## b) Brief concepts

### The skill is a declared contract, not a function

```python
SkillSpec(
    name="plan",
    description="expand parsed inputs into checkable obligations and lock them into an immutable blueprint",
    input_model=PlanInputs,
    output_model=LockedPlan,
    handler=plan_skill,
    case_agnostic=True,
)
```

`SkillRegistry.invoke` enforces the contract in five ordered steps. The order
is the design:

1. **Look up** the skill. Unknown name → `SkillError(code="unknown_skill")`, which
   lists the real ones.
2. **Validate** the input against `input_model`. Invalid → `SkillError(code="invalid_input")`,
   raised **before** the handler runs, so a malformed payload cannot half-expand
   a draft before being rejected.
3. **Run** the handler. A raise is *recorded* and returned as a failed
   `SkillResult` — a capability that failed at runtime is an auditable fact, not
   a crash.
4. **Check** the returned type against `output_model`. Mismatch →
   `SkillContractError`, **raised**, not recorded — a skill bug must not look
   like ordinary runtime trouble in the audit log.
5. **Append to the ledger.** Every call, successful or not.

### `expand(inputs).lock()` is split because "locked" has to mean something

The doc's pseudocode is one expression. It needs two objects to mean anything:

- `expand(inputs) -> PlanDraft` — **mutable**. A caller can build a plan and
  change its mind before committing.
- `.lock() -> LockedPlan` — **frozen, hashed, self-verifying**. A caller cannot
  change its mind afterwards.

A single mutable object could only offer one of those. The split means "locked"
is a state the plan *entered*, not a promise the type makes.

### The lock is enforced by the type, and `frozen=True` is not enough

```python
with pytest.raises(pydantic.ValidationError):
    locked.course_code = "HACKED"        # frozen blocks rebinding
with pytest.raises(AttributeError):
    locked.obligations.append("x")       # frozen does NOT block this
```

Pydantic's `frozen=True` blocks **rebinding** but leaves list **contents**
mutable. A "locked" plan built on list fields is still editable via `append`, and
the failure is completely silent. Every collection on `LockedPlan` is therefore
a `tuple`, which has no mutating methods at all. This is demonstrated in the
notebook (§4a) rather than asserted, because the two look identical in the class
body and only one of them works.

### Locking is a copy, not a handover

`lock()` serialises through Pydantic rather than aliasing. If it kept
references, everything the draft did *after* the lock would rewrite history
inside the locked plan — silently, because the frozen model is not involved.

```
second_draft.units = ("1","2","9"); second_draft.verified_facts["total"] = 999
-> locked.units  == ("1","2")     # unchanged
-> locked.verified_facts["total"] == 40.0
-> locked.plan_sha256 == hash_before
```

### The plan hashes its own content

`plan_sha256` is a SHA-256 over the locked payload. `recompute_sha256()`
recomputes it from the object's current fields and raises
`SkillError(code="plan_tampered")` if they disagree — so tampering is detectable
even by a caller who reached past the frozen model via `model_construct`.

Reproducibility is what makes this useful, and it took two exclusions:

- **`computed_at` is excluded.** Lab 2's `verified_facts` carry a per-run
  timestamp. Hashing it would make every identical run hash differently, and the
  drift check could never tell a changed plan from a clock ticking.
  `VOLATILE_FACT_KEYS` / `stable_facts()` handle this.
- **`locked_at` is excluded.** A lock time is a fact about *this* locking, not
  about the plan's content.

The test for inclusion is one line: **does the document show it?** It shows the
`notes` — the record of which parameters were assumed rather than asked — so
stripping one is detectable. It does not show a lock time.

### Obligations: a closed vocabulary, in a legible order

Five kinds, each mapping to one part of the doc's Output column:

| kind | statement | source |
|---|---|---|
| `cover_unit` | *"cover unit N with notes from the materials store"* | §2.7 read #1 |
| `distribute_assignment` | *"distribute ASG-Xn with its rubric, marks computed by the tool layer"* | §2.7 read #2, §2.12 *"Never award marks above the faculty maximum"* |
| `ground_facts` | *"every reported number is one a tool verified for this plan, never one supplied in the prompt"* | Lab 2 pitfall |
| `track_milestones` | *"milestone tracking is in scope for this plan"* | §2.4 node 6 |
| `verify_step` | Lab 1's own per-step acceptance criteria, quoted | §2.7's four checks land here |

**Closed on purpose.** An obligation kind nothing knows how to check is an
obligation nobody enforces, so `check_against` reports an unknown kind as a
**failure**, not a pass. An open vocabulary cannot be checked for completeness,
and silently passing an unknown kind would let a new kind be added and never
enforced.

**Natural order.** `S10` after `S2`, unit `10` after unit `9`. Plain string
comparison gets both backwards, and since the order feeds the content hash,
that is not cosmetic: two runs that listed the same scope in a different order
would hash differently and the drift check could never tell a reordered plan
from a changed one.

**Scope order is preserved, deliberately.** `units` and `assignment_ids` are
hashed *in the order given*, because that order is teaching order. A plan that
teaches unit 2 before unit 1 is a different plan. The obligation list is hashed
sorted, because it is a set that merely has an order for display. Both
behaviours are tested, and the asymmetry is documented in `LockedPlan`.

### Drift detection — §2.12's named job for this lab

`WorkspaceResult.check_against(plan) -> DriftReport` compares a produced result
against the plan it was produced under. Six classes of deviation, all reported
rather than tolerated:

| class | meaning |
|---|---|
| `missing` | the plan promised it, the result does not have it |
| `unexpected` | the result has it, the plan never asked for it |
| `value_mismatch` | a reported number no tool verified, or the wrong value |
| `ungrounded_fact` | a reported number the result itself did not claim as grounded |
| `unknown kind` | an obligation kind the checker does not implement → **fails** |
| *(incomplete)* | obligations whose subject is not in any scope the checker can test |

**Grounding has to follow a path.** Lab 2 records per-assignment marks as a
nested mapping but names an individual value with a dotted key:

```python
plan.verified_facts["marks_by_assignment"]        # {"ASG-01": 20.0, ...}
result.reported_facts["marks_by_assignment.ASG-01"]  # 20.0
```

Without path resolution the grounding obligation would reject a correct number
it simply could not look up. `resolve_fact(facts, key) -> value | MISSING`
walks nested mappings. `MISSING` is a sentinel rather than `None` because
`None` is a legitimate fact value and would make "no tool verified this"
indistinguishable from "the tool said None".

### Deferred is not passed

On a broad plan, 17 of 27 obligations are `verify_step` — criteria owned by the
Lab 1 steps. **Lab 3 runs no steps.** Those obligations are held out of the
verdict via `DEFERRED_KINDS` and render as `pending`:

```
0 ran, 17 pending - the locked blueprint carries them forward to the lab that executes the steps
```

The alternative — calling them passed or failed — would be a lie in one
direction or the other. "No drift" means "nothing in the 10 things Lab 3 can
check drifted", and the report says which 10:

```
conforms to locked plan 33db57242531... (10 obligation(s) checked,
17 deferred (no step has run yet), no drift)
```

`DriftReport.coverage_is_complete` is `False` whenever anything was deferred, so
a clean verdict can never be mistaken for a full one.

### The template is data, and it cannot omit the approval gate

```python
TemplateSpec(
    template_id="faculty-pack",
    title="Course Workspace Pack",
    audience="course faculty",
    sections=("header", "plan_lock", "units", "assignments", "checks", "drift", "approval"),
    body_style="markdown",     # markdown | plain | json
)
```

Three renderers, one set of section builders — so a new template is a new
`TemplateSpec`, not a new function. `TemplateSpec` **refuses** a template
missing `header` or `approval`. §2.12's approval gate becomes a property of the
template rather than a convention somebody has to remember, and a template that
omits it cannot be constructed.

An empty optional section is **omitted** rather than rendered as a bare heading:
a heading over an empty table is indistinguishable from a drifted result.
`include_empty_sections=True` overrides this, and `include_drift_table=False` can
hide the drift section **only when there is nothing to report** — findings are
never suppressible.

### One drift computation, shared

The drift verdict is computed **once** in `format_organised` and passed to every
builder. Two computations would give the body and the status two chances to
disagree, and a document reading "no drift" while carrying `status=draft` is
worse than either state alone.

---

## c) The code

### Files

| file | lines | role |
|---|---|---|
| `src/course_organizer/skills/base.py` | 598 | `SkillError`, `SkillSpec`, `SkillCall`, `SkillResult`, `SkillContext`, `SkillLedger`, `SkillRegistry` |
| `src/course_organizer/skills/plan_skill.py` | 1,246 | `PlanInputs` → `PlanDraft` → `.lock()` → `LockedPlan`; `WorkspaceResult.check_against`; `DriftReport` |
| `src/course_organizer/skills/format_skill.py` | 913 | `TemplateSpec`, section builders, three renderers, `format_organised` |
| `src/course_organizer/skills/__init__.py` | 141 | re-exports, `SKILL_NAMES`, `build_default_skill_registry` |
| `src/course_organizer/labs/lab03_skill_creation.py` | 1,029 | `CaseSpec`, `CASES`, `TEMPLATE_SPECS`, `SkillAgent`, `ReuseProof`, CLI |
| `tests/test_lab03_skill_creation.py` | 1,468 | 121 tests in 8 groups |

The `skills/` package is **standalone**: it imports no lab module, and no lab
module's constants. That is R5 ("planning and assembly emerge as skills") taken
literally, and it is enforced by a test rather than left to review.

### The core of `plan_skill.py`

```python
def expand(inputs: PlanInputs) -> PlanDraft:
    """Lower parsed inputs into explicit obligations.  <- the doc's `expand`"""
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
        notes=tuple(inputs.notes),
        as_of=inputs.as_of,
        human_authority=inputs.human_authority,
    )

    for unit in inputs.units:
        draft.add_obligation(Obligation(
            obligation_id=obligation_id(KIND_COVER_UNIT, unit),
            kind=KIND_COVER_UNIT,
            subject=unit,
            statement=f"cover unit {unit} with notes from the materials store",
        ))

    for assignment_id in inputs.assignment_ids:
        draft.add_obligation(Obligation(
            obligation_id=obligation_id(KIND_DISTRIBUTE_ASSIGNMENT, assignment_id),
            kind=KIND_DISTRIBUTE_ASSIGNMENT,
            subject=assignment_id,
            statement=(f"distribute {assignment_id} with its rubric, marks "
                       f"computed by the tool layer"),
        ))

    if inputs.include_milestones:
        draft.add_obligation(Obligation(
            obligation_id=obligation_id(KIND_TRACK_MILESTONES, "course"),
            kind=KIND_TRACK_MILESTONES,
            subject="course",
            statement="milestone tracking is in scope for this plan",
        ))

    draft.add_obligation(Obligation(
        obligation_id=obligation_id(KIND_GROUND_FACTS, "all"),
        kind=KIND_GROUND_FACTS,
        subject="all",
        statement=("every reported number is one a tool verified for this plan, "
                   "never one supplied in the prompt"),
    ))

    for step in inputs.steps:
        for index, criterion in enumerate(step.acceptance_criteria, start=1):
            draft.add_obligation(Obligation(
                obligation_id=obligation_id(
                    KIND_VERIFY_STEP, step.step_id, index),
                kind=KIND_VERIFY_STEP,
                subject=step.step_id,
                statement=criterion,
                criterion=index,
            ))

    return draft
```

Out-of-scope items create **no** obligation: a plan with milestones disabled
carries no `track_milestones` commitment, because an unmeetable promise is worse
than none.

### `lock()` — the doc's `.lock()`

```python
def lock(self) -> "LockedPlan":
    """Commit this draft to an immutable, hashed blueprint.  <- the doc's `.lock()`"""
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
```

`payload = self.content_payload()` is the deep copy: it goes through
`list(...)` and `model_validate`, so no mutable object is shared with the draft.

### One payload builder, not two

`PlanDraft.content_payload()` and `LockedPlan.recompute_sha256()` are the same
computation over the same fields at two points in a model's life. They both call
a single `_content_payload(...)`:

```python
def _content_payload(*, course_code, units, assignment_ids, include_milestones,
                     obligations, verified_facts, notes, human_authority):
    return {
        "course_code": course_code,
        "units": list(units),
        "assignment_ids": list(assignment_ids),
        "include_milestones": include_milestones,
        "obligations": [_obligation_payload(o)
                        for o in sorted(obligations, key=_obligation_sort_key)],
        "verified_facts": stable_facts(verified_facts),
        "notes": list(notes),
        "human_authority": human_authority,
    }
```

This was **not** the first version. Each built its own dict, and the code
carried a comment saying they must not drift apart. That is a latent fault, not a
style preference: adding a field to one and not the other would make every
integrity check fail, or — far worse — make them pass while agreeing on
something other than the whole plan, which is a check that reports "no
tampering" while being unable to detect it. During this build the divergence
actually happened: `notes` was added to the draft's payload and the locked
plan's integrity check kept passing. The single builder is the fix.

### `check_against` — the drift check

```python
def check_against(self, plan: "LockedPlan") -> "DriftReport":
    """Compare this result against the plan it was produced under."""
    plan.recompute_sha256()          # refuse to judge against a tampered plan
    findings: list[DriftFinding] = []

    promised_units = plan.subjects_of(KIND_COVER_UNIT)
    findings += _missing(promised_units, self.units_covered, "unit")
    findings += _unexpected(self.units_covered, promised_units, "unit")

    promised_asg = plan.subjects_of(KIND_DISTRIBUTE_ASSIGNMENT)
    findings += _missing(promised_asg, self.assignments_included, "assignment")
    findings += _unexpected(self.assignments_included, promised_asg, "assignment")

    if plan.obligations_of(KIND_TRACK_MILESTONES) and not self.milestones_emitted:
        findings.append(DriftFinding(
            kind="missing", subject="course",
            detail="milestones are in scope in the plan but the result emitted none"))

    for key in sorted(self.reported_facts):
        verified = resolve_fact(plan.verified_facts, key)
        if verified is MISSING:
            findings.append(DriftFinding(
                kind="value_mismatch", subject=key,
                detail=(f"result reports {key!r}={self.reported_facts[key]!r}, "
                        f"which no tool verified for this plan")))
        elif verified != self.reported_facts[key]:
            findings.append(DriftFinding(
                kind="value_mismatch", subject=key,
                detail=(f"result reports {key!r}={self.reported_facts[key]!r} "
                        f"but the tool verified {verified!r}")))

    ground = plan.subjects_of(KIND_GROUND_FACTS)
    if ground:
        for key in sorted(self.reported_facts):
            if key not in self.ground_fact_keys:
                findings.append(DriftFinding(
                    kind="ungrounded_fact", subject=key,
                    detail=f"{key!r} is reported but not declared as tool-grounded"))

    known = set(OBLIGATION_KINDS)
    settled = [o for o in plan.obligations if o.kind not in DEFERRED_KINDS]
    for obligation in settled:
        if obligation.kind not in known:
            findings.append(DriftFinding(
                kind="unknown kind", subject=obligation.obligation_id,
                detail=(f"obligation kind {obligation.kind!r} is not implemented, "
                        f"so it cannot be checked; failing it is the safe "
                        f"direction")))
    ...
    return DriftReport(
        plan_sha256=plan.plan_sha256,
        findings=tuple(findings),
        obligations_checked=len(settled),
        obligations_deferred=len(plan.obligations) - len(settled),
    )
```

### `format_skill.py` — the approval section

```python
def _build_approval(result, plan, template, drift):
    """The human gate. 2.12's hard stop, rendered so it cannot be missed.

    The status line states the *live* status rather than a fixed one, so a
    document that reads "READY FOR REVIEW" is visibly still awaiting a person,
    and a drifted one visibly is not ready at all. A static "DRAFT" string here
    would have been a lie in one direction the moment a clean document was
    produced, which is exactly the kind of fixed reassurance a reader cannot
    check.
    """
    status = (DocumentStatus.DRAFT if drift.blocking
              else DocumentStatus.READY_FOR_REVIEW)
    payload = {
        "authority": plan.human_authority,
        "required": True,
        "approved": False,
        "published": False,
        "status": status.value,
        "rule": ("Document 2.12: no grade, feedback or published artefact "
                 "advances without faculty approval."),
    }
    lines = [
        f"Approver : {plan.human_authority}",
        f"Status   : {status.value.replace('_', ' ').upper()}",
        "Approved : no - this lab implements no approval step",
        "Published: no",
        f"Rule     : {payload['rule']}",
    ]
    return payload, lines
```

### `DocumentStatus` — the type that cannot overstate

```python
class DocumentStatus(str, Enum):
    """The lifecycle a rendered document can be in.

    Two states, and deliberately no third.

    ``DRAFT``
        Something is unresolved -- almost always drift from the locked plan.

    ``READY_FOR_REVIEW``
        No drift, and the plan's integrity check passed. The document is
        complete and internally consistent. It is still *not* approved and
        *not* published.

    There is no ``FINAL`` or ``PUBLISHED`` state, and that absence is the
    point. Document 2.12: "the blueprint/plan is locked before work begins;
    validation is a hard gate; the human approves before publish." Passing
    validation is what earns a document the right to be *seen* by the faculty
    -- never the right to have been accepted by them. Lab 3 has no human gate,
    so a state called "final" would be a state this lab cannot honestly
    produce, and a type that permits it is an invitation to invent one later.
    Lab 7 implements the gate and will earn any stronger state.
    """
    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
```

### `SkillSpec.failure_value` — no fabricated values in the audit trail

```python
def failure_value(self) -> Any:
    """The `value` a failed call carries, or None if none is meaningful."""
    try:
        return self.output_model()
    except ValidationError:
        return None
```

A skill that failed produced no value, and `None` is the honest representation.
`Document()` would be a usable "nothing rendered yet" value because every field
has a default, but `LockedPlan()` is not, because a blueprint with no course
and no scope is not a degraded blueprint — it is not a blueprint. Inventing
defaults for it would put a plausible-looking object into the audit trail, which
is the opposite of what a ledger is for.

---

## d) Setup and run

### Environment (already provisioned)

```powershell
# The venv lives OUTSIDE OneDrive. A venv inside a synced folder fails
# pip install with [WinError 32].
$py = "C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"
Set-Location "C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
```

No new dependencies. Lab 3 uses only what Labs 1–2 already required
(`pydantic==2.13.5`) plus the standard library.

### Run the agent's own demo

```powershell
& $py -m course_organizer.labs.lab03_skill_creation
```

Expected: six sections (the skills, the four cases, injected drift, the
templates, the ledger, the acceptance report), all acceptance checks `[PASS]`,
exit code **0**.

```
==============================================================================
6. Acceptance
==============================================================================

  [PASS] exactly the two documented skills are registered           ['format_organised', 'plan']
  [PASS] case cs-broad obeys its locked plan
  [PASS] case cs-broad document is ready for review, not published  ready_for_review
  [PASS] case cs-broad locked plan passes its integrity check       33db57242531
  [PASS] case cs-narrow obeys its locked plan
  [PASS] case cs-narrow document is ready for review, not published ready_for_review
  [PASS] case cs-narrow locked plan passes its integrity check      ba572fd23ea0
  [PASS] case db-broad obeys its locked plan
  [PASS] case db-broad document is ready for review, not published  ready_for_review
  [PASS] case db-broad locked plan passes its integrity check       bed0b69766a8
  [PASS] case db-narrow obeys its locked plan
  [PASS] case db-narrow document is ready for review, not published ready_for_review
  [PASS] case db-narrow locked plan passes its integrity check      ee290a055627
  [PASS] every case produced a different locked plan                4 of 4
  [PASS] no skill outside the documented two was invoked            format_organised, plan
  [PASS] an injected drift is detected                              6 finding(s)
  [PASS] a drifted document is never ready for review               draft
  [PASS] no document is ever marked approved or published           2.12: faculty approval is a hard stop

OVERALL: PASS
```

The notebook runs the same checks with a different set, and prints `26/26
acceptance checks passed` — including that every case rendered through all three
templates, that drift was caught across every deviation class, and that the
course-identifier scan is clean.

### Run one case, one template

```powershell
& $py -m course_organizer.labs.lab03_skill_creation --case cs-narrow --template faculty-pack
```

### Run the notebook

```powershell
& $py -m jupyter lab notebooks\lab03_skill_creation.ipynb
```

### Run the tests

```powershell
& $py -m pytest tests\test_lab03_skill_creation.py -o addopts= -v
& $py -m pytest                       # the whole suite: 204 passed
```

> `pyproject.toml` sets `addopts = "-q --strict-markers"`. An extra `-q` makes it
> `-qq` and suppresses the summary line. Use bare `python -m pytest`, or
> `-o addopts= -v` for the full verbose list.

---

## e) Test cases, expected output, and proof

**121 tests, 8 groups, 0 failures.** Full verbose log:
`docs/evidence/lab03_pytest.txt`. Notebook execution: 63 cells, 36 code, 36
executed, **0 errors** — `docs/evidence/lab03_notebook_output.txt`.

| group | tests | what it defends |
|---|---|---|
| 1. `TestStructural` | 10 | exactly two skills, distinct models, validation before the handler, ledger typing |
| 2. `TestTheLock` | 23 | immutability, deep-copy on lock, integrity, reproducibility, natural order, duplicate refusal |
| 3. `TestDrift` | 15 | all six deviation classes, deferred ≠ passed, path resolution, tampered-plan refusal |
| 4. `TestFormatSkill` | 28 | template validation, three renderers, byte-identical reruns, empty sections, failure recording |
| 5. `TestReuse` | 28 | four cases, four plans, no hard-coding, tool-computed facts |
| 6. `TestHonesty` | 8 | no fabricated execution, nothing ever "final" |
| 7. `TestNoCourseIsHardCoded` | 6 | R8, enforced by AST scan |
| 8. `TestLimitations` | 3 | the documented gaps, asserted so they cannot quietly close |

Counts are `pytest --collect-only` totals, so two parametrized tests appear once
in their class and twice in the sum. Reproduce with:

```powershell
& $py -m pytest tests\test_lab03_skill_creation.py -o addopts= --collect-only -q
```

### Selected cases, with the output they produce

**E1 — the lock is real (`TestTheLock::test_a_locked_plan_cannot_be_edited_through_its_lists`)**

```python
with pytest.raises(AttributeError):
    locked.obligations.append("x")
with pytest.raises(AttributeError):
    locked.units.append("x")
```

Expected: passes. `frozen=True` would have allowed the first line.

**E2 — locking is a copy (`TestTheLock::test_editing_the_draft_after_locking_does_not_change_the_lock`)**

```python
draft.units = draft.units + ("99",)
draft.course_code = "HACKED"
assert "99" not in plan.units
assert plan.plan_sha256 == locked.plan_sha256
```

Expected: passes. Observed: `plan units: ('1','2')`, `plan course: TEST1`, `hash unchanged: True`.

**E3 — a timestamp cannot change the hash (`TestTheLock::test_a_per_run_timestamp_cannot_change_the_hash`)**

```python
stamped = inputs.model_copy(update={"verified_facts": {
    **inputs.verified_facts, "computed_at": "2099-12-31T23:59:59+00:00"}})
assert expand(stamped).lock().plan_sha256 == expand(inputs).lock().plan_sha256
```

Expected: passes. This is why `VOLATILE_FACT_KEYS` exists.

**E4 — tampering is caught (`TestTheLock::test_a_tampered_plan_fails_its_own_integrity_check`)**

```python
tampered = LockedPlan.model_construct(
    **{**locked.model_dump(), "units": ("1", "2", "3")}
)
with pytest.raises(SkillError) as exc:
    tampered.recompute_sha256()
assert exc.value.code == "plan_tampered"
```

Observed in the notebook on a real case plan:

```
recorded  : 9beae9c3cb3f4e67451e92315f39a677eba60db433391a85ab58fad4634c06be
recomputed: 9beae9c3cb3f4e67451e92315f39a677eba60db433391a85ab58fad4634c06be
match     : True

tampered (units 1, 2 -> 1, 2, 3), integrity check re-run:
  SkillError[plan_tampered]: locked plan CS201 fails its own integrity check.
  recorded 9beae9c3cb3f4e67..., recomputed f01a1b0bf016878f...
```

**E5 — deferred is not passed (`TestDrift::test_deferred_obligations_are_counted_not_passed`)**

```python
assert report.obligations_deferred == len(locked.obligations_of(KIND_VERIFY_STEP))
assert report.obligations_checked + report.obligations_deferred == len(locked.obligations)
assert report.coverage_is_complete is False
```

Observed on `cs-broad`: `10 checked, 17 deferred`.

**E6 — path resolution (`TestDrift::test_a_dotted_fact_path_resolves_into_nested_verified_facts`)**

```python
assert resolve_fact(locked.verified_facts, "marks_by_assignment.A-1") == 40.0
assert conforming.check_against(locked).conforms
```

**E7 — injected drift, all classes (`TestReuse::test_injected_drift_covers_every_deviation_kind`)**

```python
kinds = {finding.kind for finding in agent.inject_drift(agent.run(CASES[0])).drift.findings}
assert kinds == {"missing", "unexpected", "value_mismatch"}
```

Observed, from the CLI:

```
kind            obligation                        subject                detail
------------------------------------------------------------------------------------------------------------------------------------------------
missing         OBL-cover_unit/5                  5                      unit 5 is in the locked plan's scope but was not covered
missing         OBL-distribute_assignment/ASG-03  ASG-03                 assignment ASG-03 is in the locked plan's scope but was not distributed
missing         OBL-track_milestones/course       course                 the locked plan requires milestone tracking but the result emitted none
unexpected      -                                 unit-99                unit unit-99 was covered but is not in the locked plan's scope
unexpected      -                                 ASG-99                 assignment ASG-99 was distributed but is not in the locked plan's scope
value_mismatch  -                                 total_available_marks  result reports 'total_available_marks'=999.0 but the tool verified 65.0
```

`unit-99` and `ASG-99` are ids that exist in no store, so an `unexpected` finding
cannot be a mistyped real id. `missing` findings name the obligation they
violate; `unexpected` and `value_mismatch` name a key that *should not be there*,
so there is no obligation to point at and the column is `-`.

**E8 — the acceptance criterion (`TestReuse`)**

```python
proof = SkillAgent(registry).prove_reuse()
assert proof.distinct_plan_hashes == len(proof.runs)      # 4 of 4
assert proof.document_count == len(CASES) * len(TEMPLATE_SPECS)   # 4 x 3 = 12
assert set(proof.skill_names_used) <= set(SKILL_NAMES)
assert proof.all_conform
```

Observed:

```
REUSE: the same two skills, four cases, no code changes
case       course   units  asg  obligations  plan sha256   doc status
--------------------------------------------------------------------
cs-broad   CS201    5      3    27           33db57242531  ready_for_review
cs-narrow  CS201    1      1    20           ba572fd23ea0  ready_for_review
db-broad   DBMS402  5      3    27           bed0b69766a8  ready_for_review
db-narrow  DBMS402  2      2    22           ee290a055627  ready_for_review

distinct locked plans : 4 of 4 (one per case, so each really did differ)
skills invoked        : format_organised, plan
documents rendered    : 12
every case conforms   : yes
```

**E9 — the pitfall, enforced (`TestNoCourseIsHardCoded::test_the_skills_package_contains_no_course_identifier`)**

```python
offenders = []
for path in sorted(SKILLS_DIR.glob("*.py")):
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    for literal in FORBIDDEN_LITERALS:
        if literal in source:
            offenders.append(f"{path.name}: {literal}")
assert offenders == []
```

Observed from the notebook:

```
scanning C:\...\agentic-course-organizer\src\course_organizer\skills for 7 forbidden literals

  __init__.py          3795 bytes  clean
  base.py             22694 bytes  clean
  format_skill.py     33217 bytes  clean
  plan_skill.py       48622 bytes  clean

offenders: none
```

**This test found three real violations during the build.** The offenders were
not logic — they were illustrative examples in docstrings: a document id
`DOC-CS201-faculty-pack-...`, and `ASG-01` / `ASG-02` used in a worked example
of natural sorting. That is precisely why the scan reads source text rather than
AST constants: a docstring is where a "temporary" constant survives a deadline.
The examples were rewritten to `CSxxx` / `ASG-X1`.

**E10 — no fabricated execution (`TestHonesty`)**

```python
for run in SkillAgent(registry).prove_reuse().runs:
    assert run.result.completed_steps == ()

clean = SkillAgent(registry).run(CASES[0])
assert "[ ] S1:" in clean.document.body
assert "0 ran, " in clean.document.body
assert checks["pending"] == checks["total"] > 0
assert {row["state"] for row in checks["checks"]} == {"pending"}
```

Observed in the rendered document:

```
## Acceptance Checks

[ ] S1: all three sources are read via the connector, none directly
[ ] S2: every in-scope unit resolves to exactly one current version
...
[ ] S11: only faculty-approved results are published

0 ran, 17 pending - the locked blueprint carries them forward to the lab that executes the steps
```

**E11 — no stronger status exists (`TestHonesty::test_there_is_no_final_status_to_reach`)**

```python
assert {s.value for s in DocumentStatus} == {"draft", "ready_for_review"}
assert not hasattr(DocumentStatus, "FINAL")
assert not hasattr(DocumentStatus, "PUBLISHED")
```

**E12 — the skills package does no IO (`TestNoCourseIsHardCoded`)**

An AST scan over every `Import` / `ImportFrom` in `skills/` fails on
`os`, `io`, `pathlib`, `open`. A skill that opened a file would bypass the
connector boundary Lab 2 exists to enforce, and there would be no ledger entry
for it.

---

## f) Common errors and how they were fixed

Every one of these occurred during this build. They are listed because each is a
trap the next lab will meet too.

### F1 — `SkillContractError` was swallowed by its own `except`

The contract check originally sat **inside** the handler's `try`. A skill that
returned the wrong type therefore produced a *recorded failure* — identical, in
the ledger, to a handler that raised for an ordinary reason. The bug was
invisible: nothing crashed, and the audit log said "failed" for the wrong cause.

**Fix:** move step 4 outside the `try`. A type mismatch means the skill is
broken; it is raised, never recorded.

### F2 — `model_construct()` fabricated broken objects on failure

The first design claimed "every output model in this package is
default-constructible" and called `output_model()` when a handler raised. It was
**not true**: `LockedPlan` and `Document` both have required fields, so a handler
exception became a `ValidationError` about a missing `course_code`, raised far
from the cause. `test_a_handler_that_raises_is_recorded_not_crashed` failed with
exactly that.

**Fix:** `SkillSpec.failure_value()` tries the constructor and returns `None` if
it cannot. `SkillResult.value` is now `Optional`, and `unwrap()` — which raises
`handler_failure` — is documented as the only supported way to read a failed
result. The rule is *"is the empty value meaningful"*, not *"always None"*, and
a synthetic spec in the test suite proves both branches.

### F3 — the grounded-fact check could not follow a dotted path

`reported_facts` used Lab 2's dotted convention
(`marks_by_assignment.ASG-01`) while `plan.verified_facts` held the nested dict.
Every conforming case reported one spurious deviation:

```
value_mismatch  -  marks_by_assignment.ASG-01  result reports
                 'marks_by_assignment.ASG-01'=20.0, which no tool verified
                 for this plan
```

**Fix:** `resolve_fact(facts, key)` walks nested mappings by dotted path and
returns the `MISSING` sentinel when any segment is absent.

### F4 — `pydantic.ValidationError` from inside `recompute_sha256`

`LockedPlan.model_construct(**{**locked.model_dump(), ...})` leaves plain
**dicts** in `obligations`, because `model_dump()` serialises nested models and
`model_construct` does not re-validate. `recompute_sha256` then raised
`AttributeError: 'dict' object has no attribute 'kind'`.

This is the worst-shaped bug of the build: the integrity check **crashed** on
exactly the malformed objects it exists to detect, so the adversary it was
written for got an `AttributeError` instead of a tamper report — and the
`AttributeError` looked like an unrelated bug.

**Fix:** `_obligation_field(obligation, name)` reads a field from a model *or* a
mapping, and `_obligation_sort_key` / `_obligation_payload` go through it.

### F5 — the content payload was built twice, and diverged

`PlanDraft.content_payload()` and `LockedPlan.recompute_sha256()` each built the
hash dict, with a comment noting they must not drift apart. Then `notes` was
added to one and the other kept passing — a check that reports "no tampering"
while hashing something other than the whole plan.

**Fix:** one `_content_payload(...)` function, called by both.

### F6 — `verified_facts["computed_at"]` made every run hash differently

Lab 2 records a `computed_at` timestamp. Hashing it meant no two runs of an
identical case produced the same `plan_sha256`, and the drift check could not
tell a changed plan from a clock ticking.

**Fix:** `VOLATILE_FACT_KEYS` + `stable_facts()`. A separate instance of the same
class of bug arrived from the other side: `locked_at` was initially *inside* the
hash payload, so the lock timestamp broke reproducibility the same way.

### F7 — obligation order was string order, so `S10` preceded `S2`

Plain `sorted()` on `obligation_id` put the document's step S10 before S2. Since
the order feeds the content hash, this also meant two runs that listed the same
scope in a different order could hash differently.

**Fix:** `_sort_key` does natural ordering — any run of digits sorts numerically,
text between them sorts as text — and `_obligation_sort_key` composes
`(kind, _sort_key(subject), criterion)`. Verified:

```
input  : ['S10','S1','unit-10','unit-9','ASG-10','ASG-2','ASG-01','U1b',
          'U1a','2','10','1','9','unit-3','S2','S11','S3']
sorted : ['1','2','9','10','ASG-01','ASG-2','ASG-10','S1','S2','S3','S10',
          'S11','U1a','U1b','unit-3','unit-9','unit-10']
```

Note the deliberate part: `ASG-2` sorts before `ASG-10` because the comparison
is numeric, and `ASG-01` still precedes `ASG-2` because the digit runs are
*equal* and the remainder (`-01` vs `-2`) then falls back to text. A plan's
obligation ids are only ever emitted by `obligation_id()`, which pads
consistently, so this tiebreak never has to arbitrate a real case — it is there
so the ordering is total and deterministic rather than merely usually right.

### F8 — `assemble()` claimed every Lab 1 step had run

The first `WorkspaceResult` set `completed_steps` to every step in the plan,
which would have rendered a document with a row of green ticks over work Lab 3
never did.

**Fix:** `completed_steps=()`, plus `DEFERRED_KINDS = {KIND_VERIFY_STEP}` so the
criteria those steps own are held out of the verdict and render as `pending`.
A test asserts `completed_steps == ()` for all four cases.

### F9 — a conforming document was marked `final`

The acceptance check exposed this: the code required
`run_result.document.status == "draft"`, and the *conforming* case reported
`final`. Beyond the check, the semantics were wrong. §2.12 requires faculty
approval before publish, and Lab 3 cannot grant it. "No drift" means *ready for
review*, not *final*.

**Fix:** `DocumentStatus` with two members and no third.
`is_ready_for_review` replaces `is_final`; `awaiting_approval` is a property
that is always `True` at this lab, so no caller has to know that and Lab 7 has
one obvious place to change it. The approval section reports the **live** status
rather than a fixed "DRAFT" string, which would have been a lie the moment a
clean document existed.

### F10 — the `include_milestones` override was computed and then dropped

`build_plan_inputs` appended `"include_milestones=False set by case scope, not
asked by the clarify loop"` to a local list, then never passed it to
`PlanInputs`. The record was built correctly and went nowhere. Meanwhile
Lab 1's own note said `include_milestones: assumed true` — so the audit trail
contradicted the plan it was auditing.

**Fix:** `PlanInputs.notes` → `PlanDraft.notes` → `LockedPlan.notes` →
rendered in the document header as `Assumed : ...`, and covered by the content
hash so stripping one is detectable. Observed:

```
Assumed : include_milestones: assumed true (the doc lists milestone tracking as a standard work stream)
Assumed : include_milestones=False set by case scope, not asked by the clarify loop
```

Both facts are now recorded, which is the point: neither is a contradiction when
read as a sequence.

### F11 — three course constants survived in docstrings

See E9 above.

### F12 — `describe()` rendered `<class '...'>` and then a literal `Ellipsis`

The skill index printed raw annotations from `__annotations__`, then rendered
`...` (the `Field(...)` sentinel) as the text "Ellipsis".

**Fix:** `_annotation_text`, which maps the `Ellipsis` sentinel to the field's
default rather than passing it through.

### F13 — dead code reached the file

- `__mro__[1]` used to reach `SkillError` without importing it — replaced with a
  real import.
- `_outline_steps`, which existed only to feed the fabricated
  `completed_steps` in F8 — deleted.
- `prove_reuse` contained `self.render_all_templates(run_result) and
  (tuple(TEMPLATE_SPECS))` — a meaningless expression that happened to be
  truthy. `ReuseProof.templates_tried` (a count) was replaced with
  `documents: dict[str, dict[str, Document]]`, so a test can open any one
  document and assert on its content.

### F14 — the test file had two syntax errors of my own making

`assert x := f()` and a trailing comma after an `assert` message. Both caught at
collection. Recorded because the lesson is mundane and worth stating: a 1,460-line
test file gets written faster than it gets read, and pytest's collection pass is
the cheapest review there is.

---

## g) Requirement coverage

| # | Doc requirement | Status | Evidence |
|---|---|---|---|
| R1 | Reusable, schema-typed skills | ✅ met | `SkillSpec(input_model, output_model, handler)`; `SkillRegistry.invoke` validates before, checks after. `TestStructural`, 10 tests |
| R2 | A plan/blueprint skill **and** a formatting skill, both reusable | ✅ met | `SKILL_NAMES == ("plan", "format_organised")`. `test_registry_holds_exactly_the_two_documented_skills` |
| R3 | Inputs: parsed inputs; template spec | ✅ met | `PlanInputs`; `FormatInputs(result, plan, template: TemplateSpec)`. Notebook §4, §6 |
| R4 | Output: a locked plan object; a formatted shell | ✅ met (shell honestly) | `LockedPlan`, `Document`. 17 obligations deferred; `0 ran, 17 pending` rendered. `TestHonesty`, `TestLimitations` |
| R5 | Planning and assembly emerge as skills | ✅ met | `skills/plan_skill.py`, `skills/format_skill.py` import no lab module — `test_the_skill_package_does_not_import_the_lab_modules` |
| R6 | Two callable skills with schemas, invoked by the agent | ✅ met | `SkillAgent.run` invokes both per case; `SkillLedger` records all 9 calls in the CLI run. `TestReuse`, 28 tests |
| R7 | **Accept:** same skill, two different cases, no code changes | ✅ exceeded | **Four** cases (2 courses × 2 scope shapes), four distinct plan hashes, 12 documents, zero changes to `skills/`. `TestReuse`, notebook §8 |
| R8 | **Pitfall:** keep skills case-agnostic and typed | ✅ enforced | AST scan of `skills/` for 7 course literals + import scan for lab modules + IO scan. `TestNoCourseIsHardCoded`, 6 tests. Found 3 real violations (F11) |
| R9 | `skill plan(inputs) -> Plan: return expand(inputs).lock()` | ✅ met, verbatim | `DOC_PSEUDOCODE`; `test_doc_pseudocode_is_recorded_verbatim`. `expand()` and `.lock()` are the two halves of one handler |
| R10 | §2.12: *"Lock the plan (Lab 3/7)"* | ✅ met | `LockedPlan` (frozen, hashed, self-verifying) **and** `check_against` → `DriftReport`. `TestDrift`, 15 tests, 6 deviation classes |
| R11 | §2.12: lock before work; validation a hard gate; human approves before publish | ✅ met in the type | `DocumentStatus` has no `FINAL`/`PUBLISHED`; `awaiting_approval` always `True`. `test_there_is_no_final_status_to_reach` |
| R12 | Glossary: blueprint = *"the locked specification the pipeline obeys"* | ⚠️ **partial, documented** | The lock and the comparison exist; **nothing yet obeys the plan in anger** — Lab 3 assembles its own result. `TestLimitations::test_the_locked_plan_is_not_yet_obeyed_by_a_real_pipeline` is written to **fail when Lab 7 lands** |
| R13 | §2.7's four validation checks appear as obligations | ✅ carried forward | All four are `verify_step` obligations, quoted from Lab 1's acceptance criteria: *"no draft grade exceeds the rubric maximum"*, *"late submissions are flagged accurately"*, *"every submission has a similarity score attached"*, *"no grade or feedback is published without faculty approval"*. Deferred, not passed — Lab 7 settles them |

### Honest gaps

| gap | why | settled by |
|---|---|---|
| `verify_step` obligations (17 of 27 on a broad plan) are **deferred**, not satisfied | Lab 3 locks a blueprint and renders a shell; it runs no pipeline step. Showing green ticks would be a lie in one direction, red crosses a lie in the other | Lab 7 |
| The drift check is exercised against a result **Lab 3 assembles itself** | this proves the check *runs* and *catches* deviations, not that a real pipeline obeys a plan | Lab 7 |
| `ground_facts` covers only the numbers Lab 2 computed | no submissions, similarity scores or draft grades exist yet | Labs 4–6 |
| No document is ever `final` | §2.12: the human approves before publish, and Lab 3 has no gate | Lab 7 |
| `locked_at` is excluded from the hash | a lock time is a fact about the locking, not the plan's content | by design |

---

## h) Report and documentation text

### Lab 3 report

**Objective.** Implement the document's two skills — a plan/blueprint skill and
a formatting skill — so that repeatable capability is packaged as typed,
reusable units rather than written inline per case.

**What was built.** Two schema-typed skills in a standalone package
(`src/course_organizer/skills/`), each declaring its input model, output model
and handler, each invoked through a registry that validates input before the
handler runs, checks the returned type after, and appends every call — success
or failure — to an append-only ledger. The `plan` skill implements the
document's pseudocode exactly: `expand(inputs)` lowers parsed inputs into
explicit, checkable obligations, and `.lock()` commits the draft to a frozen,
content-hashed blueprint. The `format_organised` skill renders a produced result
through a `TemplateSpec` — sections and body style as *data* — across three
renderers, and annotates any deviation from the locked plan.

**What made it non-trivial.** Locking is easy to claim and easy to get wrong.
Pydantic's `frozen=True` blocks attribute rebinding but **not** `list.append`, so
a "locked" plan built on list fields is still editable, silently. Every
collection on `LockedPlan` is therefore a `tuple`. Beyond that, the lock is a
**copy**: `lock()` serialises rather than aliases, so nothing the draft does
afterwards can rewrite history inside the locked plan. And the plan **hashes its
own content**, with two deliberate exclusions (`computed_at` and `locked_at`) so
that two runs of an identical case produce the same `plan_sha256` — without
which a drift report could never distinguish a changed plan from a clock
reading.

**Drift detection.** §2.12 names *"Lock the plan"* as the plan-drift mitigation
for this lab, and a lock nobody compares against is a document, not a control.
`WorkspaceResult.check_against(plan)` reports six classes of deviation —
`missing`, `unexpected`, `value_mismatch`, `ungrounded_fact`, `unknown kind`, and
incompleteness. An obligation kind the checker does not implement **fails**,
because a kind nothing can evaluate is an obligation nobody enforces. Grounding
has to resolve dotted paths, since Lab 2 records per-assignment marks as a
nested mapping but names individual values with a dotted key. And the 17
`verify_step` obligations that Lab 3 cannot settle are reported as **deferred**,
never as passed: the document says `0 ran, 17 pending` and
`coverage_is_complete` is `False`, so a clean verdict can never be mistaken for
a full one.

**The acceptance criterion.** *"The same skill works for two different cases
without code changes."* A test that calls a skill once and checks it did not
raise cannot establish this — a skill containing `if course == "CS201"` passes
it. The criterion was therefore split into four falsifiable parts, and the demo
widened from two cases to four (two courses × two scope shapes) so that a skill
handling only "the whole course" fails on `cs-narrow` and a hard-coded course
fails on `db-*`. The strongest part is structural: the skills package is parsed
and scanned for any course identifier in **any** file, **including docstrings and
comments**, which is where a temporary constant survives a deadline. That scan
found three real violations in this build, all in illustrative docstrings.

**§2.12 compliance, by construction.** The document's own hard stops are types,
not conventions. `DocumentStatus` has two members and deliberately no third: a
clean document is `ready_for_review`, never `final` or `published`, because
passing validation earns a document the right to be *seen* by the faculty and
never the right to have been *accepted* by them. `awaiting_approval` is always
`True` at this lab. `TemplateSpec` refuses to be constructed without an
`approval` section, so the gate cannot be forgotten by whoever writes the next
template. Every number in a rendered document resolves to a fact a tool verified,
re-checked by the `ground_facts` obligation.

**Conclusion.** The lab's acceptance criterion is met and met strictly: the same
two skills, unmodified, produce four different locked plans, twelve documents
across three templates, with every case conforming to its own plan. **121 tests
pass; the full suite is 204.** The notebook runs 36 code cells with zero errors.
What the lab does not do is stated rather than glossed: it runs no pipeline step,
so its 17 step-verification obligations are deferred, and the result it checks
the plan against is one it assembled itself. Both gaps are recorded as tests
that are written to fail when Lab 7 closes them.
