"""Lab 14 Phase 4 tests - End-to-End Capstone Demo integration.

Verifies the integrated demo workflow against the real components: the API
start/status/resume endpoints, the faculty approval interrupt, publication
governance, the demo script's artifacts, and the dashboard's status contract.

Isolation: every test uses a unique run id, a per-test temp database
(monkeypatched engine), and a per-test pipeline root (monkeypatched
``services.graph_service.REPO_ROOT``). No live Google/OAuth, no production
database, no writes into the repo's ``out/`` tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]

from course_organizer.graph.registry import get_global_registry
from course_organizer.graph.runner import run_pipeline
from course_organizer.graph.specialists import SpecialistResult


# ===========================================================================
# Helpers
# ===========================================================================

def _unique_run_id(prefix: str = "p4") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _failing_specialist(state, ctx) -> SpecialistResult:
    return SpecialistResult(
        step_id="w14",
        agent="Phase4 Failing Specialist",
        ok=False,
        summary="synthetic specialist failure injected by the phase 4 test",
        read=0,
    )


def _inject_domain(monkeypatch, domain: str, specialist) -> None:
    registry = get_global_registry()
    monkeypatch.setitem(registry._specialists, domain, specialist)


# ===========================================================================
# API fixtures: temp DB + temp pipeline root + faculty user
# ===========================================================================

@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient(app) with a temp DB, temp pipeline root, and a faculty login."""
    from fastapi.testclient import TestClient
    from sqlmodel import SQLModel, Session, create_engine

    from api.main import app
    from api.security import get_password_hash
    from models.auth import User

    tmp_db = tmp_path / "p4.db"
    test_engine = create_engine(
        f"sqlite:///{tmp_db}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(test_engine)

    # Redirect every DB access to the temp engine. get_session resolves the
    # ``engine`` global at call time, so patching db.session covers all users.
    monkeypatch.setattr("db.session.engine", test_engine)

    # Redirect pipeline checkpoints/artifacts to a temp root.
    monkeypatch.setattr("services.graph_service.REPO_ROOT", tmp_path)

    email = f"fac_{uuid.uuid4().hex[:8]}@example.edu"
    with Session(test_engine) as s:
        s.add(
            User(
                username=email.split("@")[0],
                email=email,
                full_name="Phase4 Faculty",
                role="faculty",
                hashed_password=get_password_hash("phase4-pass-123"),
            )
        )
        s.commit()

    client = TestClient(app)
    login = client.post(
        "/auth/login", data={"username": email, "password": "phase4-pass-123"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    return SimpleNamespace(
        client=client,
        headers={"Authorization": f"Bearer {token}"},
        root=tmp_path / "out" / "lab07",
    )


# ===========================================================================
# 1-3: A request starts a run with the requested domain; the graph reaches
#      the real approval interrupt; pending status via authorized mechanism.
# ===========================================================================

class TestStartAndStatus:
    def test_start_pipeline_with_requested_domain_reaches_gate(self, api):
        """POST /runs/pipeline/start runs to the gate with the chosen domain."""
        run_id = _unique_run_id("lab")
        resp = api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "lab"},
            headers=api.headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["awaiting_gate"] is True
        assert "workers" in body["nodes_ran"]
        assert "gate" not in body["nodes_ran"]  # gate has not run yet

    def test_status_shows_domain_and_interrupt(self, api):
        """The status endpoint reports the domain and the real interrupt."""
        run_id = _unique_run_id("proj")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "project"},
            headers=api.headers,
        )
        resp = api.client.get(f"/runs/{run_id}/status", headers=api.headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["domain"] == "project"
        assert body["awaiting_gate"] is True
        assert body["specialist_ok"] is True
        assert "workers" in body["nodes_ran"]

    def test_status_requires_faculty_token(self, api):
        """Status is role-guarded: no token -> 401."""
        run_id = _unique_run_id()
        resp = api.client.get(f"/runs/{run_id}/status")
        assert resp.status_code == 401

    def test_unknown_run_id_404(self, api):
        resp = api.client.get(
            f"/runs/{_unique_run_id('nope')}/status", headers=api.headers
        )
        assert resp.status_code == 404


# ===========================================================================
# 4-5: Explicit approval publishes; refusal does not publish.
# ===========================================================================

class TestApprovalAndRefusal:
    def test_approval_resumes_and_publishes(self, api):
        run_id = _unique_run_id("ok")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "theory"},
            headers=api.headers,
        )
        resp = api.client.post(
            "/runs/pipeline/resume",
            json={"run_id": run_id, "decision": "approve"},
            headers=api.headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "published"
        assert body["published"] is True
        run_dir = api.root / run_id
        md_files = list(run_dir.glob("*.md"))
        assert md_files, f"expected a published .md under {run_dir}"

    def test_refusal_does_not_publish(self, api):
        run_id = _unique_run_id("ref")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "theory"},
            headers=api.headers,
        )
        resp = api.client.post(
            "/runs/pipeline/resume",
            json={"run_id": run_id, "decision": "refuse"},
            headers=api.headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "refused"
        run_dir = api.root / run_id
        assert not list(run_dir.glob("*.md")), "refused run must not publish"

    def test_unknown_decision_rejected(self, api):
        """An unknown decision string is 422, never a silent approval."""
        run_id = _unique_run_id("baddec")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "theory"},
            headers=api.headers,
        )
        resp = api.client.post(
            "/runs/pipeline/resume",
            json={"run_id": run_id, "decision": "banana"},
            headers=api.headers,
        )
        assert resp.status_code == 422
        # The run is still at the gate, untouched.
        status = api.client.get(f"/runs/{run_id}/status", headers=api.headers)
        assert status.json()["awaiting_gate"] is True


# ===========================================================================
# 6: Failed specialist / unknown domain cannot publish (through the API).
# ===========================================================================

class TestGovernanceBlocksPublication:
    def test_unknown_domain_cannot_publish_via_api(self, api):
        """An unknown domain gates with specialist_ok=False; approval gets 409."""
        run_id = _unique_run_id("unk")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "astrology"},
            headers=api.headers,
        )
        status = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert status["specialist_ok"] is False
        assert status["domain_error"] is True

        resp = api.client.post(
            "/runs/pipeline/resume",
            json={"run_id": run_id, "decision": "approve"},
            headers=api.headers,
        )
        assert resp.status_code == 409, resp.text
        assert "publish refused" in resp.json()["detail"].lower()
        assert not list((api.root / run_id).glob("*.md"))

    def test_failed_specialist_cannot_publish_via_api(self, api, monkeypatch):
        """Injected failing specialist -> approve -> 409, no artifact."""
        _inject_domain(monkeypatch, "p4fail", _failing_specialist)
        run_id = _unique_run_id("failspec")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "p4fail"},
            headers=api.headers,
        )
        status = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert status["specialist_ok"] is False

        resp = api.client.post(
            "/runs/pipeline/resume",
            json={"run_id": run_id, "decision": "approve"},
            headers=api.headers,
        )
        assert resp.status_code == 409
        assert not list((api.root / run_id).glob("*.md"))

    def test_failed_validation_escalates_and_does_not_publish(self, tmp_path):
        """fail_until_attempt=99 escalates; no gate, no publication."""
        run = run_pipeline(
            course_code="CS201",
            run_id=_unique_run_id("esc"),
            root=tmp_path,
            output_dir=tmp_path / "publish",
            fail_until_attempt=99,
        )
        assert run.status.value == "escalated"
        assert run.published is False
        assert not list((tmp_path / "publish").glob("*.md"))


# ===========================================================================
# Course authorization: a faculty member only sees runs for courses they
# teach (assignments they created), per the app's existing convention.
# ===========================================================================

class TestCourseAuthorization:
    def test_course_authorized_reflects_teaching_assignment(self, api):
        run_id = _unique_run_id("authz")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "theory"},
            headers=api.headers,
        )
        # Before teaching CS201: not authorized.
        status = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert status["course_authorized"] is False

        # Create an assignment in CS201 -> now authorized.
        resp = api.client.post(
            "/assignments/",
            data={"course_code": "CS201", "title": "P4 Demo Assignment"},
            headers=api.headers,
        )
        assert resp.status_code == 200, resp.text
        status = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert status["course_authorized"] is True


# ===========================================================================
# 7 + 9 + 10: The demo script publishes real artifacts, is repeatable, and
#             needs no live Google/OAuth/production integration.
# ===========================================================================

_DEMO_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")},
    "LLM_BACKEND": "scripted",
    "EMBEDDING_BACKEND": "hashing",
}


def _run_demo(args, cwd=REPO, env=None):
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_lab14_demo", *args],
        capture_output=True, text=True, cwd=cwd, env=env or _DEMO_ENV,
        timeout=120,
    )


class TestDemoScript:
    def test_demo_approve_publishes_artifacts(self, tmp_path):
        """The documented approve flow writes a real .md under --root."""
        root = tmp_path / "demo"
        start = _run_demo(["--domain", "project", "--root", str(root)])
        assert start.returncode == 0, start.stdout + start.stderr
        # Extract run id from output line "run id:        demo-project-xxxx"
        run_id = None
        for line in start.stdout.splitlines():
            if line.strip().startswith("run id:"):
                run_id = line.split(":", 1)[1].strip()
        assert run_id, f"no run id in output:\n{start.stdout}"
        assert "PENDING FACULTY REVIEW" in start.stdout
        assert "Held at the gate" in start.stdout

        resume = _run_demo(
            ["--resume", run_id, "--decision", "approve", "--root", str(root)]
        )
        assert resume.returncode == 0, resume.stdout + resume.stderr
        md = list((root / run_id).glob("*.md"))
        assert md, f"no published .md under {root / run_id}"
        assert "OK: approved run published." in resume.stdout

    def test_demo_refuse_does_not_publish(self, tmp_path):
        root = tmp_path / "demo"
        start = _run_demo(["--domain", "theory", "--root", str(root)])
        assert start.returncode == 0
        run_id = next(
            l.split(":", 1)[1].strip()
            for l in start.stdout.splitlines()
            if l.strip().startswith("run id:")
        )
        resume = _run_demo(
            ["--resume", run_id, "--decision", "refuse", "--root", str(root)]
        )
        assert resume.returncode == 0
        assert not list((root / run_id).glob("*.md"))
        assert "OK: refusal recorded" in resume.stdout

    def test_demo_repeatable_no_checkpoint_inheritance(self, tmp_path):
        """Two default runs get distinct ids and independent checkpoints."""
        root = tmp_path / "demo"
        r1 = _run_demo(["--domain", "lab", "--root", str(root)])
        r2 = _run_demo(["--domain", "lab", "--root", str(root)])
        assert r1.returncode == 0 and r2.returncode == 0
        id1 = next(l.split(":", 1)[1].strip() for l in r1.stdout.splitlines()
                   if l.strip().startswith("run id:"))
        id2 = next(l.split(":", 1)[1].strip() for l in r2.stdout.splitlines()
                   if l.strip().startswith("run id:"))
        assert id1 != id2, "demo must not reuse run ids"
        assert (root / id1 / "graph.sqlite").exists()
        assert (root / id2 / "graph.sqlite").exists()

    def test_demo_rejects_existing_run_id(self, tmp_path):
        """Reusing a run id that has a checkpoint fails loudly (exit 3)."""
        root = tmp_path / "demo"
        run_id = _unique_run_id("dup")
        first = _run_demo(
            ["--domain", "theory", "--run-id", run_id, "--root", str(root)]
        )
        assert first.returncode == 0
        second = _run_demo(
            ["--domain", "theory", "--run-id", run_id, "--root", str(root)]
        )
        assert second.returncode == 3
        assert "already has a checkpoint" in second.stdout

    def test_demo_unknown_domain_rejected(self, tmp_path):
        res = _run_demo(["--domain", "astrology", "--root", str(tmp_path)])
        assert res.returncode == 3

    def test_demo_offline_no_live_integration(self, tmp_path):
        """Default demo runs with no GOOGLE_* env and scripted backend."""
        env = {k: v for k, v in _DEMO_ENV.items() if k != "LLM_BACKEND"}
        env.pop("GOOGLE_API_KEY", None)
        res = _run_demo(
            ["--domain", "theory", "--decision", "approve", "--root", str(tmp_path)],
            env=env,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "OK: approved run published." in res.stdout


# ===========================================================================
# 8: Dashboard status contract -- the page maps endpoint fields to the five
#    distinguishable states; the endpoint supplies those fields.
# ===========================================================================

class TestDashboardContract:
    def test_dashboard_page_consumes_status_fields(self):
        page = (REPO / "ui_roles" / "pages" / "01_Faculty_Dashboard.py").read_text(
            encoding="utf-8"
        )
        for field in ("awaiting_gate", "specialist_ok", "domain_error", "nodes_ran",
                      "course_authorized", "/runs/"):
            assert field in page, f"dashboard no longer reads {field}"
        for state_label in ("Awaiting faculty approval", "Published",
                            "Refused/escalated", "Failed validation"):
            assert state_label in page, f"dashboard lost state {state_label!r}"

    def test_status_endpoint_distinguishes_all_states(self, api):
        """Endpoint fields distinguish awaiting/published/refused/failed."""
        # Awaiting
        run_id = _unique_run_id("st")
        api.client.post(
            "/runs/pipeline/start",
            json={"course_code": "CS201", "run_id": run_id, "domain": "theory"},
            headers=api.headers,
        )
        s = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert s["awaiting_gate"] is True and s["status"] == "draft"

        # Published
        api.client.post("/runs/pipeline/resume",
                        json={"run_id": run_id, "decision": "approve"},
                        headers=api.headers)
        s = api.client.get(f"/runs/{run_id}/status", headers=api.headers).json()
        assert s["awaiting_gate"] is False and s["status"] == "published"

        # Refused
        run_id2 = _unique_run_id("st2")
        api.client.post("/runs/pipeline/start",
                        json={"course_code": "CS201", "run_id": run_id2,
                              "domain": "theory"},
                        headers=api.headers)
        api.client.post("/runs/pipeline/resume",
                        json={"run_id": run_id2, "decision": "refuse"},
                        headers=api.headers)
        s = api.client.get(f"/runs/{run_id2}/status", headers=api.headers).json()
        assert s["status"] == "refused"

        # Failed specialist
        run_id3 = _unique_run_id("st3")
        api.client.post("/runs/pipeline/start",
                        json={"course_code": "CS201", "run_id": run_id3,
                              "domain": "astrology"},
                        headers=api.headers)
        s = api.client.get(f"/runs/{run_id3}/status", headers=api.headers).json()
        assert s["specialist_ok"] is False and s["domain_error"] is True



