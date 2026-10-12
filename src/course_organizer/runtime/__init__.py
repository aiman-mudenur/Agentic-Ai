"""Lab 6: the governed runtime -- budgets, gates, checkpoint/resume, audit log.

Doc 2.10 Lab 6 in one line:

    **Goal:** "Run the agent inside a controlled runtime with a full audit log."

and its deliverable:

    "Runtime wrapping the agent: budgets, gates, checkpoint/resume, audit log.
    Accept: Every action is logged with inputs/outputs; a crashed run resumes
    from its last checkpoint."

Six modules, in dependency order:

:mod:`~course_organizer.runtime.errors`
    The five failure types, one per kind of governed refusal, so an audit reader
    can tell a policy refusal from a capacity refusal.
:mod:`~course_organizer.runtime.budget`
    Countable resources with one ceiling each, checked *before* the action.
:mod:`~course_organizer.runtime.audit`
    Every action with its inputs **and** outputs, hash-chained and append-only.
:mod:`~course_organizer.runtime.checkpoint`
    Resumable state in SQLite via LangGraph's ``SqliteSaver``, carrying the
    cursor, the accumulated artifacts, the budget spend *and* the audit chain
    position.
:mod:`~course_organizer.runtime.gates`
    The approval question and its three answers: approved, refused, deferred.
:mod:`~course_organizer.runtime.runtime`
    The controller that puts them in the order the document's pitfall requires.

The three the document names in one breath -- "skills (Lab 3), connector (Lab 5),
memory (Lab 4)" -- reach the runtime as :class:`~course_organizer.runtime.runtime.Step`
objects with ``kind`` set to ``skill``, ``connector`` and ``memory``. That is the
whole integration: the runtime does not know what a skill or a connector *is*,
only that each kind of work costs one unit of its own budget dimension and must
be logged with what went in and what came out.

Import here for convenience; the submodules remain importable on their own so a
lab module or a test can depend on one piece without pulling in the rest.
"""

from __future__ import annotations

from course_organizer.runtime.audit import (
    DIGEST_HEX,
    GENESIS_DIGEST,
    AuditLog,
    AuditOutcome,
    AuditRecord,
    InMemoryAuditLog,
    JsonlAuditLog,
    build_audit_record,
    canonical_json,
    chain_digest,
)
from course_organizer.runtime.budget import (
    DEFAULT_LIMITS,
    Budget,
    BudgetDimension,
    BudgetLedger,
    BudgetSnapshot,
)
from course_organizer.runtime.checkpoint import (
    CHECKPOINT_NS,
    WRITE_SEQ_WIDTH,
    CheckpointRef,
    CheckpointStore,
    InMemoryCheckpointStore,
    RunState,
    SqliteCheckpointStore,
    sqlite_path,
)
from course_organizer.runtime.errors import (
    AuditIntegrityError,
    BudgetExceeded,
    CheckpointUnavailable,
    GateRefused,
    ResumeDivergence,
    RuntimeBoundaryError,
)
from course_organizer.runtime.gates import (
    APPROVAL_OUTCOMES,
    UNANSWERED,
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    CallbackGate,
    DenyAllGate,
    GateScriptExhausted,
    PreapprovedGate,
    RecordingGate,
    ScriptedGate,
)
from course_organizer.runtime.runtime import (
    DEFAULT_GATE,
    DEFAULT_PRINCIPAL,
    HUMAN_AUTHORITY,
    KIND_DIMENSION,
    RunReport,
    Runtime,
    Step,
    StepContext,
    StepOutcome,
    StepStatus,
    plan_digest,
)

__all__ = [
    # errors
    "AuditIntegrityError",
    "BudgetExceeded",
    "CheckpointUnavailable",
    "GateRefused",
    "ResumeDivergence",
    "RuntimeBoundaryError",
    # budget
    "Budget",
    "BudgetDimension",
    "BudgetLedger",
    "BudgetSnapshot",
    "DEFAULT_LIMITS",
    # audit
    "AuditLog",
    "AuditOutcome",
    "AuditRecord",
    "DIGEST_HEX",
    "GENESIS_DIGEST",
    "InMemoryAuditLog",
    "JsonlAuditLog",
    "build_audit_record",
    "canonical_json",
    "chain_digest",
    # checkpoints
    "CHECKPOINT_NS",
    "CheckpointRef",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "RunState",
    "SqliteCheckpointStore",
    "sqlite_path",
    "WRITE_SEQ_WIDTH",
    # gates
    "APPROVAL_OUTCOMES",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "CallbackGate",
    "DenyAllGate",
    "GateScriptExhausted",
    "PreapprovedGate",
    "RecordingGate",
    "ScriptedGate",
    "UNANSWERED",
    # runtime
    "DEFAULT_GATE",
    "DEFAULT_PRINCIPAL",
    "HUMAN_AUTHORITY",
    "KIND_DIMENSION",
    "RunReport",
    "Runtime",
    "Step",
    "StepContext",
    "StepOutcome",
    "StepStatus",
    "plan_digest",
]