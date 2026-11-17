"""Realtime enrollment + submit-to-faculty flow (temp DB, no workspace pollution)."""

import atexit
import io
import os
import shutil
import tempfile

os.environ["AUTO_SWARM_ON_SUBMIT"] = "false"
os.environ["AUTO_GRADE_ON_SUBMIT"] = "false"

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from db.base import SQLModel
from db.session import get_session as prod_get_session

#: A per-run directory, not a fixed name in the shared temp dir: a crashed or
#: still-running earlier pytest leaves that file locked on Windows, and then
#: *collection* of this module fails with PermissionError before any test runs.
TMP_DIR = tempfile.mkdtemp(prefix="test_enroll_realtime_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_enroll_realtime.db")
test_engine = create_engine(f"sqlite:///{TMP_DB}", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(test_engine)


def override_session():
    with Session(test_engine) as session:
        yield session


def _client():
    from api.main import app

    app.dependency_overrides[prod_get_session] = override_session
    return TestClient(app)


def _mkuser(client, email, password, role, name):
    from api.security import get_password_hash
    from models.auth import User

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


def test_open_course_creation_and_self_enrollment():
    client = _client()
    _mkuser(client, "fac1@x.edu", "faculty123", "faculty", "Fac One")
    _mkuser(client, "stu1@x.edu", "student123", "student", "Stu One")
    hf = _login(client, "fac1@x.edu", "faculty123")
    hs = _login(client, "stu1@x.edu", "student123")

    # faculty creates a brand-new course, no restrictions
    r = client.post("/courses/", headers=hf, json={"course_code": "CS301", "title": "New Course",
                                                   "units": [{"unit_number": "1", "title": "Intro"}]})
    assert r.status_code == 200, r.text

    # catalog visible; student enrolls
    r = client.get("/courses/catalog", headers=hs)
    assert r.status_code == 200 and any(c["course_code"] == "CS301" for c in r.json())
    r = client.post("/courses/CS301/enroll", headers=hs)
    assert r.status_code == 200, r.text
    # idempotent re-enroll
    r = client.post("/courses/CS301/enroll", headers=hs)
    assert r.status_code == 200 and r.json()["already"] is True
    # unknown course -> 404
    r = client.post("/courses/NOPE/enroll", headers=hs)
    assert r.status_code == 404


def test_submit_visible_to_faculty_with_auto_draft():
    client = _client()
    _mkuser(client, "fac2@x.edu", "faculty123", "faculty", "Fac Two")
    _mkuser(client, "stu2@x.edu", "student123", "student", "Stu Two")
    hf = _login(client, "fac2@x.edu", "faculty123")
    hs = _login(client, "stu2@x.edu", "student123")

    client.post("/courses/", headers=hf, json={"course_code": "CS302", "title": "Flow Course"})
    assert client.post("/courses/CS302/enroll", headers=hs).status_code == 200

    r = client.post("/assignments/", headers=hf,
                    data={"course_code": "CS302", "title": "Essay", "max_marks": 20,
                          "rubric_text": "networks report lab analysis"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    # unenrolled student cannot submit
    _mkuser(client, "stu3@x.edu", "student123", "student", "Stu Three")
    ho = _login(client, "stu3@x.edu", "student123")
    r = client.post("/submissions/submit", headers=ho, data={"assignment_id": aid},
                    files={"file": ("x.txt", io.BytesIO(b"hi"))})
    assert r.status_code == 403

    # enrolled submit works and is instantly visible to faculty
    r = client.post("/submissions/submit", headers=hs, data={"assignment_id": aid},
                    files={"file": ("essay.txt", io.BytesIO(b"lab report on networks covering all rubric points with detailed analysis and results"))})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["student_id"] is not None

    r = client.get("/faculty/submissions", headers=hf, params={"assignment_id": aid})
    assert r.status_code == 200
    assert any(s["id"] == sid and s["status"] == "submitted" for s in r.json()), r.text

    # Lab orchestration: run the submit pipeline synchronously (swarm off, grader on)
    os.environ["AUTO_GRADE_ON_SUBMIT"] = "true"
    from services.swarm_trigger import run_submit_pipeline

    def fac():
        return Session(test_engine)

    out = run_submit_pipeline("CS302", aid, sid, session_factory=fac)
    assert out["auto_grade"] is not None and out["auto_grade"]["awarded_marks"] >= 0, out

    # faculty now sees a draft grade ready to approve
    r = client.get("/faculty/submissions", headers=hf, params={"assignment_id": aid})
    got = [s for s in r.json() if s["id"] == sid][0]
    assert got["status"] == "draft_graded", got
    assert got["awarded_marks"] is not None

    # approve -> student sees it (progress line-chart payload has dates)
    r = client.post(f"/submissions/{sid}/approve", headers=hf)
    assert r.status_code == 200
    r = client.get("/submissions/", headers=hs)
    mine = [s for s in r.json() if s["id"] == sid][0]
    assert mine["awarded_marks"] is not None and mine["submitted_at"], mine
    assert mine["similarity_score"] is None  # never leaked to students


def test_google_endpoints_fail_closed_without_config(monkeypatch):
    client = _client()
    import services.google_oauth as go

    monkeypatch.setattr(go, "client_config", lambda: None)
    r = client.get("/google/login-url", params={"purpose": "login", "role": "student"})
    assert r.status_code == 503
    assert "Google Cloud Console" in r.json()["detail"]


def test_catalog_public_and_hod_stats_with_admin_role():
    client = _client()
    _mkuser(client, "hod9@x.edu", "hod12345", "hod", "Hod Nine")
    _mkuser(client, "adm9@x.edu", "admin12345", "admin", "Adm Nine")
    _mkuser(client, "fac9@x.edu", "faculty123", "faculty", "Fac Nine")
    hh = _login(client, "hod9@x.edu", "hod12345")
    ha = _login(client, "adm9@x.edu", "admin12345")
    hf = _login(client, "fac9@x.edu", "faculty123")

    # catalog is public — no auth header at all
    r = client.get("/courses/catalog")
    assert r.status_code == 200 and isinstance(r.json(), list)

    # faculty creates course + assignment; HoD stats reflect them
    assert client.post("/courses/", headers=hf, json={"course_code": "CS909", "title": "Stats"}).status_code == 200
    assert client.post("/assignments/", headers=hf, data={"course_code": "CS909", "title": "HW", "max_marks": 10}).status_code == 200
    r = client.get("/hod/dashboard", headers=hh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_courses"] >= 1 and body["total_assignments"] >= 1
    assert "submission_breakdown" in body and "courses" in body

    # admin role sees the same overview (was 403 before)
    r = client.get("/hod/dashboard", headers=ha)
    assert r.status_code == 200, r.text

    # student payload exposes approved_at so the line graph can filter
    _mkuser(client, "stu9@x.edu", "student123", "student", "Stu Nine")
    hs = _login(client, "stu9@x.edu", "student123")
    assert client.post("/courses/CS909/enroll", headers=hs).status_code == 200
    aid = [a["id"] for a in client.get("/student/assignments", headers=hs).json() if a["course_code"] == "CS909"][0]
    sid = client.post("/submissions/submit", headers=hs, data={"assignment_id": aid},
                      files={"file": ("w.txt", io.BytesIO(b"work " * 100))}).json()["id"]
    client.post(f"/submissions/{sid}/grade", headers=hf, params={"awarded_marks": 8})
    client.post(f"/submissions/{sid}/approve", headers=hf)
    mine = [s for s in client.get("/submissions/", headers=hs).json() if s["id"] == sid][0]
    assert mine["status"] == "graded" and mine["awarded_marks"] == 8
    assert mine.get("approved_at"), "approved_at must reach students for the graph filter"


def test_submit_pipeline_records_swarm_evidence_and_dev_activity():
    client = _client()
    _mkuser(client, "fac8@x.edu", "faculty123", "faculty", "Fac Eight")
    _mkuser(client, "stu8@x.edu", "student123", "student", "Stu Eight")
    hf = _login(client, "fac8@x.edu", "faculty123")
    hs = _login(client, "stu8@x.edu", "student123")

    client.post("/courses/", headers=hf, json={"course_code": "CS808", "title": "Evidence"})
    client.post("/courses/CS808/enroll", headers=hs)
    aid = client.post("/assignments/", headers=hf,
                      data={"course_code": "CS808", "title": "Lab", "max_marks": 10}).json()["id"]
    sid = client.post("/submissions/submit", headers=hs, data={"assignment_id": aid},
                      files={"file": ("w.txt", io.BytesIO(b"evidence " * 100))}).json()["id"]

    os.environ["AUTO_GRADE_ON_SUBMIT"] = "true"
    from services.swarm_trigger import run_submit_pipeline

    def fac():
        return Session(test_engine)

    # The Lab 8 swarm organizes the seed-corpus course (CS201 exists in
    # data/seed); the run evidence is still stamped onto this submission.
    os.environ["AUTO_SWARM_ON_SUBMIT"] = "true"
    out = run_submit_pipeline("CS201", aid, sid, session_factory=fac)
    assert out["swarm"] is not None and out["swarm"].get("run_id"), out
    assert out["auto_grade"] is not None

    # evidence landed on the submission + audit log
    with Session(test_engine) as s:
        from models.submission import Submission
        from models.auth import AuditLog

        sub = s.get(Submission, sid)
        assert sub.run_id and sub.run_id == out["swarm"]["run_id"], sub.run_id
        actions = [l.action for l in s.exec(
            select(AuditLog).where(AuditLog.entity_type == "submission", AuditLog.entity_id == sid)).all()]
        assert "swarm_completed" in actions, actions
        assert "auto_draft_grade_entered" in actions, actions

    # developer endpoint exposes the trace
    r = client.get("/dev/activity", headers=hf)
    assert r.status_code == 200, r.text
    row = [t for t in r.json()["trace"] if t["submission_id"] == sid][0]
    assert row["swarm_run_id"] == out["swarm"]["run_id"], row
    assert row["draft_marks"] is not None and "similarity" in row
    assert any(l["action"] == "swarm_completed" for l in r.json()["agent_audit"])
    r = client.get("/dev/runs", headers=hf)
    assert r.status_code == 200 and isinstance(r.json(), list)

    # agent repository: live counts, never hardcoded
    r = client.get("/dev/inventory", headers=hf)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["counts"]["specialist_agents"] == 6, inv["counts"]
    assert inv["counts"]["lab_agents"] == 4, inv["counts"]
    assert inv["counts"]["tools"] == 4, inv["counts"]
    assert inv["counts"]["skills"] == 2, inv["counts"]
    tool_names = {t["name"] for t in inv["tools"]}
    assert {"read_notes_store", "read_assignment_rubrics", "compute_scope_validation",
            "read_submission_records"} <= tool_names, tool_names
    assert inv["framework"]["versions"].get("langgraph"), inv["framework"]["versions"]
    assert inv["framework"]["versions"].get("langchain"), inv["framework"]["versions"]
    assert any("StateGraph" in u["piece"] for u in inv["framework"]["uses"])


def test_classroom_transport_uses_discovery_param_names():
    """Pin googleapiclient kwargs to the live discovery doc: courses.get takes
    ``id``; courseWork / courseWorkMaterials / topics lists take ``courseId``."""
    from course_organizer.connector.adapters import RestGoogleWorkspaceTransport

    seen: dict = {}

    class Req:
        def __init__(self, resp=None, **kw):
            seen.update(kw)
            self._resp = resp if resp is not None else {"id": "1"}

        def execute(self):
            return self._resp

    class Res:
        def __init__(self, allowed, resp=None):
            self._allowed = allowed
            self._resp = resp

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)

            def method(**kw):
                for k in kw:
                    assert k in self._allowed, f"unexpected kwarg {k}"
                return Req(self._resp, **kw)

            return method

        def list_next(self, *a):
            return None

    class Courses:
        def get(self, **kw):
            assert set(kw) == {"id"}, kw
            return Req(**kw)

        def courseWork(self):
            return Res({"courseId"}, {"courseWork": [{"id": "w1", "title": "HW"}]})

        def courseWorkMaterials(self):
            return Res({"courseId"}, {"courseWorkMaterial": [{"id": "m1", "title": "BDA Study material"}]})

        def announcements(self):
            return Res({"courseId"}, {"announcements": [{"id": "a1", "text": "hello"}]})

        def topics(self):
            return Res({"courseId"}, {"topic": []})

    class Svc:
        def courses(self):
            return Courses()

    t = RestGoogleWorkspaceTransport(client_id="", client_secret="", token_path="",
                                     classroom_service=Svc())
    assert t.course("123") == {"id": "1"}
    # discovery-shaped payloads must flow through (wrong response keys => [])
    assert t.course_work("123") == [{"id": "w1", "title": "HW"}]
    assert t.course_materials("123") == [{"id": "m1", "title": "BDA Study material"}]
    assert t.announcements("123") == [{"id": "a1", "text": "hello"}]
    assert t.topics("123") == {}

    # portal scope set covers student coursework, topics and announcements
    from services.google_oauth import CLASSROOM_SCOPES

    for scope in ("https://www.googleapis.com/auth/classroom.coursework.me.readonly",
                  "https://www.googleapis.com/auth/classroom.topics.readonly",
                  "https://www.googleapis.com/auth/classroom.announcements.readonly",
                  "https://www.googleapis.com/auth/drive.readonly"):
        assert scope in CLASSROOM_SCOPES, scope


def test_classroom_drive_file_parsing_and_download():
    """Drive attachments are extracted from materials and downloaded as bytes."""
    from services.classroom import material_drive_files, download_drive_file

    m = {"materials": [{"driveFile": {"driveFile": {"id": "f1", "title": "HDFS_Replication.pdf"}}},
                       {"link": {"url": "https://x"}}]}
    assert material_drive_files(m) == [{"id": "f1", "title": "HDFS_Replication.pdf"}]
    assert material_drive_files({}) == []

    class Files:
        def get(self, **kw):
            assert kw["fileId"] == "f1"

            class R:
                def execute(self):
                    return {"id": "f1", "name": "HDFS_Replication.pdf", "mimeType": "application/pdf"}

            return R()

        def get_media(self, **kw):
            class R:
                def execute(self):
                    return b"%PDF-1.4 fake"

            return R()

    class Drive:
        def files(self):
            return Files()

    import services.classroom as gc

    orig = gc.drive_service_for
    gc.drive_service_for = lambda uid: Drive()
    try:
        data, name, mime = download_drive_file(7, "f1")
    finally:
        gc.drive_service_for = orig
    assert data == b"%PDF-1.4 fake" and name == "HDFS_Replication.pdf" and mime == "application/pdf"


def test_import_normalizes_code_and_stores_source_url(monkeypatch):
    from api.routers.classroom import _normalize_course_code

    assert _normalize_course_code("CS_AI2027 (891792891951)") == "CS_AI2027"
    assert _normalize_course_code("  CS201  ") == "CS201"
    assert _normalize_course_code("(891792891951)") == "(891792891951)"
    assert _normalize_course_code("") == ""

    import services.classroom as gc

    bundle = {
        "course": {"name": "CS_AI2027"},
        "coursework": [],
        "materials": [{"id": "m9", "title": "BDA Study material",
                       "materials": [{"link": {"url": "https://classroom.google.com/m9"}}]}],
        "announcements": [],
        "topics": {},
        "warnings": {},
    }
    monkeypatch.setattr(gc, "get_work", lambda uid, cid: bundle)

    client = _client()
    _mkuser(client, "facn@x.edu", "faculty123", "faculty", "Fac Norm")
    hf = _login(client, "facn@x.edu", "faculty123")
    # pasted dropdown label is normalized to the short code
    r = client.post("/classroom/import", headers=hf,
                    json={"classroom_course_id": "x", "course_code": "CS_AI2027 (891792891951)",
                          "import_coursework": False, "import_materials": True, "publish": True})
    assert r.status_code == 200, r.text
    assert r.json()["course_code"] == "CS_AI2027"
    assert r.json()["notes_created"] == 1

    with Session(test_engine) as s:
        from models.course import Note

        note = s.exec(select(Note).where(Note.course_code == "CS_AI2027")).first()
        assert note is not None and note.source_url == "https://classroom.google.com/m9", note


def test_init_migrates_legacy_db_missing_columns(tmp_path):
    """Old DB files (pre source_url / approval / versioning columns) keep
    working after upgrade: init_db adds the columns, backfills, preserves rows."""
    from sqlalchemy import create_engine as sa_create
    from sqlalchemy import text as _t

    from db.init_db import _ensure_columns, _backfill_note_defaults

    legacy = tmp_path / "legacy.db"
    eng = sa_create(f"sqlite:///{legacy}")
    with eng.begin() as conn:
        conn.execute(_t(
            "CREATE TABLE note (id INTEGER PRIMARY KEY, course_code VARCHAR NOT NULL, "
            "title VARCHAR NOT NULL, uploaded_by INTEGER NOT NULL)"))
        conn.execute(_t(
            "INSERT INTO note (course_code, title, uploaded_by) VALUES ('CS1', 'Old note', 9)"))
        conn.execute(_t(
            "CREATE TABLE submission (id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, "
            "student_id INTEGER NOT NULL, course_code VARCHAR NOT NULL)"))
    _ensure_columns(eng)
    _backfill_note_defaults(eng)

    from models.course import Note
    from models.submission import Submission

    with Session(eng) as s:
        old = s.exec(select(Note)).first()
        assert old.title == "Old note" and old.version == 1 and old.is_current is True
        assert old.source_url is None
        # new-column writes + reads work on the migrated file
        s.add(Note(course_code="CS1", title="New", uploaded_by=1,
                   source_url="https://classroom.google.com/m9"))
        s.add(Submission(assignment_id=1, student_id=1, course_code="CS1"))
        s.commit()
    with Session(eng) as s:
        new = s.exec(select(Note).where(Note.title == "New")).first()
        assert new.source_url == "https://classroom.google.com/m9"
        assert s.exec(select(Submission)).first() is not None


def test_backfill_recovers_note_links_from_audit_log(tmp_path):
    """Pre-source_url imports become openable: the audit provenance URL is
    written back onto the note row at startup."""
    from sqlalchemy import create_engine as sa_create

    from db.init_db import _backfill_note_defaults

    legacy = tmp_path / "links.db"
    eng = sa_create(f"sqlite:///{legacy}")
    with eng.begin() as conn:
        from sqlalchemy import text as _t
        conn.execute(_t(
            "CREATE TABLE note (id INTEGER PRIMARY KEY, course_code VARCHAR NOT NULL, "
            "title VARCHAR NOT NULL, uploaded_by INTEGER NOT NULL)"))
        conn.execute(_t(
            "CREATE TABLE auditlog (id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, "
            "action VARCHAR NOT NULL, entity_type VARCHAR NOT NULL, entity_id INTEGER NOT NULL, "
            "details VARCHAR, timestamp VARCHAR NOT NULL)"))
        conn.execute(_t(
            "INSERT INTO note (course_code, title, uploaded_by) VALUES ('CS1', 'BDA Study material', 9)"))
        conn.execute(_t(
            "INSERT INTO auditlog (actor_id, action, entity_type, entity_id, details, timestamp) VALUES "
            "(9, 'note_imported', 'note', 1, "
            "'Imported from Classroom material m1 (https://classroom.google.com/m1) by f@x', "
            "'2026-10-13T00:00:00+00:00')"))

    from db.init_db import _ensure_columns

    _ensure_columns(eng)
    _backfill_note_defaults(eng)

    from models.course import Note

    with Session(eng) as s:
        healed = s.get(Note, 1)
        assert healed.source_url == "https://classroom.google.com/m1", healed.source_url
