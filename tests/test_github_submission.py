"""Focused tests for GitHub submission tracking feature.

Tests cover:
- Submitting a GitHub repo URL and commit SHA as a student
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
test_engine = create_engine(f'sqlite:///{TMP_DB}', connect_args={'check_same_thread': False})
SQLModel.metadata.create_all(test_engine)


def override_session():
    with Session(test_engine) as session:
        yield session


def _client():
    from api.main import app

    app.dependency_overrides[prod_get_session] = override_session
    return TestClient(app)


def _mkuser(email, password, role, name):
    """Create a test user directly in DB."""
    with Session(test_engine) as s:
        u = User(username=email.split("@")[0] + role[:2], email=email, full_name=name,
                 role=role, hashed_password=get_password_hash(password))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, f"Login failed for {email}: {r.text}"
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def _seed_course_and_assignment(client, hf, course_code="CS100", course_title="Test Course",
                                assignment_title="GitHub Assignment"):
    """Create course + assignment as faculty, return assignment id."""
    r = client.post("/courses/", headers=hf,
                    json={"course_code": course_code, "title": course_title})
    assert r.status_code == 200, r.text
    r = client.post(
        "/assignments/",
        headers=hf,
        data={"course_code": course_code, "title": assignment_title, "max_marks": 20},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mock_validate_and_fetch_commit(repo_url, commit_hash, branch=None, github_token=None):
    """Mock that accepts any github.com URL and any commit SHA."""
    import re
    from services.github_submission import CommitInfo

    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", repo_url.strip(), re.IGNORECASE)
    if m:
        owner, repo = m.group(1), m.group(2).rstrip(".git")
    else:
        owner, repo = "octocat", "Hello-World"
    return CommitInfo(
        sha=commit_hash,
        short_sha=commit_hash[:7],
        message="Test commit",
        author_name="Test Author",
        author_date="2026-01-01T00:00:00Z",
        url=f"https://api.github.com/repos/{owner}/{repo}/commits/{commit_hash}",
        html_url=f"https://github.com/{owner}/{repo}/commit/{commit_hash}",
    )


def test_github_submission_create_and_retrieve():
    """Submit a GitHub repo URL and commit SHA as a student, then retrieve it."""
    client = _client()
    fid = _mkuser("facp@x.edu", "faculty123", "faculty", "Fac GH")
    sid = _mkuser("stugh@x.edu", "student123", "student", "Stu GH")
    hf = _login(client, "facp@x.edu", "faculty123")
    hs = _login(client, "stugh@x.edu", "student123")

    aid = _seed_course_and_assignment(client, hf)

    # Student enrolls
    r = client.post("/courses/CS100/enroll", headers=hs)
    assert r.status_code == 200, r.text

    # Student submits a GitHub repo
    with patch("api.routers.submissions.validate_and_fetch_commit",
               mock_validate_and_fetch_commit), \
         patch("api.routers.submissions.validate_repo_accessible",
               return_value=None):
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
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["submission_type"] == "github"
    assert data["github_repo_url"] == "https://github.com/octocat/Hello-World"
    assert data["commit_hash"] == "553c227e7f4d6c1f1f9a3b7b4d7c8e9f0a1b2c3d"
    assert data["branch"] == "main"

    # Retrieve the submission
    sub_id = data["id"]
    r = client.get(f"/submissions/{sub_id}", headers=hs)
    assert r.status_code == 200, f"GET /submissions/{sub_id} failed: {r.text}"
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

    aid = _seed_course_and_assignment(client, hf, "CS110", "Test Course",
                                      "GitHub Assignment")

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

    aid = _seed_course_and_assignment(client, hf, "CS111", "Test Course",
                                      "GitHub Assignment")

    r = client.post("/courses/CS111/enroll", headers=hs)
    assert r.status_code == 200

    # Use a clearly fake SHA - mock validate_and_fetch_commit to raise GitHubError
    from services.github_submission import GitHubError
    with patch("api.routers.submissions.validate_and_fetch_commit",
               side_effect=GitHubError("Commit not found")):
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

    aid = _seed_course_and_assignment(client, hf, "CS112", "Test Course",
                                      "GitHub Assignment")

    r = client.post("/courses/CS112/enroll", headers=hs1)
    assert r.status_code == 200
    r = client.post("/courses/CS112/enroll", headers=hs2)
    assert r.status_code == 200

    # Student 1 submits
    with patch("api.routers.submissions.validate_and_fetch_commit",
               mock_validate_and_fetch_commit), \
         patch("api.routers.submissions.validate_repo_accessible",
               return_value=None):
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


def test_unauthenticated_cannot_access():
    client = _client()
    r = client.get("/submissions/1")
    assert r.status_code == 401, r.text


def test_faculty_can_view_student_submission():
    client = _client()
    fid = _mkuser("facpf@x.edu", "faculty123", "faculty", "Fac PF")
    sid = _mkuser("stupf@x.edu", "student123", "student", "Stu PF")
    hf = _login(client, "facpf@x.edu", "faculty123")
    hs = _login(client, "stupf@x.edu", "student123")

    aid = _seed_course_and_assignment(client, hf, "CS105", "Test Course",
                                      "GitHub Assignment")

    r = client.post("/courses/CS105/enroll", headers=hs)
    assert r.status_code == 200

    with patch("api.routers.submissions.validate_and_fetch_commit",
               mock_validate_and_fetch_commit), \
         patch("api.routers.submissions.validate_repo_accessible",
               return_value=None):
        r = client.post(
            "/submissions/github",
            headers=hs,
            data={
                "assignment_id": aid,
                "repo_url": "https://github.com/octocat/Hello-World",
                "commit_hash": "553c227e7f4d6c1f1f9a3b7b4d7c8e9f0a1b2c3d",
            },
        )
    assert r.status_code == 200, r.text
    sub_id = r.json()["id"]

    # Faculty can view student submission
    r = client.get(f"/submissions/{sub_id}", headers=hf)
    assert r.status_code == 200, r.text