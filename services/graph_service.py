from __future__ import annotations

from pathlib import Path
from typing import Optional, Any, Dict

from course_organizer.graph import run_pipeline, resume_run, DECISION_APPROVE, DECISION_EDIT, DECISION_OVERRIDE, DECISION_REFUSE
from course_organizer.graph.swarm import run_swarm, resume_swarm
from course_organizer.graph.runner import GraphRun


REPO_ROOT = Path(__file__).resolve().parents[2]


def _out_dir(kind: str) -> Path:
    d = REPO_ROOT / "out" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_full_pipeline(course_code: str, run_id: str) -> Dict[str, Any]:
    root = _out_dir("lab07")
    outd = root / run_id
    outd.mkdir(parents=True, exist_ok=True)
    run: GraphRun = run_pipeline(course_code=course_code, run_id=run_id, root=root, output_dir=outd)
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
    root = _out_dir("lab07")
    run = resume_run(run_id=run_id, decision=DEC_MAP.get(decision, DECISION_APPROVE), root=root)
    return _run_to_dict(run)


def resume_swarm_run(run_id: str, decision: str) -> Dict[str, Any]:
    root = _out_dir("lab08")
    run = resume_swarm(run_id=run_id, decision=DEC_MAP.get(decision, DECISION_APPROVE), root=root)
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
