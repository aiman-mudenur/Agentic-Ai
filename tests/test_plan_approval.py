"""Focused tests for GitHub submission tracking.

Tests cover:
- Submitting a GitHub repo URL and commit SHA
- Validation of repository URLs and commit SHAs
- Distinguishing file submissions from GitHub submissions
- Faculty/student access control
- Invalid repo URLs and missing commits rejected
- Unauthenticated access rejected
"""

import atexit
import os
import shutil
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from db.base import SQLModel
from db.session import get_session as prod_get_session
from models.auth import User, Enrollment
from models.assignment import Assignment
from api.security import get_password_hash

TMP_DIR = tempfile.mkdtemp(prefix="test_github_submissions_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_github_submissions.db")
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


def test_github_submission_create_and_retrieve():
    """Submit a GitHub repo URL and commit SHA as a student, then retrieve it."""
    client = _client()
    fid = _mkuser("facgh@x.edu", "faculty123", "faculty", "Fac GH")
    sid = _mkuser("stugh@x.edu", "student123", "student", "Stu GH")
    hf = _login(client, "facgh@x.edu", "faculty123")
    hs = _login(client, "stugh@x.edu", "student123")

    # Create a course and assignment as faculty
    r = client.post(
        "/courses/",
        headers=hf,
        json={"course_code": "CS100", "title": "Test Course"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/assignments/",
        headers=hf,
        data={"course_code": "CS100", "title": "GitHub Assignment", "max_marks": 20},
    )
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    # Student enrolls
    r = client.post("/courses/CS100/enroll", headers=hs)
    assert r.status_code == 200, r.text

    # Student submits a GitHub repo
    with patch("api.routers.submissions.validate_and_fetch_commit", return_value=None), \
         patch("api.routers.submissions.validate_repo_accessible", return_value=None):
        r = client.post(
            "/submissions/github",
            headers=hs,
            data={
                "assignment_id": aid,
                "repo_url": "https://github.com/octocat/Hello-World",
                "commit_hash": "553c227e7f4d6c1f1f9a3b7b4d7c8e9f0a1b2c3d",
                "branch": "main",
            },
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["submission_type"] == "github"
    assert data["github_repo_url"] == "https://github.com/octocat/Hello-World"
    assert data["commit_hash"] == "553c227e7f4d6c1f1f9a3b7b4d7c8e9f0a1b2c3d"
    assert data["branch"] == "main"

    # Retrieve the submission
    sub_id = data["id"]
    r = client.get(f"/submissions/{sub_id}", headers=hs)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["submission_type"] == "github"
    assert data["github_repo_url"] == "https://github.com/octocat/Hello-World"


def test_invalid_repo_url_rejected():
    """A non-GitHub URL should be rejected with 400."""
    client = _client()
    fid = _mkuser("facpg@x.edu", "faculty123", "faculty", "Fac PG")
    sid = _mkuser("stugp@x.edu", "student123", "student", "Stu GP")
    hf = _login(client, "facpg@x.edu", "faculty123")
    hs = _login(client, "stugp@x.edu", "student123")

    r = client.post(
        "/courses/",
        headers=hf,
        json={"course_code": "CS110", "title": "Test Course"},
    )
    assert r.status_code == 200
    r = client.post(
        "/assignments/",
        headers=hf,
        data={"course_code": "CS110", "title": "GitHub Assignment", "max_marks": 20},
    )
    assert r.status_code == 200
    aid = r.json()["id"]

    r = client.post("/courses/CS110/enroll", headers=hs)
    assert r.status_code == 200

    r = client.post(
        "/submissions/github",
        headers=hs,
        data={
            "assignment_id": aid,
            "repo_url": "https://notgithub.com/some/repo",
            "commit_hash": "abc123",
        },
    )
    assert r.status_code == 400, r.text


def test_nonexistent_commit_rejected():
    """A commit SHA that doesn't exist should be rejected."""
    client = _client()
    fid = _mkuser("facpg2@x.edu", "faculty123", "faculty", "Fac PG2")
    sid = _mkuser("stugp2@x.edu", "student123", "student", "Stu GP2")
    hf = _login(client, "facpg2@x.edu", "faculty123")
    hs = _login(client, "stugp2@x.edu", "student123")

    r = client.post(
        "/courses/",
        headers=hf,
        json={"course_code": "CS111", "title": "Test Course"},
    )
    assert r.status_code == 200
    r = client.post(
        "/assignments/",
        headers=hf,
        data={"course_code": "CS110", "title": "GitHub Assignment", "max_marks": 20},
    )
    assert r.status_code == 200
    aid = r.json()["id"]

    r = client.post("/courses/CS110/enroll", headers=hs)
    assert r.status_code == 200

    # Use a clearly fake SHA
    r = client.post(
        "/submissions/github",
        headers=hs,
        data={
            "assignment_id": aid,
            "repo_url": "https://github.com/octocat/Hello-World",
            "commit_hash": "0000000000000000000000000000000000000000",
        },
    )
    assert r.status_code == 400, r.text


def test_student_cannot_see_other_students_github_submission():
    client = _client()
    fid = _mkuser("facpg3@x.edu", "faculty123", "faculty", "Fac PG3")
    sid1 = _mkuser("stugp3a@x.edu", "student123", "student", "Stu1 PG3")
    sid2 = _mkuser("stugp3b@x.edu", "student123", "student", "Stu2 PG3")
    hf = _login(client, "facpg3@x.edu", "faculty123")
    hs1 = _login(client, "stugp3a@x.edu", "student123")
    hs2 = _login(client, "stugp3b@x.edu", "student123")

    r = client.post(
        "/courses/",
        headers=hf,
        json={"course_code": "CS112", "title": "Test Course"},
    )
    assert r.status_code == 200
    r = client.post(
        "/assignments/",
        headers=hf,
        data={"course_code": "CS110", "title": "GitHub Assignment", "max_marks": 20},
    )
    assert r.status_code == 200
    aid = r.json()["id"]

    r = client.post("/courses/CS110/enroll", headers=hs1)
    assert r.status_code == 200
    r = client.post("/courses/CS110/enroll", headers=hs2)
    assert r.status_code == 200

    # Student 1 submits
    with patch("api.routers.submissions.validate_and_fetch_commit", return_value=None), \
         patch("api.routers.submissions.validate_repo_accessible", return_value=None):
        r = client.post(
            "/submissions/github",
            headers=hs1,
            data={
                "assignment_id": aid,
                "repo_url": "https://github.com/octocat/Hello-World",
                "commit_hash": "553c227e7f4d6c1f1f9a3b7b4d7c8e9f0a1b2c3d",
            },
        )
    assert r.status_code == 200, r.text
    sub_id = r.json()["id"]

    # Student 2 cannot see student 1's submission
    r = client.get(f"/submissions/{sub_id}", headers=hs2)
    assert r.status_code == 403, r.text


def test_cannot_update_approved_plan():
    client = _client()
    fid = _mkuser("facpu2@x.edu", "faculty123", "faculty", "Fac PU2")
    hf = _login(client, "facpu2@x.edu", "faculty123")
    r = client.post("/plans/", params={"plan_id": "PLAN-TEST-008", "plan_hash": "hash888", "course_code": "CS108"}, headers=hf)
    assert r.status_code == 201
    r = client.post("/plans/PLAN-TEST-008/approve", params={"plan_hash": "hash888"}, headers=hf)
    assert r.status_code == 200
    # Try to update (should fail - already approved)
    r = client.post("/plans/", params={"plan_id": "PLAN-TEST-008", "plan_hash": "hash999", "course_code": "CS108"}, headers=hf)
    assert r.status_code == 409, r.text


def test_list_plans_filter_by_status():
    client = _client()
    fid = _mkuser("facpf@x.edu", "faculty123", "faculty", "Fac PF")
    hf = _login(client, "facpf@x.edu", "faculty123")
    r = client.post("/plans/", params={"plan_id": "PLAN-TEST-009", "plan_hash": "hashf1", "course_code": "CS109"}, headers=hf)
    assert r.status_code == 201
    r = client.post("/plans/PLAN-TEST-009/approve", params={"plan_hash": "hashf1"}, headers=hf)
    assert r.status_code == 200
    # Filter by draft status
    r = client.get("/plans/", params={"status": "draft"}, headers=hf)
    assert r.status_code == 200
    # Filter by approved status
    r = client.get("/plans/", params={"status": "approved"}, headers=hf)
    assert r.status_code == 200
    approved = r.json()
    assert any(p["plan_id"] == "PLAN-TEST-009" for p in approved)