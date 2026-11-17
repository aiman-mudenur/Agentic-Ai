"""Focused tests for student note-version access (Task 3)."""

import atexit
import os
import shutil
import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from db.base import SQLModel
from db.session import get_session as prod_get_session
from models.auth import User, Enrollment
from models.course import Course, Note
from models.assignment import Assignment
from api.security import get_password_hash

TMP_DIR = tempfile.mkdtemp(prefix="test_note_versions_")
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)
TMP_DB = os.path.join(TMP_DIR, "test_note_versions.db")
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


from datetime import datetime, timezone

_seeded = {}


def _seed_note():
    if _seeded:
        return _seeded["n1"], _seeded["n2"], _seeded["stu"], _seeded["fac"], _seeded["secret"]

    with Session(test_engine) as s:
        c = s.exec(select(Course).where(Course.course_code == "CS301")).first()
        if not c:
            c = Course(course_code="CS301", title="Test Course")
            s.add(c)
            s.commit()

        c2 = s.exec(select(Course).where(Course.course_code == "CS999")).first()
        if not c2:
            c2 = Course(course_code="CS999", title="Other Course")
            s.add(c2)
            s.commit()

        fac = s.exec(select(User).where(User.email == "facnv@x.edu")).first()
        if not fac:
            fac = User(username="facnv", email="facnv@x.edu", full_name="Fac NV",
                       role="faculty", hashed_password=get_password_hash("faculty123"))
            s.add(fac)
            s.commit()
            s.refresh(fac)

        stu = s.exec(select(User).where(User.email == "stunv@x.edu")).first()
        if not stu:
            stu = User(username="stunv", email="stunv@x.edu", full_name="Stu NV",
                       role="student", hashed_password=get_password_hash("student123"))
            s.add(stu)
            s.commit()
            s.refresh(stu)

        enr = s.exec(select(Enrollment).where(Enrollment.student_id == stu.id, Enrollment.course_code == "CS301")).first()
        if not enr:
            s.add(Enrollment(student_id=stu.id, course_code="CS301", enrolled_at=datetime.now(timezone.utc).isoformat()))
            s.commit()

        a = s.exec(select(Assignment).where(Assignment.created_by == fac.id, Assignment.course_code == "CS301")).first()
        if not a:
            a = Assignment(course_code="CS301", title="Test Assignment", max_marks=20.0, created_by=fac.id, is_published=True)
            s.add(a)
            s.commit()

        n1 = s.exec(select(Note).where(Note.course_code == "CS301", Note.title == "Test Note", Note.version == 1)).first()
        if not n1:
            n1 = Note(course_code="CS301", title="Test Note", file_name="v1.pdf", version=1, is_current=False, uploaded_by=fac.id)
            s.add(n1)
            s.commit()
            s.refresh(n1)

        n2 = s.exec(select(Note).where(Note.course_code == "CS301", Note.title == "Test Note", Note.version == 2)).first()
        if not n2:
            n2 = Note(course_code="CS301", title="Test Note", file_name="v2.pdf", version=2, is_current=True, parent_note_id=n1.id, uploaded_by=fac.id)
            s.add(n2)
            s.commit()
            s.refresh(n2)

        n_secret = s.exec(select(Note).where(Note.course_code == "CS999", Note.title == "Secret Note")).first()
        if not n_secret:
            n_secret = Note(course_code="CS999", title="Secret Note", file_name="secret.pdf", version=1, is_current=True, uploaded_by=fac.id)
            s.add(n_secret)
            s.commit()
            s.refresh(n_secret)

        _seeded["n1"] = n1.id
        _seeded["n2"] = n2.id
        _seeded["stu"] = stu.id
        _seeded["fac"] = fac.id
        _seeded["secret"] = n_secret.id
        return n1.id, n2.id, stu.id, fac.id, n_secret.id


def test_student_can_view_versions_for_enrolled_course():
    client = _client()
    n1_id, n2_id, stu_id, fac_id, secret_id = _seed_note()
    hs = _login(client, "stunv@x.edu", "student123")
    r = client.get(f"/notes/{n2_id}/versions", headers=hs)
    assert r.status_code == 200, r.text
    versions = r.json()
    assert len(versions) == 2
    assert versions[0]["version"] == 2
    assert versions[1]["version"] == 1


def test_student_cannot_view_versions_for_unenrolled_course():
    client = _client()
    n1_id, n2_id, stu_id, fac_id, secret_id = _seed_note()
    hs = _login(client, "stunv@x.edu", "student123")
    r = client.get(f"/notes/{secret_id}/versions", headers=hs)
    assert r.status_code == 403, r.text


def test_faculty_can_view_versions_for_taught_course():
    client = _client()
    n1_id, n2_id, stu_id, fac_id, secret_id = _seed_note()
    hf = _login(client, "facnv@x.edu", "faculty123")
    r = client.get(f"/notes/{n2_id}/versions", headers=hf)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2


def test_hod_can_view_versions():
    client = _client()
    n1_id, n2_id, stu_id, fac_id, secret_id = _seed_note()
    _mkuser("hodnv@x.edu", "hod123", "hod", "Hod NV")
    hh = _login(client, "hodnv@x.edu", "hod123")
    r = client.get(f"/notes/{n2_id}/versions", headers=hh)
    assert r.status_code == 200, r.text


def test_unauthenticated_cannot_view_versions():
    client = _client()
    n1_id, n2_id, stu_id, fac_id, secret_id = _seed_note()
    r = client.get(f"/notes/{n2_id}/versions")
    assert r.status_code == 401, r.text


def test_versions_not_found():
    client = _client()
    _seed_note()
    hs = _login(client, "stunv@x.edu", "student123")
    r = client.get("/notes/99999/versions", headers=hs)
    assert r.status_code == 404, r.text
