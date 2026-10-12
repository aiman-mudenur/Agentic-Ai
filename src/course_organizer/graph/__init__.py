"""Labs 7 and 8 -- the agentic node graph and its parallel swarm.

Doc 2.10's Lab 7 deliverable (line 690): *"An executable graph producing a first
end-to-end draft"*, accepted when *"one command yields a validated organised
course workspace (notes, assignments, grades) draft end to end."*

Doc 2.10's Lab 8 deliverable (line 704): *"A fan-out/fan-in that processes all
units and merges by alignment to the plan"*, accepted when *"Parallel run
matches or beats sequential quality at lower wall-clock time."*

Nine modules, in dependency order:

============  ==========================================================
:mod:`state`  the shared typed state and the doc's own constants
:mod:`context` per-run registries, kept out of the state because the state
              has to survive a SQLite round-trip
:mod:`validation` doc 2.7's four checks and the diagnosis
:mod:`specialists` the six Phase-A agents that run inside ``workers``
:mod:`escalation` doc 2.12 line 825's escalation with a diagnosis
:mod:`nodes` the eight nodes, including the real ``interrupt()`` gate
:mod:`builder` the topology -- chain, back-edge and bound
:mod:`runner` one command in, one result out
:mod:`swarm` Lab 8's fan-out/fan-in: a second topology that reuses the eight
              nodes and inserts a ``Send`` fan-out before ``validate``
============  ==========================================================

Nothing here writes durable audit records. That is Lab 6's job, deliberately:
this lab is about control flow, and two subsystems owning the audit log would
mean two definitions of what "audited" means. Lab 8 is additive: it does not
edit Lab 7's topology, nodes, runner or tests.
"""

from __future__ import annotations

from course_organizer.graph.builder import (
    DOC_CHAIN,
    GATE_ROUTES,
    RECURSION_BACKSTOP,
    VALIDATE_ROUTES,
    build_graph,
    compiled_nodes,
    describe_topology,
    expected_executions,
    graph_config,
    graph_edges,
    inspect_graph,
    node_execution_count,
    route_after_gate,
    route_after_validate,
    topology_lines,
)
from course_organizer.graph.context import (
    SPECIALIST_INTENTS,
    RunContext,
    build_run_context,
    default_output_dir,
    repo_root,
)
from course_organizer.graph.escalation import (
    DEFAULT_ESCALATION_TARGET,
    build_escalation,
)
from course_organizer.graph.nodes import (
    TEMPLATE_AUDIENCE,
    TEMPLATE_ID,
    TEMPLATE_TITLE,
    build_nodes,
)
from course_organizer.graph.specialists import (
    SIMILARITY_FLAG_THRESHOLD,
    SPECIALIST_NAMES,
    SPECIALISTS,
    SpecialistResult,
    run_specialists,
)
from course_organizer.graph.runner import (
    CHECKPOINTED_TYPES,
    CHECKPOINT_FILENAME,
    GraphRun,
    checkpoint_path,
    checkpoint_serializer,
    load_gate_request,
    open_checkpointer,
    resume_run,
    run_pipeline,
)
from course_organizer.graph.state import (
    APPROVING_DECISIONS,
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_OVERRIDE,
    DECISION_REFUSE,
    DOC_BACK_EDGE,
    DOC_CHECKS,
    DOC_NODE_CHAIN,
    MAX_ATTEMPTS,
    ROUTE_APPROVED,
    ROUTE_ESCALATE,
    ROUTE_PASS,
    ROUTE_REFUSED,
    ROUTE_RETRY,
    CourseState,
    CourseStatus,
    DraftGradeRow,
    Escalation,
    FacultyDecision,
    GateRequest,
    ValidationOutcome,
    append,
)
from course_organizer.graph.validation import (
    CHECKS,
    RUBRIC_TOLERANCE,
    CheckContext,
    check_draft_grade_within_rubric_maximum,
    check_late_submissions_flagged_accurately,
    check_no_grade_published_without_faculty_approval,
    check_similarity_score_attached,
    explain_failure,
    missing_checks,
    next_route,
    summarise,
    validate,
)
from course_organizer.graph.swarm import (
    KIND_SUBMISSION,
    KIND_UNIT,
    PRIORITY_SUBMISSION,
    PRIORITY_UNIT,
    SUBMISSION_STRATEGIES,
    SWARM_MAX_CONCURRENCY,
    SWARM_OUTPUT_ROOT,
    UNIT_STRATEGIES,
    Alignment,
    SwarmState,
    WorkItem,
    alignment_to_plan,
    build_swarm_graph,
    build_work_items,
    default_swarm_root,
    inspect_swarm,
    merge_results,
    plan_scope,
    resume_swarm,
    run_swarm,
    swarm_nodes,
    timing_summary,
)

__all__ = [
    "APPROVING_DECISIONS",
    "CHECKPOINTED_TYPES",
    "CHECKS",
    "DEFAULT_ESCALATION_TARGET",
    "DECISION_APPROVE",
    "DECISION_EDIT",
    "DECISION_OVERRIDE",
    "DECISION_REFUSE",
    "DOC_BACK_EDGE",
    "DOC_CHAIN",
    "DOC_CHECKS",
    "DOC_NODE_CHAIN",
    "GATE_ROUTES",
    "MAX_ATTEMPTS",
    "RECURSION_BACKSTOP",
    "RUBRIC_TOLERANCE",
    "ROUTE_APPROVED",
    "ROUTE_ESCALATE",
    "ROUTE_PASS",
    "ROUTE_REFUSED",
    "ROUTE_RETRY",
    "SIMILARITY_FLAG_THRESHOLD",
    "SPECIALIST_INTENTS",
    "SPECIALIST_NAMES",
    "SPECIALISTS",
    "TEMPLATE_AUDIENCE",
    "TEMPLATE_ID",
    "TEMPLATE_TITLE",
    "VALIDATE_ROUTES",
    "CheckContext",
    "CourseState",
    "CourseStatus",
    "DraftGradeRow",
    "Escalation",
    "FacultyDecision",
    "GateRequest",
    "RunContext",
    "SpecialistResult",
    "ValidationOutcome",
    "CHECKPOINT_FILENAME",
    "GraphRun",
    "append",
    "build_escalation",
    "build_graph",
    "build_nodes",
    "build_run_context",
    "checkpoint_path",
    "checkpoint_serializer",
    "compiled_nodes",
    "load_gate_request",
    "open_checkpointer",
    "resume_run",
    "run_pipeline",
    "check_draft_grade_within_rubric_maximum",
    "check_late_submissions_flagged_accurately",
    "check_no_grade_published_without_faculty_approval",
    "check_similarity_score_attached",
    "default_output_dir",
    "describe_topology",
    "explain_failure",
    "expected_executions",
    "graph_config",
    "graph_edges",
    "inspect_graph",
    "missing_checks",
    "next_route",
    "node_execution_count",
    "repo_root",
    "route_after_gate",
    "route_after_validate",
    "run_specialists",
    "summarise",
    "topology_lines",
    "validate",
    # -- Lab 8: the parallel swarm ------------------------------------------
    "KIND_SUBMISSION",
    "KIND_UNIT",
    "PRIORITY_SUBMISSION",
    "PRIORITY_UNIT",
    "SUBMISSION_STRATEGIES",
    "SWARM_MAX_CONCURRENCY",
    "SWARM_OUTPUT_ROOT",
    "UNIT_STRATEGIES",
    "Alignment",
    "SwarmState",
    "WorkItem",
    "alignment_to_plan",
    "build_swarm_graph",
    "build_work_items",
    "default_swarm_root",
    "inspect_swarm",
    "merge_results",
    "plan_scope",
    "resume_swarm",
    "run_swarm",
    "swarm_nodes",
    "timing_summary",
]