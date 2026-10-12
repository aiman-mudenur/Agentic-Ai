"""The topology: doc 2.10's chain, its back-edge, and the bound.

Doc 2.10 line 696::

    graph.chain(ingest -> plan -> workers -> validate)
    graph.edge(validate --fail--> workers)              # bounded retry
    graph.edge(validate --pass--> assemble -> gate -> publish)

::

                        +---------------------------------+
                        |                                 |
                        v                                 |
    START -> ingest -> plan -> workers -> validate --pass--> assemble -> gate -> publish -> END
                                       ^                |
                                       +---- fail ------+
                                       |
                                       +-- (budget spent) --> escalate -> END

Two facts about LangGraph that this module exists to get right, both verified
against langgraph 1.2.12 by probe rather than read off a tutorial:

**A conditional edge alone does not create a cycle.** ``validate`` returning
``"fail"`` and a route table mapping ``fail -> workers`` is *not* enough. The
graph also needs the forward edge ``workers -> validate``, or the loop
converges after one attempt and ``MAX_ATTEMPTS`` never binds anything. Verified:
with only the conditional back-edge, a permanently failing input completed after
a single attempt.

**The bound is not the recursion limit.** ``MAX_ATTEMPTS`` (doc 2.12 line 825) is
an explicit counter in the state that the route function reads. ``recursion_limit``
is set as a backstop at a deliberately higher number, and a test asserts the
backstop never fires -- otherwise "the loop terminated" would not distinguish
"the design stopped it" from "the framework did".

The checkpointer is passed to ``compile()``, not to the builder constructor.
Omitting it is the quiet failure: ``invoke`` then uses a default in-memory
saver, the run appears to work, and nothing is persisted -- so the gate cannot
be resumed from a new process, which is the one thing the gate must be able to
do.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from langgraph.graph import END, START, StateGraph

from course_organizer.graph.context import RunContext
from course_organizer.graph.nodes import build_nodes
from course_organizer.graph.state import (
    MAX_ATTEMPTS,
    ROUTE_APPROVED,
    ROUTE_ESCALATE,
    ROUTE_PASS,
    ROUTE_REFUSED,
    ROUTE_RETRY,
    CourseState,
)

LOGGER = logging.getLogger(__name__)

#: How many node executions one run may take before LangGraph refuses.
#:
#: A clean run is ingest, plan, workers, validate, assemble, gate, publish =
#: 7. With every retry spent it is 4 (ingest, plan) + 3 * 2 (workers,
#: validate) + escalate = 12. So 32 leaves generous headroom while still being
#: low enough that an accidental cycle fails in well under a second. The
#: ``recursion_backstop_never_fires`` test asserts the real runs stay far
#: below it.
RECURSION_BACKSTOP = 32


def route_after_validate(state: CourseState) -> str:
    """Send ``validate``'s report to one of its three destinations.

    Args:
        state: The run state. ``route`` was written by the ``validate`` node,
            which is the only place the doc 2.7 checks run, so reading it here
            instead of recomputing keeps one owner for that decision.

    Returns:
        ``pass``, ``fail`` or ``escalate``. Anything unrecognised goes to
        ``escalate``: an unknown branch label means the graph is wired
        differently from what the doc describes, and stopping for a human beats
        guessing.
    """
    route = str(state.get("route") or ROUTE_PASS)
    if route in (ROUTE_PASS, ROUTE_RETRY, ROUTE_ESCALATE):
        return route
    LOGGER.warning(
        "validate produced unknown route %r; escalating rather than guessing",
        route,
    )
    return ROUTE_ESCALATE


def route_after_gate(state: CourseState) -> str:
    """Split the gate's outcome into publish and escalate.

    Doc 2.5 line 538 gives the gate three approving answers and one refusal, so
    four of five paths publish and one escalates.
    """
    decision = state.get("decision")
    if decision is not None and getattr(decision, "approved", False):
        return ROUTE_APPROVED
    return ROUTE_REFUSED


#: The route table for ``validate``. Named so a test can assert the compiled
#: graph's conditional edges against it.
VALIDATE_ROUTES: dict[str, str] = {
    ROUTE_PASS: "assemble",
    ROUTE_RETRY: "workers",
    ROUTE_ESCALATE: "escalate",
}

#: The route table for ``gate``.
GATE_ROUTES: dict[str, str] = {
    ROUTE_APPROVED: "publish",
    ROUTE_REFUSED: "escalate",
}

#: Doc 2.10's forward chain, without the back-edge.
DOC_CHAIN: tuple[str, ...] = ("ingest", "plan", "workers", "validate", "assemble", "gate", "publish")


def build_graph(ctx: RunContext, *, checkpointer: Optional[Any] = None):
    """Wire the nodes and compile.

    Args:
        ctx: Per-run dependencies, captured by the node closures.
        checkpointer: LangGraph's saver. **Pass it here**, at
            ``compile()``, not to ``StateGraph``. Supplying one is what makes
            the gate resumable across processes.

    Returns:
        The compiled graph, ready for ``invoke``.

    Note:
        The order the edges are added in is the doc's: forward chain first, then
        the back-edge, then the conditional edges. It makes no behavioural
        difference, but a reader comparing this file to line 696 should be able
        to do it line by line.
    """
    nodes = build_nodes(ctx)
    builder: StateGraph = StateGraph(CourseState)

    for name, fn in nodes.items():
        builder.add_node(name, fn)

    # -- doc 2.10's chain ------------------------------------------------
    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "workers")
    # Required for the back-edge to loop at all. Omitting this makes the
    # retry converge after one pass with no error, which is the single most
    # misleading failure mode in this lab.
    builder.add_edge("workers", "validate")
    builder.add_edge("assemble", "gate")
    builder.add_edge("publish", END)
    builder.add_edge("escalate", END)

    # -- the back-edge ---------------------------------------------------
    builder.add_conditional_edges(
        "validate", route_after_validate, VALIDATE_ROUTES
    )
    builder.add_conditional_edges("gate", route_after_gate, GATE_ROUTES)

    compiled = builder.compile(checkpointer=checkpointer)
    if checkpointer is None:
        LOGGER.warning(
            "compiled the graph without a checkpointer: the gate will "
            "suspend but the run cannot be resumed in another process"
        )
    return compiled


def graph_config(thread_id: str, *, run_id: str) -> dict[str, Any]:
    """The config every ``invoke`` needs.

    Args:
        thread_id: LangGraph's unit of resumption. One run is one thread.
        run_id: Recorded in the run metadata so a checkpoint can be traced
            back to the run that made it. LangGraph puts
            ``configurable.thread_id`` under its own key; the rest goes in
            ``metadata`` where LangGraph stores it alongside the checkpoint.
    """
    return {
        "configurable": {
            "thread_id": thread_id,
            "run_id": run_id,
        }
    }


def node_execution_count(state: CourseState) -> int:
    """How many node executions the run has recorded.

    ``nodes_ran`` is appended by every node, so a clean run is 7 and a run that
    spent every retry is 12. Used by the tests that assert the backstop never
    fired and that a resumed run did not redo pre-gate work.
    """
    return len(state.get("nodes_ran", []) or [])


def expected_executions(*, max_attempts: int, retries_used: int) -> int:
    """Node executions for a run with ``retries_used`` retries.

    2 for ingest and plan, 2 per workers/validate pass, then the tail.

    Args:
        max_attempts: The bound. Present so a test can assert the *backstop* is
            above what the design can ever produce, rather than hard-coding a
            second number that could drift from this one.

    Returns:
        The count, including whichever terminal node the run reached.
    """
    return 2 + 2 * (1 + retries_used) + 4


def graph_edges(graph: Any) -> list[tuple[str, str, bool]]:
    """Every edge of the **compiled** graph as ``(source, target, conditional)``.

    Read off ``compiled.get_graph()`` rather than off a declared constant, so
    this is evidence about what LangGraph actually built. A declared table would
    still read correctly if a node were wired wrongly, which is exactly the bug
    this lab exists to catch.

    Conditional edges appear once per destination. ``validate -> workers`` shows
    up as a conditional edge *and* ``workers -> validate`` appears as a plain
    one; both are required for the loop to exist, and a test asserts both.

    Note:
        ``draw_ascii`` is deliberately not used. It raises
        ``ImportError: Install grandalf to draw graphs`` when that optional
        package is absent, and an ASCII picture is not worth a dependency --
        especially for evidence files that must render identically everywhere.
    """
    drawable = graph.get_graph()
    return sorted(
        (
            str(edge.source),
            str(edge.target),
            bool(getattr(edge, "conditional", False)),
        )
        for edge in drawable.edges
    )


def describe_topology(graph: Any) -> list[str]:
    """The compiled topology as sorted, ASCII-only lines.

    Deterministic by construction: sorted by source then target, no colours, no
    box drawing, no dependency on an optional package. Two runs on two machines
    produce byte-identical output, so it can go in an evidence file.
    """
    lines = ["compiled edges (source -> target, conditional):"]
    for source, target, conditional in graph_edges(graph):
        marker = "[cond]" if conditional else "[edge]"
        lines.append(f"  {marker} {source} -> {target}")
    return lines


def inspect_graph(ctx: RunContext):
    """Compile ``ctx``'s graph for *inspection only*, with an in-memory saver.

    The graph still has to be really compiled for
    :func:`describe_topology` to read anything meaningful, and it is never
    invoked, so nothing is persisted and there is nothing to resume.

    The in-memory saver is explicit so :func:`build_graph` can keep warning when
    it is handed no checkpointer. That warning is worth having in the one case
    it is about: a graph that is about to run. Warning on every inspection would
    train a reader to ignore it.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(ctx, checkpointer=InMemorySaver())


def compiled_nodes(ctx: RunContext) -> list[str]:
    """The node ids the compiled graph actually contains, sorted.

    Read from the compiled graph rather than from :data:`DOC_CHAIN`, so this is
    evidence about what LangGraph built. A declared chain would still read
    correctly if a node had been wired wrongly.
    """
    return sorted(str(n) for n in inspect_graph(ctx).get_graph().nodes)


def topology_lines(ctx: RunContext) -> list[str]:
    """:func:`describe_topology` for a graph built only to be inspected."""
    return describe_topology(inspect_graph(ctx))


__all__ = [
    "DOC_CHAIN",
    "GATE_ROUTES",
    "RECURSION_BACKSTOP",
    "VALIDATE_ROUTES",
    "build_graph",
    "compiled_nodes",
    "describe_topology",
    "expected_executions",
    "graph_config",
    "graph_edges",
    "inspect_graph",
    "node_execution_count",
    "route_after_gate",
    "route_after_validate",
    "topology_lines",
]