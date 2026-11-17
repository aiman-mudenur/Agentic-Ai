"""Focused tests for project/milestone authorization (Task 1)."""

import atexit
import os
import shutil
import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from db.base import SQLModel
from db.session import get_session as prod_get_session
from models.auth import User, Enrollment
from models.project import Project, ProjectGroup, Milestone
from api.security import get_password_hash

TMP_DIR = tempfile.mkdtemp(prefix="test_projects_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_projects.db")
test_engine = create_engine(f"sqlite:///{TMP_DB}", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(test_engine)


def override_session():
    with Session(test_engine) as session:
        yield session


def _client():
    from api.main import app

    app.dependency_overrides[prod_get_session] = override_session
    return TestClient(app)


def _mkuser(email, password, role, name):
    with Session(test_engine) as s:
        existing = s.exec(select(User).where(User.email == email)).first()
        if existing:
            return existing.id
        u = User(username=email.split("@")[0] + role[:2], email=email, full_name=name,
                 role=role, hashed_password=get_password_hash(password))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_faculty_can_create_project():
    client = _client()
    fid = _mkuser("facp@x.edu", "faculty123", "faculty", "Fac P")
    hf = _login(client, "facp@x.edu", "faculty123")
    r = client.post("/projects", params={"course_code": "CS101", "title": "Test Project"}, headers=hf)
    assert r.status_code == 201, r.text
    assert r.json()["course_code"] == "CS101"


def test_student_cannot_create_project():
    client = _client()
    _mkuser("stup@x.edu", "student123", "student", "Stu P")
    hs = _login(client, "stup@x.edu", "student123")
    r = client.post("/projects", params={"course_code": "CS101", "title": "Test Project"}, headers=hs)
    assert r.status_code == 403, r.text


def test_student_can_view_own_group_milestones():
    client = _client()
    fid = _mkuser("facp2@x.edu", "faculty123", "faculty", "Fac P2")
    sid = _mkuser("stup2@x.edu", "student123", "student", "Stu P2")
    hf = _login(client, "facp2@x.edu", "faculty123")
    hs = _login(client, "stup2@x.edu", "student123")
    # Create project
    r = client.post("/projects", params={"course_code": "CS102", "title": "Proj2"}, headers=hf)
    assert r.status_code == 201
    pid = r.json()["id"]
    # Create group with student as member
    r = client.post(f"/projects/{pid}/groups", params={"group_id": "G1", "members": str(sid)}, headers=hf)
    assert r.status_code == 201
    # Create milestone
    r = client.post(f"/projects/{pid}/milestones", params={"group_id": "G1", "checkpoint_id": "M1", "name": "Checkpoint 1", "due_date": "2026-12-01", "expected_deliverable": "Report"}, headers=hf)
    assert r.status_code == 201
    # Student can view milestones for their group
    r = client.get(f"/projects/{pid}/milestones", headers=hs)
    assert r.status_code == 200
    ms = r.json()
    assert len(ms) == 1
    assert ms[0]["checkpoint_id"] == "M1"


def test_student_cannot_view_other_group_milestones():
    client = _client()
    fid = _mkuser("facp3@x.edu", "faculty123", "faculty", "Fac P3")
    sid1 = _mkuser("stu1p3@x.edu", "student123", "student", "Stu1 P3")
    sid2 = _mkuser("stu2p3@x.edu", "student123", "student", "Stu2 P3")
    hf = _login(client, "facp3@x.edu", "faculty123")
    hs1 = _login(client, "stu1p3@x.edu", "student123")
    hs2 = _login(client, "stu2p3@x.edu", "student123")
    r = client.post("/projects", params={"course_code": "CS103", "title": "Proj3"}, headers=hf)
    pid = r.json()["id"]
    # Group with only sid1
    client.post(f"/projects/{pid}/groups", params={"group_id": "G1", "members": str(sid1)}, headers=hf)
    client.post(f"/projects/{pid}/milestones", params={"group_id": "G1", "checkpoint_id": "M1", "name": "CP1", "due_date": "2026-12-01"}, headers=hf)
    # sid2 (not in group) cannot see milestones
    r = client.get(f"/projects/{pid}/milestones", headers=hs2)
    assert r.status_code == 200
    assert len(r.json()) == 0


def test_milestone_requires_evidence_for_accepted():
    client = _client()
    fid = _mkuser("facp4@x.edu", "faculty123", "faculty", "Fac P4")
    sid = _mkuser("stup4@x.edu", "student123", "student", "Stu P4")
    hf = _login(client, "facp4@x.edu", "faculty123")
    hs = _login(client, "stup4@x.edu", "student123")
    r = client.post("/projects", params={"course_code": "CS104", "title": "Proj4"}, headers=hf)
    pid = r.json()["id"]
    client.post(f"/projects/{pid}/groups", params={"group_id": "G1", "members": str(sid)}, headers=hf)
    r = client.post(f"/projects/{pid}/milestones", params={"group_id": "G1", "checkpoint_id": "M1", "name": "CP1", "due_date": "2026-12-01"}, headers=hf)
    ms_id = r.json()["id"]
    # Try to set status to accepted without evidence -> 422
    r = client.patch(f"/milestones/{ms_id}", params={"status": "accepted"}, headers=hs)
    assert r.status_code == 422, r.text
    # Set with evidence -> OK
    r = client.patch(f"/milestones/{ms_id}", params={"status": "submitted", "evidence": "https://example.com/report"}, headers=hs)
    assert r.status_code == 200, r.text


def test_schedule_health():
    client = _client()
    fid = _mkuser("facp5@x.edu", "faculty123", "faculty", "Fac P5")
    sid = _mkuser("stup5@x.edu", "student123", "student", "Stu P5")
    hf = _login(client, "facp5@x.edu", "faculty123")
    hs = _login(client, "stup5@x.edu", "student123")
    r = client.post("/projects", params={"course_code": "CS105", "title": "Proj5"}, headers=hf)
    pid = r.json()["id"]
    client.post(f"/projects/{pid}/groups", params={"group_id": "G1", "members": str(sid)}, headers=hf)
    r = client.post(f"/projects/{pid}/milestones", params={"group_id": "G1", "checkpoint_id": "M1", "name": "CP1", "due_date": "2026-12-01"}, headers=hf)
    ms_id = r.json()["id"]
    # Update to submitted with evidence
    client.patch(f"/milestones/{ms_id}", params={"status": "submitted", "evidence": "report link"}, headers=hs)
    # Check schedule health
    r = client.get(f"/projects/{pid}/schedule-health", headers=hf)
    assert r.status_code == 200
    health = r.json()
    assert len(health) == 1
    assert health[0]["completed"] == 1
    assert health[0]["schedule_health"] == 1.0


def test_unauthenticated_cannot_access_projects():
    client = _client()
    r = client.get("/projects")
    assert r.status_code == 401, r.text


def test_faculty_cannot_manage_other_faculty_project():
    client = _client()
    fid1 = _mkuser("facp6a@x.edu", "faculty123", "faculty", "Fac P6a")
    fid2 = _mkuser("facp6b@x.edu", "faculty123", "faculty", "Fac P6b")
    hf1 = _login(client, "facp6a@x.edu", "faculty123")
    hf2 = _login(client, "facp6b@x.edu", "faculty123")
    r = client.post("/projects", params={"course_code": "CS106", "title": "Proj6"}, headers=hf1)
    pid = r.json()["id"]
    # faculty2 cannot create group for faculty1's project
    r = client.post(f"/projects/{pid}/groups", params={"group_id": "G1", "members": "1"}, headers=hf2)
    assert r.status_code == 403, r.text
    # faculty2 cannot create milestone for faculty1's project
    r = client.post(f"/projects/{pid}/milestones", params={"group_id": "G1", "checkpoint_id": "M1", "name": "CP1", "due_date": "2026-12-01"}, headers=hf2)
    assert r.status_code == 403, r.text
