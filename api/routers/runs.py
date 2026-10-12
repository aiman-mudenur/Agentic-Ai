from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.deps import get_current_user, require_role
from models.auth import User
from services.graph_service import (
    run_full_pipeline,
    run_parallel_swarm,
    resume_pipeline,
    resume_swarm_run,
)

router = APIRouter()


class RunStart(BaseModel):
    course_code: str = "CS201"
    run_id: str


class RunResume(BaseModel):
    run_id: str
    decision: str  # approve|edit|override|refuse


@router.post("/pipeline/start")
def start_pipeline(payload: RunStart, user: User = Depends(require_role("faculty", "admin"))):
    return run_full_pipeline(payload.course_code, payload.run_id)


@router.post("/swarm/start")
def start_swarm(payload: RunStart, parallel: bool = True, user: User = Depends(require_role("faculty", "admin"))):
    return run_parallel_swarm(payload.course_code, payload.run_id, parallel=parallel)


@router.post("/pipeline/resume")
def resume_pipe(payload: RunResume, user: User = Depends(require_role("faculty", "admin"))):
    return resume_pipeline(payload.run_id, payload.decision)


@router.post("/swarm/resume")
def resume_sw(payload: RunResume, user: User = Depends(require_role("faculty", "admin"))):
    return resume_swarm_run(payload.run_id, payload.decision)
