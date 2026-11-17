from __future__ import annotations

from pathlib import Path
from typing import Optional, Any, Dict

from course_organizer.graph import run_pipeline, resume_run, DECISION_APPROVE, DECISION_EDIT, DECISION_OVERRIDE, DECISION_REFUSE
from course_organizer.graph.swarm import run_swarm, resume_swarm
from course_organizer.graph.runner import GraphRun


#: Repository root. ``services/`` is one level below it, so ``parents[1]`` --
#: ``parents[2]`` would point at the *parent* of the checkout and put API run
#: artifacts and uploads outside the project, where the documented ``out/``
#: tree and the dashboard's run list do not look.
REPO_ROOT = Path(__file__).resolve().parents[1]


def _out_dir(kind: str) -> Path:
    d = REPO_ROOT / "out" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def pipeline_root() -> Path:
    """Checkpoint/artifact root for API-started pipeline runs (``out/lab07``).

    One source of truth for run start, resume *and* the status endpoint, so a
    run started by the API can always be found again by its run id. The
    status endpoint previously computed its own copy of this path, which is
    how it drifted out of sync (and stopped working entirely) in the first
    place.
    """
    return _out_dir("lab07")


def run_full_pipeline(course_code: str, run_id: str, domain: str = "theory") -> Dict[str, Any]:
    root = pipeline_root()
    outd = root / run_id
    outd.mkdir(parents=True, exist_ok=True)
    run: GraphRun = run_pipeline(course_code=course_code, run_id=run_id, root=root, output_dir=outd, domain=domain)
    return _run_to_dict(run)


def run_parallel_swarm(course_code: str, run_id: str, parallel: bool = True) -> Dict[str, Any]:
    root = _out_dir("lab08")
    outd = root / run_id
    outd.mkdir(parents=True, exist_ok=True)
    run: GraphRun = run_swarm(course_code=course_code, run_id=run_id, root=root, output_dir=outd, parallel=parallel)
    return _run_to_dict(run)


DEC_MAP = {
    "approve": DECISION_APPROVE,
    "edit": DECISION_EDIT,
    "override": DECISION_OVERRIDE,
    "refuse": DECISION_REFUSE,
}


def resume_pipeline(run_id: str, decision: str) -> Dict[str, Any]:
    root = pipeline_root()
    if decision not in DEC_MAP:
        # An unknown decision must never silently become an approval. The
        # router validates first; this guard covers direct service callers.
        raise ValueError(
            f"Unknown decision {decision!r}; expected one of {sorted(DEC_MAP)}"
        )
    run = resume_run(run_id=run_id, decision=DEC_MAP[decision], root=root)
    return _run_to_dict(run)


def resume_swarm_run(run_id: str, decision: str) -> Dict[str, Any]:
    root = _out_dir("lab08")
    if decision not in DEC_MAP:
        raise ValueError(
            f"Unknown decision {decision!r}; expected one of {sorted(DEC_MAP)}"
        )
    run = resume_swarm(run_id=run_id, decision=DEC_MAP[decision], root=root)
    return _run_to_dict(run)


def _run_to_dict(run: GraphRun) -> Dict[str, Any]:
    return {
        "run_id": getattr(run, "run_id", None),
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "awaiting_gate": bool(run.awaiting_gate),
        "nodes_ran": list(run.nodes_ran()),
        "published": getattr(run, "published", None),
        "summary": run.summary(),
        "state_keys": list(run.state.keys()) if hasattr(run, "state") else [],
        "swarm_timing": run.state.get("swarm_timing") if hasattr(run, "state") else None,
        "swarm_quality": run.state.get("swarm_quality") if hasattr(run, "state") else None,
        "swarm_merge_log": run.state.get("swarm_merge_log") if hasattr(run, "state") else None,
    }
