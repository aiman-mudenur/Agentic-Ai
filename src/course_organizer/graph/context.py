"""Per-run dependencies for the graph's nodes.

LangGraph checkpoints the **state**, not the functions that read it. That one
fact drives this module's existence.

A node is a closure over a :class:`RunContext`, and the context holds a tool
registry, a skill registry and a run id -- none of which belong in
:class:`~course_organizer.graph.state.CourseState`, because a registry is a live
object and the state must survive a round-trip through SQLite. Putting a
registry in the state would mean every checkpoint carried a reference to
something that cannot be serialised, and resume would break on the first
interrupt.

The cost of that choice is that a resumed run needs its context rebuilt. That is
cheap here -- :func:`build_run_context` is deterministic given a run id -- and
the alternative (a registry in the state) does not work at all. So the context
is built fresh on every ``invoke``, including the one that resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from course_organizer.skills import (
    LabStep,
    SkillRegistry,
    build_default_skill_registry,
)
from course_organizer.tools import (
    AssignmentQuery,
    NotesQuery,
    ScopeQuery,
    SubmissionHistoryQuery,
    ToolContext,
    ToolLedger,
    ToolRegistry,
    build_default_registry,
    register_read_submission_records,
)


@dataclass(frozen=True)
class RunContext:
    """Everything a node needs that is not state.

    Attributes:
        run_id: Ties the tool and skill ledgers to one run, so Lab 6's audit
            log and this graph refer to the same run when both are used.
        tools: The tool registry, with ``read_submission_records`` registered.
        skills: The skill registry, providing ``plan`` and ``format_organised``.
        tool_context: Passed to every ``invoke`` so the tool ledger records it.
        course_code: The one course this run organises.
        max_attempts: The bound on the back-edge, from doc 2.12 line 825.
        human_authority: Doc 2.2 line 467 names the human at the gate.
        output_dir: Where ``publish`` writes the published workspace. Part of
            the context rather than the state because it is a filesystem path
            fixed for the run, and the state must stay serialisable.
    """

    run_id: str
    tools: ToolRegistry
    skills: SkillRegistry
    tool_context: ToolContext
    course_code: str
    max_attempts: int
    human_authority: str
    output_dir: Path

    # -- query factories -------------------------------------------------
    # Thin wrappers rather than the raw query classes at every call site, so
    # that changing a query's required fields is one edit here instead of six
    # inside the specialists, and so a test can see what each specialist asked
    # for without importing the tools package.

    def notes_query(self, *, course_code: str, units: list[str]) -> NotesQuery:
        """Every version of every note for the course.

        ``current_only=False`` on purpose: doc 2.12 line 830 requires clear
        version history for notes and materials.
        """
        return NotesQuery(
            course_code=course_code, units=units, current_only=False
        )

    def scope_query(
        self, *, course_code: str, units: list[str], assignment_ids: list[str]
    ) -> ScopeQuery:
        """The scope check: which units and assignments the run is over.

        Empty lists mean "everything in the course", which is what an empty
        ``units_requested`` asks for. Asking for all of it explicitly is clearer
        than omitting the field, because these models are ``extra="forbid"``
        and a missing key is a different request from an empty one.
        """
        return ScopeQuery(
            course_code=course_code, units=units, assignment_ids=assignment_ids
        )

    def assignment_query(self, *, course_code: str) -> AssignmentQuery:
        """Assignment briefs and rubrics for the course."""
        return AssignmentQuery(course_code=course_code)

    def submission_query(
        self, *, course_code: str, statuses: list[str]
    ) -> SubmissionHistoryQuery:
        """Submission records for the course."""
        return SubmissionHistoryQuery(course_code=course_code, statuses=statuses)

    def lab_steps(self) -> tuple[LabStep, ...]:
        """The six roster steps as plan obligations.

        Doc 2.5 line 528's message map is a chain, and the ``plan`` skill turns
        named steps into ``verify_step`` obligations -- the deferred kind
        (:data:`~course_organizer.skills.DEFERRED_KINDS`). These are the steps
        the plan will hold the run to, one per specialist, in roster order,
        each criterion quoted from doc 2.4.
        """
        return tuple(
            LabStep(
                step_id=step_id,
                agent=agent,
                name=agent,
                intent=intent,
                acceptance_criteria=(criterion,),
            )
            for step_id, agent, intent, criterion in SPECIALIST_INTENTS
        )

    def with_course(self, course_code: str) -> "RunContext":
        """A copy pointed at a different course.

        Used by the runner when it is asked for a course other than the
        default, so a second run reuses the same registries instead of
        rebuilding them and double-counting nothing.
        """
        return RunContext(
            run_id=self.run_id,
            tools=self.tools,
            skills=self.skills,
            tool_context=self.tool_context,
            course_code=course_code,
            max_attempts=self.max_attempts,
            human_authority=self.human_authority,
            output_dir=self.output_dir,
        )


#: ``(step_id, agent, intent, acceptance criterion)`` per specialist, in roster
#: order. Doc 2.4 gives each agent one governing rule; each rule is quoted as
#: the acceptance criterion so the locked plan's obligations are traceable to
#: the document rather than invented.
SPECIALIST_INTENTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "w1",
        "Notes Repository Agent",
        "read the current notes for every unit and keep their version history",
        "every unit has at least one current note",
    ),
    (
        "w2",
        "Assignment Distribution Agent",
        "distribute only faculty-approved assignment briefs and rubrics",
        "no brief is distributed without a named approver",
    ),
    (
        "w3",
        "Submission Collection Agent",
        "collect submissions and derive lateness without altering timestamps",
        "no submission timestamp is altered",
    ),
    (
        "w4",
        "Plagiarism/Similarity Agent",
        "attach a similarity score and flag high cases for review, never decide",
        "every graded submission carries a similarity score",
    ),
    (
        "w5",
        "Grading Assistance Agent",
        "draft marks against the faculty-approved rubric, leaving approval to a human",
        "no draft grade exceeds the rubric maximum",
    ),
    (
        "w6",
        "Project Milestone Tracking Agent",
        "track project milestones with faculty-approved due dates",
        "every milestone has a due date and an expected deliverable",
    ),
)


def build_run_context(
    run_id: str,
    *,
    course_code: str = "CS201",
    max_attempts: int = 3,
    human_authority: str = "course_faculty",
    output_dir: Optional[Path] = None,
) -> RunContext:
    """Build the registries and the context that holds them.

    Args:
        run_id: Identifies this run in both ledgers.
        course_code: The course to organise.
        max_attempts: Bound on the ``validate -> workers`` back-edge.
        human_authority: Doc 2.2 line 467: the named human is the final
            authority. Defaults to the doc's own name for the role.
        output_dir: Where ``publish`` writes. Defaults to
            ``<repo>/out/lab07/<run_id>``.

    Returns:
        A ready context. Building it is cheap and has no side effects beyond
        creating the in-memory ledgers, so calling it again on resume is safe.
    """
    tools = register_read_submission_records(build_default_registry(run_id))
    ledger = ToolLedger(run_id=run_id)
    if output_dir is None:
        output_dir = default_output_dir(run_id)
    return RunContext(
        run_id=run_id,
        tools=tools,
        skills=build_default_skill_registry(run_id),
        tool_context=ToolContext(
            registry=tools, ledger=ledger, run_id=run_id
        ),
        course_code=course_code,
        max_attempts=max_attempts,
        human_authority=human_authority,
        output_dir=output_dir,
    )


def repo_root() -> Path:
    """The repository root, derived from this file's location.

    ``src/course_organizer/graph/context.py`` -> up four levels. Resolved from
    ``__file__`` rather than from the working directory so the graph writes to
    the same place whether it is run from the repo root, from ``src``, or from
    a test in ``tests/``.
    """
    return Path(__file__).resolve().parents[3]


def default_output_dir(run_id: str) -> Path:
    """``out/lab07/<run_id>`` -- one directory per run.

    Per-run rather than shared, because a resumed run and a fresh run of the
    same course would otherwise overwrite each other's published document, and
    "which one is the published artifact" would stop having an answer.
    """
    return repo_root() / "out" / "lab07" / run_id


__all__ = [
    "SPECIALIST_INTENTS",
    "RunContext",
    "build_run_context",
    "default_output_dir",
    "repo_root",
]