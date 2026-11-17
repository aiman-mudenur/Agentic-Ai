from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel
from sqlmodel import Session, select

from api.deps import require_role
from db.session import get_session
from models.assignment import Assignment
from models.auth import User
from course_organizer.graph.context import build_run_context
from course_organizer.graph.runner import (
    build_graph,
    checkpoint_path,
    graph_config,
    open_checkpointer,
)
from course_organizer.graph.state import CourseStatus
from services.graph_service import (
    DEC_MAP,
    pipeline_root,
    resume_pipeline,
    resume_swarm_run,
    run_full_pipeline,
    run_parallel_swarm,
)


router = APIRouter()


class RunStart(BaseModel):
    course_code: str = "CS201"
    run_id: str
    domain: str = "theory"


class RunResume(BaseModel):
    run_id: str
    decision: str  # approve|edit|override|refuse


def _check_run_id(run_id: str) -> str:
    """Refuse run ids that could escape the pipeline root (path traversal)."""
    if not run_id or run_id != run_id.strip() or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise HTTPException(status_code=422, detail="Invalid run_id")
    return run_id


def _check_decision(decision: str) -> str:
    """An unknown decision must never become an implicit approval.

    ``resume_pipeline`` used to map unknown strings to approve via
    ``DEC_MAP.get(decision, DECISION_APPROVE)``; the API now rejects them
    before they reach the graph.
    """
    if decision not in DEC_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown decision {decision!r}; expected one of {sorted(DEC_MAP)}",
        )
    return decision


@router.post("/pipeline/start")
def start_pipeline(payload: RunStart, user: User = Depends(require_role("faculty"))):
    return run_full_pipeline(
        payload.course_code, _check_run_id(payload.run_id), payload.domain
    )


@router.post("/swarm/start")
def start_swarm(payload: RunStart, parallel: bool = True, user: User = Depends(require_role("faculty"))):
    return run_parallel_swarm(
        payload.course_code, _check_run_id(payload.run_id), parallel=parallel
    )


@router.post("/pipeline/resume")
def resume_pipe(payload: RunResume, user: User = Depends(require_role("faculty"))):
    _check_decision(payload.decision)
    try:
        return resume_pipeline(_check_run_id(payload.run_id), payload.decision)
    except PermissionError as exc:
        # The publish guard refuses a run whose specialist failed; that is a
        # governance decision, not a server crash.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/swarm/resume")
def resume_sw(payload: RunResume, user: User = Depends(require_role("faculty"))):
    _check_decision(payload.decision)
    return resume_swarm_run(_check_run_id(payload.run_id), payload.decision)


@router.get("/{run_id}/status", response_model=dict)
def run_status(
    run_id: str = Path(...),
    user: User = Depends(require_role("faculty")),
    session: Session = Depends(get_session),
):
    """Get the status of a pipeline run, including specialist and domain info.

    Requires faculty role. Returns only read-only, scoped run metadata:
    - run_id, course_code (if authorized), domain, specialist_ok,
      domain_error, run status, and nodes executed.
    Does NOT expose student data, submissions, full audit payloads,
    credentials, or tokens.
    """
    _check_run_id(run_id)
    checkpoint_file = checkpoint_path(pipeline_root(), run_id)

    if not checkpoint_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    # Reopen the checkpoint to read state (without resuming). The context is
    # rebuilt the same way run_pipeline built it, so build_graph gets a
    # registry it can construct a graph from.
    ctx = build_run_context(run_id, output_dir=pipeline_root() / run_id)
    with open_checkpointer(checkpoint_file) as saver:
        graph = build_graph(ctx, checkpointer=saver)
        state = graph.get_state(graph_config(f"thread-{run_id}", run_id=run_id))

        # LangGraph's get_state returns a StateSnapshot: the channel *values*
        # live under ``.values`` and a suspended run shows its pending work
        # under ``.next`` / ``.tasks`` (which carry the gate interrupt).
        values = dict(state.values) if isinstance(getattr(state, "values", None), dict) else {}
        next_nodes = list(getattr(state, "next", ()) or ())
        tasks = getattr(state, "tasks", ()) or ()
        has_interrupt = any(getattr(t, "interrupts", ()) for t in tasks)

    domain = values.get("domain")
    specialist_ok = values.get("specialist_ok")
    domain_error = values.get("domain_error")
    raw_status = values.get("status")

    # Determine run status enum
    if raw_status is not None:
        try:
            status_enum = CourseStatus(raw_status)
        except (ValueError, TypeError):
            status_enum = None
    else:
        status_enum = None

    # Build safe response
    response: dict = {
        "run_id": run_id,
        "status": status_enum.value if status_enum else None,
        # A run is awaiting the gate when the graph is suspended on the gate
        # interrupt: either the interrupt is pending on a task or the gate is
        # the next node to run.
        "awaiting_gate": bool(has_interrupt) or ("gate" in next_nodes),
        "nodes_ran": list(values.get("nodes_ran", []) or []),
    }

    # Add domain/specialist info only if present
    if domain is not None:
        response["domain"] = domain
    if specialist_ok is not None:
        response["specialist_ok"] = specialist_ok
    if domain_error is not None:
        response["domain_error"] = domain_error

    # Course authorization: a faculty member may only see runs for a course
    # they teach, where "teach" follows the app's existing convention --
    # they have created an assignment in that course (the same rule the
    # assignments router and login use; the Course model has no owner).
    run_course_code = values.get("course_code")
    if run_course_code:
        teaches = session.exec(
            select(Assignment.id).where(
                Assignment.created_by == user.id,
                Assignment.course_code == run_course_code,
            )
        ).first()
        response["course_authorized"] = teaches is not None
    else:
        response["course_authorized"] = False

    return response
