from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, Enrollment
from models.project import Project, ProjectGroup, Milestone
from api.deps import get_current_user, require_role

router = APIRouter()


# =============================================================================
# Projects
# =============================================================================

@router.get("/")
def list_projects(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """List projects visible to the current user."""
    q = select(Project)
    if user.role == "student":
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id)).all()
        if not enrolled:
            return []
        q = q.where(Project.course_code.in_(enrolled))
    elif user.role == "faculty":
        q = q.where(Project.created_by == user.id)
    # HoD/admin see all
    return session.exec(q).all()


@router.post("/", status_code=201)
def create_project(
    course_code: str,
    title: str,
    description: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    proj = Project(
        course_code=course_code,
        title=title,
        description=description,
        created_by=user.id,
    )
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


@router.get("/{project_id}")
def get_project(
    project_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "student":
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id, Enrollment.course_code == proj.course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty":
        if proj.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your project")
    return proj


@router.patch("/{project_id}")
def update_project(
    project_id: int,
    title: str | None = None,
    description: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "faculty" and proj.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your project")
    if title is not None:
        proj.title = title
    if description is not None:
        proj.description = description
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


# =============================================================================
# Groups
# =============================================================================

@router.get("/{project_id}/groups")
def list_groups(
    project_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "student":
        enrolled = session.exec(select(Enrollment.course_code).where(Enrollment.student_id == user.id, Enrollment.course_code == proj.course_code)).first()
        if not enrolled:
            raise HTTPException(status_code=403, detail="Not enrolled in this course")
    elif user.role == "faculty":
        if proj.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your project")
    groups = session.exec(select(ProjectGroup).where(ProjectGroup.project_id == project_id)).all()
    return groups


@router.post("/{project_id}/groups", status_code=201)
def create_group(
    project_id: int,
    group_id: str,
    members: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "faculty" and proj.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your project")
    grp = ProjectGroup(project_id=project_id, group_id=group_id, members=members or "")
    session.add(grp)
    session.commit()
    session.refresh(grp)
    return grp


@router.patch("/{project_id}/groups/{group_id}")
def update_group(
    project_id: int,
    group_id: str,
    members: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "faculty" and proj.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your project")
    grp = session.get(ProjectGroup, group_id)
    if not grp or grp.project_id != project_id:
        raise HTTPException(status_code=404, detail="Group not found")
    if members is not None:
        grp.members = members
    session.add(grp)
    session.commit()
    session.refresh(grp)
    return grp


# =============================================================================
# Milestones
# =============================================================================

@router.get("/{project_id}/milestones")
def list_milestones(
    project_id: int,
    group_id: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "student":
        groups = session.exec(select(ProjectGroup).where(ProjectGroup.project_id == project_id)).all()
        my_groups = [g.group_id for g in groups if str(user.id) in [m.strip() for m in g.members.split(",") if m.strip()]]
        if not my_groups:
            return []
    q = select(Milestone).where(Milestone.project_id == project_id)
    if group_id:
        q = q.where(Milestone.group_id == group_id)
    milestones = session.exec(q).all()
    if user.role == "student":
        groups = session.exec(select(ProjectGroup).where(ProjectGroup.project_id == project_id)).all()
        my_groups = [g.group_id for g in groups if str(user.id) in [m.strip() for m in g.members.split(",") if m.strip()]]
        milestones = [m for m in milestones if m.group_id in my_groups]
    return milestones


@router.post("/{project_id}/milestones", status_code=201)
def create_milestone(
    project_id: int,
    group_id: str,
    checkpoint_id: str,
    name: str,
    due_date: str | None = None,
    expected_deliverable: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(require_role("faculty", "hod", "admin")),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "faculty" and proj.created_by != user.id:
        raise HTTPException(status_code=403, detail="Not your project")
    grp = session.get(ProjectGroup, group_id)
    if not grp or grp.project_id != project_id:
        raise HTTPException(status_code=404, detail="Group not found")
    ms = Milestone(
        project_id=project_id,
        group_id=group_id,
        checkpoint_id=checkpoint_id,
        name=name,
        due_date=date.fromisoformat(due_date) if due_date else None,
        expected_deliverable=expected_deliverable,
    )
    session.add(ms)
    session.commit()
    session.refresh(ms)
    return ms


@router.patch("/milestones/{milestone_id}")
def update_milestone(
    milestone_id: int,
    status: str | None = None,
    evidence: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    ms = session.get(Milestone, milestone_id)
    if not ms:
        raise HTTPException(status_code=404, detail="Milestone not found")
    proj = session.get(Project, ms.project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "student":
        groups = session.exec(select(ProjectGroup).where(ProjectGroup.project_id == ms.project_id)).all()
        my_groups = [g.group_id for g in groups if str(user.id) in [m.strip() for m in g.members.split(",") if m.strip()]]
        if ms.group_id not in my_groups:
            raise HTTPException(status_code=403, detail="Not your group")
    elif user.role == "faculty":
        if proj.created_by != user.id:
            raise HTTPException(status_code=403, detail="Not your project")
    if status is not None:
        ms.status = status
    if evidence is not None:
        ms.evidence = evidence
    ms.updated_at = datetime.now(timezone.utc)
    ms.updated_by = user.id
    session.add(ms)
    session.commit()
    session.refresh(ms)
    return ms


@router.get("/{project_id}/schedule-health")
def schedule_health(
    project_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    proj = session.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    groups = session.exec(select(ProjectGroup).where(ProjectGroup.project_id == project_id)).all()
    milestones = session.exec(select(Milestone).where(Milestone.project_id == project_id)).all()
    result = []
    for g in groups:
        g_ms = [m for m in milestones if m.group_id == g.group_id]
        total = len(g_ms)
        done = sum(1 for m in g_ms if m.status in ("submitted", "accepted"))
        overdue = sum(1 for m in g_ms if m.status == "overdue")
        evidence_missing = sum(1 for m in g_ms if m.status in ("submitted", "accepted") and not m.evidence.strip())
        result.append({
            "group_id": g.group_id,
            "members": g.members,
            "total_checkpoints": total,
            "completed": done,
            "overdue": overdue,
            "evidence_missing": evidence_missing,
            "schedule_health": round(done / total, 4) if total > 0 else 0.0,
            "status": "overdue" if overdue > 0 else ("completed" if total > 0 and done == total else "in_progress" if done > 0 else "not_started"),
        })
    return result
