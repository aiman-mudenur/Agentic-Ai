"""Multi-role accounts: an account may hold several roles and act as one at a time.

The security property under test is that the *choice* is client-side but the
*grant* is not. A client may only ever move between roles an operator already
put on the account; it cannot widen its own access, and a role removed by an
operator stops working immediately.

Nothing touches the network, a production database, or Google.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from models.auth import KNOWN_ROLES, User, encode_roles


# ---------------------------------------------------------------------------
# Model-level behaviour
# ---------------------------------------------------------------------------

class TestAssignedRoles:
    """What an account is allowed to act as."""

    def test_null_roles_falls_back_to_the_role_column(self):
        """Accounts created before multi-role support keep working unmodified."""
        u = User(username="a", email="a@x.edu", role="hod",
                 hashed_password="h", roles=None)
        assert u.assigned_roles() == ["hod"]

    def test_json_list_is_read_back(self):
        u = User(username="a", email="a@x.edu", role="faculty",
                 hashed_password="h", roles='["faculty", "hod"]')
        assert u.assigned_roles() == ["faculty", "hod"]

    def test_unknown_and_blank_values_are_dropped(self):
        """A tampered column cannot smuggle an unrecognised role into auth."""
        u = User(username="a", email="a@x.edu", role="faculty",
                 hashed_password='h', roles='["faculty", "superuser", "", "HOD"]')
        assert u.assigned_roles() == ["faculty", "hod"]

    def test_duplicates_are_removed_keeping_order(self):
        u = User(username="a", email="a@x.edu", role="faculty",
                 hashed_password="h", roles='["hod", "faculty", "hod"]')
        assert u.assigned_roles() == ["hod", "faculty"]

    def test_unparsable_json_falls_back_to_the_role_column(self):
        u = User(username="a", email="a@x.edu", role="student",
                 hashed_password="h", roles="not json at all")
        assert u.assigned_roles() == ["student"]

    def test_json_that_is_not_a_list_falls_back(self):
        u = User(username="a", email="a@x.edu", role="faculty",
                 hashed_password="h", roles='{"role": "hod"}')
        assert u.assigned_roles() == ["faculty"]


class TestEncodeRoles:
    def test_normalises_and_deduplicates(self):
        assert encode_roles(["Faculty", "hod", "faculty"]) == '["faculty", "hod"]'

    def test_unknown_role_is_dropped(self):
        assert encode_roles(["hod", "superuser"]) == '["hod"]'

    def test_empty_result_is_rejected(self):
        with pytest.raises(ValueError):
            encode_roles(["superuser"])

    def test_known_roles_are_the_authoritative_set(self):
        assert set(KNOWN_ROLES) == {"student", "faculty", "hod", "admin"}


# ---------------------------------------------------------------------------
# API fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient(app) on a temp DB with one dual-role account."""
    from fastapi.testclient import TestClient
    from sqlmodel import SQLModel, Session, create_engine

    from api.main import app
    from api.security import get_password_hash

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'roles.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr("db.session.engine", test_engine)

    email = f"dual_{uuid.uuid4().hex[:8]}@example.edu"
    with Session(test_engine) as s:
        s.add(
            User(
                username=email.split("@")[0],
                email=email,
                full_name="Dual Role Account",
                role="faculty",
                roles=encode_roles(["faculty", "hod"]),
                hashed_password=get_password_hash("dual-pass-123"),
            )
        )
        s.commit()

    client = TestClient(app)
    login = client.post("/auth/login", data={"username": email, "password": "dual-pass-123"})
    assert login.status_code == 200, login.text
    t = login.json()

    return SimpleNamespace(
        client=client,
        engine=test_engine,
        email=email,
        login=t,
        headers={"Authorization": f"Bearer {t['access_token']}"},
    )


# ---------------------------------------------------------------------------
# Login reports every granted role
# ---------------------------------------------------------------------------

class TestLoginReportsRoles:

    def test_login_acts_as_the_primary_role(self, api):
        assert api.login["role"] == "faculty"

    def test_login_lists_every_granted_role(self, api):
        assert api.login["roles"] == ["faculty", "hod"]

    def test_single_role_account_lists_exactly_one(self, api):
        from api.security import get_password_hash

        with Session(api.engine) as s:
            solo = User(username="solo1", email="solo1@example.edu", role="student",
                        hashed_password=get_password_hash("solo-pass-123"))
            s.add(solo)
            s.commit()
        r = api.client.post("/auth/login", data={"username": "solo1@example.edu",
                                                 "password": "solo-pass-123"})
        assert r.status_code == 200, r.text
        assert r.json()["roles"] == ["student"]


# ---------------------------------------------------------------------------
# Switching between granted roles
# ---------------------------------------------------------------------------

class TestSwitchRole:

    def _switch(self, api, role):
        return api.client.post("/auth/switch-role", json={"role": role},
                               headers=api.headers)

    def test_switch_to_a_granted_role_succeeds(self, api):
        r = self._switch(api, "hod")
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "hod"
        assert r.json()["roles"] == ["faculty", "hod"]

    def test_switch_changes_which_door_opens(self, api):
        """One hat at a time: the other role's endpoints close behind you."""
        blocked = api.client.get("/hod/dashboard", headers=api.headers)
        assert blocked.status_code == 403

        switched = self._switch(api, "hod").json()
        hod_headers = {"Authorization": f"Bearer {switched['access_token']}"}

        assert api.client.get("/hod/dashboard", headers=hod_headers).status_code == 200
        # ...and now the faculty door is the one that is shut.
        assert api.client.get("/faculty/dashboard", headers=hod_headers).status_code == 403
        # The original faculty token still works.
        assert api.client.get("/faculty/dashboard", headers=api.headers).status_code == 200

    def test_switch_to_an_ungranted_role_is_refused(self, api):
        """A client cannot widen its own access."""
        r = self._switch(api, "student")
        assert r.status_code == 403, r.text

    def test_switch_to_an_invented_role_is_refused(self, api):
        r = self._switch(api, "superuser")
        assert r.status_code == 403, r.text

    def test_switch_requires_authentication(self, api):
        r = api.client.post("/auth/switch-role", json={"role": "hod"})
        assert r.status_code == 401

    def test_single_role_account_cannot_switch(self, api):
        from api.security import get_password_hash

        with Session(api.engine) as s:
            s.add(User(username="solo2", email="solo2@example.edu", role="faculty",
                       hashed_password=get_password_hash("solo-pass-123")))
            s.commit()
        solo = api.client.post("/auth/login", data={"username": "solo2@example.edu",
                                                    "password": "solo-pass-123"}).json()
        r = api.client.post("/auth/switch-role", json={"role": "hod"},
                            headers={"Authorization": f"Bearer {solo['access_token']}"})
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# The stored account is never rewritten by acting as a role
# ---------------------------------------------------------------------------

class TestBaseRoleIsPreserved:

    def test_acting_as_the_other_role_does_not_rewrite_the_column(self, api):
        """`role` stays the primary role no matter which hat is worn.

        get_current_user reflects the active role for the request without
        dirtying the row, because routers commit mid-request and a dirty value
        would overwrite the account's primary role in the database.
        """
        switched = api.client.post("/auth/switch-role", json={"role": "hod"},
                                   headers=api.headers).json()
        hod_headers = {"Authorization": f"Bearer {switched['access_token']}"}
        assert api.client.get("/hod/dashboard", headers=hod_headers).status_code == 200

        with Session(api.engine) as s:
            fresh = s.exec(
                select(User).where(User.email == api.email)
            ).first()
            assert fresh.role == "faculty", "primary role was overwritten"
            assert fresh.roles == '["faculty", "hod"]'


# ---------------------------------------------------------------------------
# Operator removal takes effect immediately
# ---------------------------------------------------------------------------

class TestDemotionTakesEffect:

    def test_a_token_for_a_removed_role_stops_working(self, api, monkeypatch):
        """An operator removing a role invalidates outstanding tokens for it."""
        switched = api.client.post("/auth/switch-role", json={"role": "hod"},
                                   headers=api.headers).json()
        hod_headers = {"Authorization": f"Bearer {switched['access_token']}"}
        assert api.client.get("/hod/dashboard", headers=hod_headers).status_code == 200

        # The operator demotes the account back to faculty-only.
        with Session(api.engine) as s:
            u = s.exec(select(User).where(User.email == api.email)).first()
            u.roles = encode_roles(["faculty"])
            s.add(u)
            s.commit()

        r = api.client.get("/hod/dashboard", headers=hod_headers)
        assert r.status_code == 401, r.text

    def test_the_primary_role_survives_the_demotion(self, api):
        switched = api.client.post("/auth/switch-role", json={"role": "hod"},
                                   headers=api.headers).json()
        hod_headers = {"Authorization": f"Bearer {switched['access_token']}"}

        with Session(api.engine) as s:
            u = s.exec(select(User).where(User.email == api.email)).first()
            u.roles = encode_roles(["faculty"])
            s.add(u)
            s.commit()

        assert api.client.get("/faculty/dashboard", headers=api.headers).status_code == 200
        assert api.client.get("/hod/dashboard", headers=hod_headers).status_code == 401
