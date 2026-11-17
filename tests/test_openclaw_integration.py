"""Mocked tests for the optional OpenClaw integration.

No live Gateway is required: the WebSocket client is patched, and the API uses a
temporary SQLite database.
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
from models.assignment import Assignment
from models.auth import User, Enrollment, AuditLog
from models.openclaw import OpenClawApproval
from models.submission import Submission
from api.security import get_password_hash

TMP_DIR = tempfile.mkdtemp(prefix="test_openclaw_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_openclaw.db")
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
    username = email.split("@")[0] + role[:2]
    with Session(test_engine) as s:
        from sqlmodel import select
        u = s.exec(select(User).where(User.username == username)).first()
        if u:
            return u.id
        u = User(username=username, email=email, full_name=name,
                 role=role, hashed_password=get_password_hash(password))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _login(client, email, password):
    r = client.post("/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _seed_faculty_course():
    from sqlmodel import select
    fid = _mkuser("ocfac@x.edu", "faculty123", "faculty", "OC Fac")
    sid = _mkuser("ocstu@x.edu", "student123", "student", "OC Stu")
    with Session(test_engine) as s:
        a = s.exec(select(Assignment).where(Assignment.course_code == "CS101", Assignment.created_by == fid)).first()
        if not a:
            a = Assignment(course_code="CS101", title="OC Assignment", created_by=fid, max_marks=20.0, is_published=True)
            s.add(a)
            s.commit()
            s.refresh(a)
        aid = a.id
        if not s.exec(select(Enrollment).where(Enrollment.student_id == sid, Enrollment.course_code == "CS101")).first():
            s.add(Enrollment(student_id=sid, course_code="CS101", enrolled_at="2026-01-01T00:00:00Z"))
        if not s.exec(select(Submission).where(Submission.assignment_id == aid)).first():
            s.add(Submission(assignment_id=aid, student_id=sid, course_code="CS101",
                             submission_type="github", github_repo_url="https://github.com/o/r",
                             commit_hash="abc123", status="submitted", is_late=True))
        s.commit()
    return fid, sid, aid


def test_health_endpoint_reports_configured_state():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with patch("api.routers.openclaw.get_health_sync", return_value={
        "enabled": False, "configured": False, "connected": False, "status": "not_configured",
        "gateway_url": "ws://127.0.0.1:18789",
    }):
        r = client.get("/openclaw/health", headers=h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "not_configured"
        assert "config" in data


def test_tools_allowlist_requires_faculty():
    client = _client()
    _seed_faculty_course()
    hs = _login(client, "ocstu@x.edu", "student123")
    r = client.get("/openclaw/tools", headers=hs)
    assert r.status_code == 403


def test_faculty_can_list_tools():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.get("/openclaw/tools", headers=h)
    assert r.status_code == 200, r.text
    tools = r.json()["allowed_tools"]
    assert "list_assignments" in tools
    assert "get_submission_status" in tools
    assert "find_late_submissions" in tools
    assert "read_github_activity" in tools
    assert "summarize_course_notes" in tools


def test_invoke_list_assignments_scoped_to_faculty():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.post("/openclaw/tools/invoke", headers=h, json={"name": "list_assignments", "args": {}})
    assert r.status_code == 200, r.text
    assert len(r.json()["assignments"]) >= 1


def test_invoke_unknown_tool_denied():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.post("/openclaw/tools/invoke", headers=h, json={"name": "run_shell", "args": {}})
    assert r.status_code == 403


def test_find_late_submissions():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.post("/openclaw/tools/invoke", headers=h, json={"name": "find_late_submissions", "args": {}})
    assert r.status_code == 200, r.text
    assert len(r.json()["late_submissions"]) >= 1


def test_github_activity_read():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.post("/openclaw/tools/invoke", headers=h, json={"name": "read_github_activity", "args": {}})
    assert r.status_code == 200, r.text
    assert r.json()["github_activity"][0]["repo_url"] == "https://github.com/o/r"


def test_tool_call_writes_audit_log():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    r = client.post("/openclaw/tools/invoke", headers=h, json={"name": "list_assignments", "args": {}})
    assert r.status_code == 200, r.text
    with Session(test_engine) as s:
        log = s.exec(
            select(AuditLog).where(AuditLog.action == "openclaw_tool_list_assignments")
        ).first()
        assert log is not None


def test_other_faculty_cannot_access_assignment():
    client = _client()
    _, _, aid = _seed_faculty_course()
    _mkuser("ocfac2@x.edu", "faculty456", "faculty", "OC Fac 2")
    h2 = _login(client, "ocfac2@x.edu", "faculty456")
    r = client.post("/openclaw/tools/invoke", headers=h2,
                    json={"name": "get_submission_status", "args": {"assignment_id": aid}})
    assert r.status_code == 403, r.text


def test_invalid_tool_args_return_400():
    client = _client()
    _, _, aid = _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with Session(test_engine) as s:
        sid = s.exec(select(Submission).where(Submission.assignment_id == aid)).first().id
    r = client.post("/openclaw/tools/invoke", headers=h,
                    json={"name": "set_grade", "args": {"submission_id": sid, "awarded_marks": "not-a-number"}})
    assert r.status_code == 400, r.text


def test_set_grade_creates_approval_not_mutation():
    client = _client()
    _, _, aid = _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with Session(test_engine) as s:
        sub = s.exec(select(Submission).where(Submission.assignment_id == aid)).first()
        sid = sub.id
    r = client.post("/openclaw/tools/invoke", headers=h,
                    json={"name": "set_grade", "args": {"submission_id": sid, "awarded_marks": 15, "feedback": "ok"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["action"] == "set_grade"
    # Submission must NOT be mutated yet
    with Session(test_engine) as s:
        sub = s.get(Submission, sid)
        assert sub.status == "submitted"
        assert sub.awarded_marks is None


def test_approve_grade_applies_after_human_approval():
    client = _client()
    _, _, aid = _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with Session(test_engine) as s:
        sub = s.exec(select(Submission).where(Submission.assignment_id == aid)).first()
        sid = sub.id
    r = client.post("/openclaw/tools/invoke", headers=h,
                    json={"name": "set_grade", "args": {"submission_id": sid, "awarded_marks": 15}})
    approval_id = r.json()["approval_id"]
    r2 = client.post(f"/openclaw/approvals/{approval_id}/approve", headers=h)
    assert r2.status_code == 200, r2.text
    with Session(test_engine) as s:
        sub = s.get(Submission, sid)
        assert sub.status == "draft_graded"
        assert float(sub.awarded_marks) == 15.0
        approval = s.get(OpenClawApproval, approval_id)
        assert approval.status == "approved"


def test_chat_requires_configured_gateway():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with patch("api.routers.openclaw.chat_sync", return_value={"ok": False, "error": "OpenClaw is not configured."}):
        r = client.post("/openclaw/chat", headers=h, json={"message": "hello"})
        assert r.status_code == 502


def test_chat_success_passthrough():
    client = _client()
    _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with patch("api.routers.openclaw.chat_sync", return_value={"ok": True, "reply": "hi", "state": "final"}):
        r = client.post("/openclaw/chat", headers=h, json={"message": "hello"})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "reply": "hi", "state": "final"}


def test_student_cannot_chat():
    client = _client()
    _seed_faculty_course()
    hs = _login(client, "ocstu@x.edu", "student123")
    r = client.post("/openclaw/chat", headers=hs, json={"message": "hello"})
    assert r.status_code == 403


def test_reject_approval():
    client = _client()
    _, _, aid = _seed_faculty_course()
    h = _login(client, "ocfac@x.edu", "faculty123")
    with Session(test_engine) as s:
        sub = s.exec(select(Submission).where(Submission.assignment_id == aid)).first()
        sid = sub.id
        # Reset state that other tests may have mutated in the shared test DB.
        sub.status = "submitted"
        sub.awarded_marks = None
        sub.feedback = None
        sub.approved_at = None
        sub.approved_by = None
        s.add(sub)
        s.commit()
    r = client.post("/openclaw/tools/invoke", headers=h,
                    json={"name": "set_grade", "args": {"submission_id": sid, "awarded_marks": 10}})
    approval_id = r.json()["approval_id"]
    r2 = client.post(f"/openclaw/approvals/{approval_id}/reject", headers=h)
    assert r2.status_code == 200, r2.text
    with Session(test_engine) as s:
        approval = s.get(OpenClawApproval, approval_id)
        assert approval.status == "rejected"
        sub = s.get(Submission, sid)
        assert sub.status == "submitted"
