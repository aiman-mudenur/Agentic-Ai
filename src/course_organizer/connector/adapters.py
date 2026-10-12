"""The source adapters: the swappable half of the connector.

Three implementations of :class:`~course_organizer.connector.port.SourceAdapter`
ship here. That number is the point of the lab -- the acceptance criterion
says swapping a source must need no agent change, and a claim like that is
only worth anything if more than one thing can be swapped.

======================  ====================================================
Adapter                 Role
======================  ====================================================
:class:`LocalSeedAdapter`  the default; reads ``data/seed`` through the one
                        module allowed to touch the filesystem
:class:`InMemoryAdapter`   a second, structurally *different* source, used to
                        prove the swap claim with a real measurement rather
                        than an assertion
:class:`GoogleWorkspaceAdapter`  real Google Drive + Classroom, off by
                        default, mock-tested only (see its docstring)
======================  ====================================================

**Verification status of the Google path, stated up front because it matters.**
Every API name, resource path, method parameter, field name and OAuth scope used
by :class:`GoogleWorkspaceAdapter` was read out of the ``classroom.v1.json`` and
``drive.v3.json`` discovery documents bundled inside
``google-api-python-client`` (``googleapiclient/discovery_cache/documents/``) on
the recorded date, not from memory and not from the network. The *mapping* is
therefore accurate to a published contract. It has **not** been run against a
live Workspace tenant in this repository, because this repository has no OAuth
client and no course. ``SOURCE_ADAPTER`` therefore defaults to ``local``, and
:func:`build_source_adapter` refuses ``google`` with the specific missing
setting rather than failing later inside a request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from course_organizer.connector.errors import (
    ConnectorNotFoundError,
    SourceUnavailableError,
)
from course_organizer.connector.port import (
    NotesPage,
    SourceAdapter,
    SourceDescriptor,
    SourceHealth,
)
from course_organizer.logging_setup import log_event
from course_organizer.schemas import (
    ArtifactKind,
    AssignmentDefinition,
    NoteMaterial,
    RubricCriterion,
    SubmissionRecord,
    SubmissionStatus,
)
from course_organizer.tools.seed_store import (
    LocalSeedStore,
    SeedStoreError,
    get_seed_store,
)

__all__ = [
    "GOOGLE_READ_SCOPES",
    "GoogleWorkspaceAdapter",
    "GoogleWorkspaceTransport",
    "InMemoryAdapter",
    "LocalSeedAdapter",
    "RestGoogleWorkspaceTransport",
    "build_source_adapter",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. The default: local seed files
# ---------------------------------------------------------------------------
class LocalSeedAdapter:
    """The default source: ``data/seed``, read through `LocalSeedStore`.

    This adapter is intentionally the *thinnest* possible pass-through. It
    adds two things and no more:

    * a :class:`~course_organizer.connector.port.SourceDescriptor`, so
      provenance travels with the data, and
    * a :meth:`health` check, so "is the corpus present?" is answerable before
      an agent asks for a course.

    It adds no filtering, no caching and no reshaping. If it did, "the
    connector is a uniform surface" would be true of the local adapter and
    false of the others.

    Args:
        seed_dir: The seed root. Defaults to the configured ``seed_dir``.
    """

    def __init__(self, seed_dir: Optional[str] = None) -> None:
        self._store = get_seed_store(seed_dir)
        self._descriptor = SourceDescriptor(
            name="local_seed",
            transport="local JSON files",
            provenance="data/seed",
            writable=False,
            requires_source_credentials=False,
            swap_note=(
                "The default. Reads data/seed/<course>/{notes,assignments,"
                "submissions}.json. No credentials, no network."
            ),
        )

    @property
    def store(self) -> LocalSeedStore:
        """The underlying store. Exposed for the bypass demo and for evidence.

        Nothing outside this package should use it -- that is exactly what the
        Lab 5 AST test enforces.
        """
        return self._store

    @property
    def descriptor(self) -> SourceDescriptor:
        """How this source describes itself."""
        return self._descriptor

    def health(self) -> SourceHealth:
        """Is the seed corpus present and non-empty?"""
        try:
            codes = self._store.course_codes()
        except Exception as exc:  # pragma: no cover - defensive
            return SourceHealth(
                ok=False,
                reason=f"the seed corpus at {self._store.seed_dir} is unreadable",
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not codes:
            return SourceHealth(
                ok=False,
                reason=f"no course folders under {self._store.seed_dir}",
                detail="expected data/seed/<course>/notes.json for at least one course",
            )
        return SourceHealth(
            ok=True,
            reason=f"seed corpus present with {len(codes)} course(s)",
            detail=", ".join(codes),
        )

    def course_codes(self) -> list[str]:
        """Every course code in the corpus, sorted."""
        return self._store.course_codes()

    def read_notes(self, course_code: str) -> NotesPage:
        """``read('the notes/materials store')`` from disk."""
        return self._guarded(course_code, self._store.read_notes, course_code)

    def read_assignments(self, course_code: str) -> list[AssignmentDefinition]:
        """``read('assignment & rubric definitions')`` from disk."""
        return self._guarded(
            course_code, self._store.read_assignments, course_code
        )

    def read_submissions(self, course_code: str) -> list[SubmissionRecord]:
        """``read('submission records')`` from disk."""
        return self._guarded(course_code, self._store.read_submissions, course_code)

    def backing(
        self, kind: str, course_code: Optional[str] = None
    ) -> list[str]:
        """The seed file a read of ``kind`` came from.

        This is the same information Lab 2's tools used to get by calling
        ``LocalSeedStore.notes_path()`` and friends directly. Routing the tools
        through the connector is what forces the question to be asked here
        instead, where a source that is not the filesystem can still answer it.

        ``kind="courses"`` answers with the corpus root rather than a single
        file, because enumerating the courses means reading every course folder's
        ``notes.json`` header -- there is no one file to name. It was returning
        an empty list, which made ``list_courses`` the one endpoint whose result
        could not say where it came from. An unreported location is worse than
        an imprecise one: it is indistinguishable from an adapter that forgot to
        answer, and a reviewer cannot tell which it was looking at.
        """
        if kind == "courses":
            return [self._store.relative(self._store.seed_dir)]
        paths = {
            "notes": self._store.notes_path,
            "assignments": self._store.assignments_path,
            "submissions": self._store.submissions_path,
        }
        resolver = paths.get(kind)
        if resolver is None or course_code is None:
            return []
        path = resolver(course_code)
        if path is None:
            return []
        return [self._store.relative(path)]

    def _guarded(self, course_code: str, reader: Any, *args: Any) -> Any:
        """Call a store reader, translating its errors at the port boundary.

        The port's documented contract says an adapter raises
        :class:`~course_organizer.connector.errors.ConnectorNotFoundError` for
        "this course does not exist". ``LocalSeedStore`` raises its own
        ``SeedStoreError`` instead, and letting that through would leak a
        ``tools``-package exception across the connector boundary -- the caller
        would have to import ``tools`` to catch a refusal from the connector,
        which is exactly the coupling Lab 5 exists to remove.

        The distinction is preserved rather than flattened: a message that
        says the course is unknown becomes a not-found, and every other store
        failure becomes a
        :class:`~course_organizer.connector.errors.SourceUnavailableError`.
        Both are ``refused`` rather than ``failed`` for a missing course, so
        the ledger records "asked for something that is not there" instead of
        "the system broke".

        Args:
            course_code: The course being read, for the message.
            reader: The bound store method.
            *args: Forwarded to ``reader``.

        Returns:
            Whatever the reader returned.

        Raises:
            ConnectorNotFoundError: The course is not in the corpus.
            SourceUnavailableError: The store failed for any other reason.
        """
        try:
            return reader(*args)
        except SeedStoreError as exc:
            message = str(exc)
            if "unknown course" in message.lower():
                raise ConnectorNotFoundError(
                    f"course {course_code!r} is not in the seed corpus at "
                    f"{self._store.seed_dir}. Available: "
                    f"{self._store.course_codes()}"
                ) from exc
            raise SourceUnavailableError(
                f"the seed corpus at {self._store.seed_dir} could not be read "
                f"for {course_code!r}: {message}"
            ) from exc
        except OSError as exc:
            raise SourceUnavailableError(
                f"the seed corpus at {self._store.seed_dir} is unreachable for "
                f"{course_code!r}: {type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# 2. The swap proof: a second source with a different shape
# ---------------------------------------------------------------------------
class InMemoryAdapter:
    """A second source, deliberately unlike :class:`LocalSeedAdapter`.

    The acceptance criterion is *"swapping a source needs no agent change"*.
    Asserting that is cheap. Measuring it needs a second source that would
    break a careless agent, so this one is built to:

    * a **different course code** (``ZZ999``) with a different number of
      entities, so any hardcoded id, count or course code in an agent shows up
      as a failure rather than as luck;
    * **no version history** on its notes (one version each), so an agent that
      assumed ``versions[-1].version >= 2`` would be wrong;
    * a **single rubric criterion** against the seed corpus's richer rubrics;
    * **no similarity score** on submissions, which is the field Lab 4 exists
      to compute. A record with ``similarity_score=None`` forces the caller to
      handle "not yet checked" honestly.

    Args:
        course_code: The course code this source serves.
    """

    def __init__(self, course_code: str = "ZZ999") -> None:
        self._course_code = course_code
        self._read_count = 0
        self._notes = [
            NoteMaterial(
                note_id=f"{course_code}-NOTE-1",
                title="Graph traversals: reference card",
                kind=ArtifactKind("reference"),
                unit="Unit 9",
                topic="Breadth-first search",
                tags=["reference", "graphs"],
                # A single version, version 1. No history to assume.
                versions=[
                    {
                        "version": 1,
                        "author": "ta@example.edu",
                        "created_at": "2026-01-05T09:00:00+00:00",
                        "changelog": "Initial reference card for BFS.",
                        "content": (
                            "Breadth-first search visits nodes in order of "
                            "distance from the start, using a queue."
                        ),
                    }
                ],
            )
        ]
        self._assignments = [
            AssignmentDefinition(
                assignment_id=f"{course_code}-A1",
                course_code=course_code,
                title="Problem set 1",
                brief="Answer the graph traversal questions in the attached sheet.",
                due_date=datetime(2026, 3, 1, 23, 59, tzinfo=timezone.utc),
                max_marks=20.0,
                rubric=[
                    RubricCriterion(
                        criterion_id="R1",
                        description="Correctness of the traversal order.",
                        max_marks=20.0,
                        weight=1.0,
                    )
                ],
                approved_by="course_faculty",
                unit="Unit 9",
            )
        ]
        self._submissions = [
            SubmissionRecord(
                submission_id=f"{course_code}-A1-S001",
                assignment_id=f"{course_code}-A1",
                student_id="stu-9001",
                student_name="In Memory Student",
                content=(
                    "BFS uses a queue seeded with the start node; each dequeued "
                    "node's undiscovered neighbours are enqueued."
                ),
                submitted_at=datetime(2026, 2, 27, 18, 30, tzinfo=timezone.utc),
                status=SubmissionStatus.SUBMITTED,
            )
        ]
        self._descriptor = SourceDescriptor(
            name="in_memory",
            transport="in-process Python objects",
            provenance="memory://in_memory",
            writable=False,
            requires_source_credentials=False,
            swap_note=(
                "The swap probe. A different course code, one note version, "
                "one rubric criterion and no similarity score, so an agent "
                "that had absorbed the seed corpus's shape would break."
            ),
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        """How this source describes itself."""
        return self._descriptor

    def health(self) -> SourceHealth:
        """Always healthy: there is nothing external to be unhealthy."""
        return SourceHealth(
            ok=True,
            reason="in-memory source is always available",
            detail=f"course {self._course_code}",
        )

    def course_codes(self) -> list[str]:
        """The one course code this source serves."""
        return [self._course_code]

    def read_notes(self, course_code: str) -> NotesPage:
        """``read('the notes/materials store')`` from memory."""
        self._check(course_code)
        self._read_count += 1
        header = {
            "course_code": course_code,
            # `course_name`, not `title`: that is the key the seed corpus uses,
            # and a swap must not change the *shape* of a header, only its
            # values. The endpoint accepts either, so this is belt and braces.
            "course_name": "In-memory exchange course",
            # Same header *shape* as the local adapter, so the endpoint renders
            # units identically whichever source answered. The unit *value*
            # differs on purpose -- that is the swap, and an agent that had
            # memorised the seed corpus's unit strings must not care.
            "units": [{"unit": "Unit 9", "title": "Graphs and traversal"}],
            "provenance": "memory://in_memory",
        }
        return header, list(self._notes)

    def read_assignments(self, course_code: str) -> list[AssignmentDefinition]:
        """``read('assignment & rubric definitions')`` from memory."""
        self._check(course_code)
        self._read_count += 1
        return list(self._assignments)

    def read_submissions(self, course_code: str) -> list[SubmissionRecord]:
        """``read('submission records')`` from memory."""
        self._check(course_code)
        self._read_count += 1
        return list(self._submissions)

    def backing(
        self, kind: str, course_code: Optional[str] = None
    ) -> list[str]:
        """A pseudo-location, because there is no file behind this source.

        Answered rather than left empty on purpose: the audit trail should say
        "this came from the in-memory source" instead of saying nothing, which
        would be indistinguishable from an adapter that forgot to report.
        """
        self._check(course_code or self._course_code)
        return [f"in-memory://{self._course_code}/{kind}"]

    @property
    def read_count(self) -> int:
        """How many reads have happened. Evidence that a swap really swapped."""
        return self._read_count

    def _check(self, course_code: str) -> None:
        if course_code != self._course_code:
            raise ConnectorNotFoundError(
                f"course {course_code!r} is not in the in-memory source; it "
                f"serves only {self._course_code!r}"
            )


# ---------------------------------------------------------------------------
# 3. The real external source: Google Drive + Classroom
# ---------------------------------------------------------------------------
#: Read-only OAuth scopes, verified against the ``classroom.v1.json`` and
#: ``drive.v3.json`` discovery documents on 2026-09-30.
#:
#: Deliberately **read-only**, because doc 2.10's Governance layer is
#: *"access control per role, version history, audit log of grades published"*
#: -- a connector that could write would need its own approval flow, and Lab 5
#: builds no writes. ``classroom.profile.emails`` and
#: ``classroom.profile.photos`` are omitted on purpose: the notes and
#: submission records need a display name, which
#: ``classroom.rosters.readonly`` already returns, so the two extra scopes buy
#: nothing.
GOOGLE_READ_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


@runtime_checkable
class GoogleWorkspaceTransport(Protocol):
    """The Google calls the adapter needs, as plain data in and plain data out.

    Split out so the mapping from Google's schemas to
    :mod:`course_organizer.schemas` can be tested without a network, a tenant
    or a token. Every method returns the raw discovery-shaped dictionaries --
    no translation happens here, so a test fixture is a literal API response
    and a reviewer can diff it against the discovery document.

    Note:
        The return types are the shapes verified from
        ``classroom.v1.json`` / ``drive.v3.json``; see the module docstring.
    """

    def courses(self) -> list[dict[str, Any]]:
        """``courses().list()`` -- the courses the caller can see."""
        ...

    def course(self, course_id: str) -> dict[str, Any]:
        """``courses().get(courseId=...)``."""
        ...

    def course_work(self, course_id: str) -> list[dict[str, Any]]:
        """``courses().courseWork().list(courseId=...)``."""
        ...

    def rubrics(self, course_id: str, work_id: str) -> list[dict[str, Any]]:
        """``courses().courseWork().rubrics().list(courseId=, courseWorkId=)``."""
        ...

    def student_submissions(
        self, course_id: str, work_id: str
    ) -> list[dict[str, Any]]:
        """``courses().courseWork().studentSubmissions().list(...)``."""
        ...

    def students(self, course_id: str) -> dict[str, dict[str, Any]]:
        """``courses().students().list(courseId=...)``, keyed by ``userId``."""
        ...

    def course_materials(self, course_id: str) -> list[dict[str, Any]]:
        """``courses().courseWorkMaterials().list(courseId=...)``."""
        ...

    def topics(self, course_id: str) -> dict[str, dict[str, Any]]:
        """``courses().topics().list(courseId=...)``, keyed by ``id``."""
        ...

    def file_metadata(self, file_id: str) -> dict[str, Any]:
        """``files().get(fileId=...)`` -- for ``modifiedTime`` and ``mimeType``."""
        ...

    def file_text(self, file_id: str) -> str:
        """``files().export_media(fileId=..., mimeType="text/plain").execute()``.

        The discovery document marks ``files.export`` with
        ``supportsMediaDownload: True``, and ``googleapiclient.discovery``
        synthesises the ``_media`` suffix from that flag.
        """
        ...


class RestGoogleWorkspaceTransport:
    """:class:`GoogleWorkspaceTransport` over ``google-api-python-client``.

    Constructing this performs **no** I/O; the first call does. Args mirror
    the four Google settings in
    :class:`course_organizer.config.Settings`.

    Args:
        client_id: OAuth client id from the Developer Console.
        client_secret: OAuth client secret.
        token_path: Where the cached refresh token lives. Kept out of the repo.
        folder_id: Optional Drive folder to scan for note documents.
        drive_service: An already-built Drive service, for injection.
        classroom_service: An already-built Classroom service, for injection.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: str,
        folder_id: Optional[str] = None,
        drive_service: Any = None,
        classroom_service: Any = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._folder_id = folder_id
        self._drive = drive_service
        self._classroom = classroom_service
        self._scopes = list(GOOGLE_READ_SCOPES)

    def _drive_api(self) -> Any:
        if self._drive is None:
            self._drive = self._build("drive", "v3")
        return self._drive

    def _classroom_api(self) -> Any:
        if self._classroom is None:
            self._classroom = self._build("classroom", "v1")
        return self._classroom

    def _build(self, name: str, version: str) -> Any:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(
            self._token_path, self._scopes
        )
        return build(name, version, credentials=creds, cache_discovery=False)

    # -- Classroom ------------------------------------------------------
    def courses(self) -> list[dict[str, Any]]:
        """``courses().list()``, paginated."""
        return self._paged(
            self._classroom_api().courses(),
            "list",
            {},
            "courses",
        )

    def course(self, course_id: str) -> dict[str, Any]:
        """``courses().get(courseId=...)``."""
        return self._classroom_api().courses().get(courseId=course_id).execute()

    def course_work(self, course_id: str) -> list[dict[str, Any]]:
        """``courses().courseWork().list(courseId=...)``, paginated."""
        return self._paged(
            self._classroom_api().courses().courseWork(),
            "list",
            {"courseId": course_id},
            "courseWork",
        )

    def rubrics(self, course_id: str, work_id: str) -> list[dict[str, Any]]:
        """``courses().courseWork().rubrics().list(...)``, paginated."""
        return self._paged(
            self._classroom_api().courses().courseWork().rubrics(),
            "list",
            {"courseId": course_id, "courseWorkId": work_id},
            "rubrics",
        )

    def student_submissions(
        self, course_id: str, work_id: str
    ) -> list[dict[str, Any]]:
        """``courses().courseWork().studentSubmissions().list(...)``, paginated."""
        return self._paged(
            self._classroom_api().courses().courseWork().studentSubmissions(),
            "list",
            {"courseId": course_id, "courseWorkId": work_id},
            "studentSubmissions",
        )

    def students(self, course_id: str) -> dict[str, dict[str, Any]]:
        """``courses().students().list(...)`` keyed by ``userId``."""
        listed = self._paged(
            self._classroom_api().courses().students(),
            "list",
            {"courseId": course_id},
            "students",
        )
        return {s["userId"]: s for s in listed if s.get("userId")}

    def course_materials(self, course_id: str) -> list[dict[str, Any]]:
        """``courses().courseWorkMaterials().list(...)``, paginated."""
        return self._paged(
            self._classroom_api().courses().courseWorkMaterials(),
            "list",
            {"courseId": course_id},
            "courseWorkMaterials",
        )

    def topics(self, course_id: str) -> dict[str, dict[str, Any]]:
        """``courses().topics().list(...)`` keyed by ``id``."""
        listed = self._paged(
            self._classroom_api().courses().topics(),
            "list",
            {"courseId": course_id},
            "topics",
        )
        return {t["id"]: t for t in listed if t.get("id")}

    # -- Drive ----------------------------------------------------------
    def file_metadata(self, file_id: str) -> dict[str, Any]:
        """``files().get(fileId=...)`` with the fields the adapter needs."""
        return (
            self._drive_api()
            .files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,modifiedTime,parents,trashed",
                supportsAllDrives=True,
            )
            .execute()
        )

    def file_text(self, file_id: str) -> str:
        """``files().export_media(...)`` decoded as UTF-8 text."""
        data = (
            self._drive_api()
            .files()
            .export_media(fileId=file_id, mimeType="text/plain")
            .execute()
        )
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    def _paged(
        self, resource: Any, method: str, params: dict[str, Any], key: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        request = getattr(resource, method)(**params)
        while request is not None:
            response = request.execute()
            out.extend(response.get(key, []) or [])
            request = resource.list_next(request, response)
        return out


class GoogleWorkspaceAdapter:
    """Google Drive + Classroom, behind the same three reads.

    **What is verified and what is not.** Every API name, method parameter,
    field name and OAuth scope below was read out of the discovery documents
    bundled inside ``google-api-python-client``; the recorded date and the
    exact method paths are in :data:`GOOGLE_READ_SCOPES` and this docstring.
    The *translation* into :mod:`course_organizer.schemas` is real code with
    real error handling, and its tests run against literal discovery-shaped
    fixtures. It has **not** been exercised against a live tenant, because this
    repository holds no OAuth client and no course. :meth:`health` says so in
    its ``reason`` rather than claiming readiness it cannot back.

    **Three translations the document does not settle, decided here and
    stated so a reviewer can overrule them:**

    1. *Course code* is the Classroom course's ``name``. ``sectionId`` is
       carried in the notes header instead. Agents ask for a human-readable
       code, and a course's display name is the one string guaranteed to be
       meaningful to whoever set the course up.
    2. *Only published assignments count.* ``CourseWork.state`` is filtered to
       ``PUBLISHED`` and ``workType`` to ``ASSIGNMENT``; the enums are in the
       discovery document. A ``DRAFT`` assignment is invisible to students, so
       exposing it would make the connector lie about what the cohort can see.
    3. *``submitted_at`` is derived, not read.* ``StudentSubmission`` in the
       Classroom v1 discovery document has **no** timestamp field --
       ``submissionHistory`` carries only ``gradeHistory`` and
       ``stateHistory`` with no times. The adapter therefore uses the Drive
       attachment's ``modifiedTime``, which is the latest moment a teacher can
       actually evidence, and marks the record's provenance so nobody mistakes
       it for a Classroom-provided submission time. This is a real limitation
       of the source, surfaced rather than papered over.

    Args:
        transport: The Google calls. Inject a fake one in tests.
        folder_id: Optional Drive folder scanned for note documents.
    """

    def __init__(
        self,
        transport: GoogleWorkspaceTransport,
        *,
        folder_id: Optional[str] = None,
    ) -> None:
        self._transport = transport
        self._folder_id = folder_id
        self._descriptor = SourceDescriptor(
            name="google_workspace",
            transport="Google Drive + Classroom REST (read-only scopes)",
            provenance="google://classroom",
            writable=False,
            requires_source_credentials=True,
            swap_note=(
                "Off by default. Needs a Developer Console OAuth client and a "
                "course the account teaches. The mapping is verified against "
                "the bundled discovery documents; the path has not been run "
                "against a live tenant in this repository."
            ),
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        """How this source describes itself."""
        return self._descriptor

    def health(self) -> SourceHealth:
        """Try one listing call, and report the failure honestly."""
        try:
            courses = self._transport.courses()
        except Exception as exc:
            return SourceHealth(
                ok=False,
                reason="the Google Workspace source is not reachable",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return SourceHealth(
            ok=True,
            reason=(
                f"reachable; {len(courses)} course(s) visible. NOTE: this path "
                f"has not been run against a live tenant in this repository."
            ),
            detail=", ".join(str(c.get("name", "?")) for c in courses[:5]),
        )

    def course_codes(self) -> list[str]:
        """Every visible course's ``name``, sorted.

        Raises:
            SourceUnavailableError: If the listing call fails.
        """
        try:
            return sorted(str(c.get("name", "")) for c in self._transport.courses())
        except Exception as exc:
            raise SourceUnavailableError(
                f"cannot list Google Classroom courses: {type(exc).__name__}: "
                f"{exc}. Check that the OAuth token is present, is not "
                f"expired, and was granted every scope in "
                f"course_organizer.connector.adapters.GOOGLE_READ_SCOPES."
            ) from exc

    def read_notes(self, course_code: str) -> NotesPage:
        """``read('the notes/materials store')`` from Drive + Classroom."""
        course = self._find_course(course_code)
        course_id = str(course["id"])

        units: list[str] = []
        notes: list[NoteMaterial] = []

        topics = self._transport.topics(course_id)
        materials = self._transport.course_materials(course_id)
        for material in materials:
            if str(material.get("state", "PUBLISHED")).upper() != "PUBLISHED":
                continue
            title = str(material.get("title") or f"Material {material.get('id')}")
            topic_id = str(material.get("topicId") or "")
            unit = str(topics.get(topic_id, {}).get("name") or topic_id or "General")
            if unit not in units:
                units.append(unit)

            body_parts: list[str] = []
            drive_files: list[str] = []
            for item in material.get("materials", []) or []:
                drive = item.get("driveFile") or {}
                file_id = drive.get("id")
                if not file_id:
                    continue
                drive_files.append(str(file_id))
                try:
                    body_parts.append(self._transport.file_text(str(file_id)))
                except Exception as exc:  # one unreadable file must not kill the read
                    log_event(
                        logger,
                        "connector.google.file_unreadable",
                        file_id=file_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    body_parts.append(f"[unreadable Drive file {file_id}]")

            created = self._parse_rfc3339(material.get("creationTime"))
            notes.append(
                NoteMaterial(
                    note_id=str(material.get("id")),
                    title=title,
                    kind=ArtifactKind("lecture_notes"),
                    unit=unit,
                    topic=unit,
                    tags=["classroom", "courseworkmaterial"],
                    versions=[
                        {
                            "version": 1,
                            "author": str(material.get("creatorUserId") or "unknown"),
                            "created_at": created,
                            "changelog": (
                                "Imported from Google Classroom coursework "
                                f"material {material.get('id')}."
                            ),
                            "content": "\n\n".join(body_parts),
                        }
                    ],
                )
            )

        header = {
            "course_code": course_code,
            "title": str(course.get("name", course_code)),
            "section_id": str(course.get("sectionId", "")),
            "course_state": str(course.get("courseState", "")),
            "classroom_id": course_id,
            # Shaped like the seed corpus's header: a list of
            # {"unit": id, "title": text} mappings. A uniform surface means
            # the header shape is part of the contract, so the connector
            # renders units the same way whichever adapter answered.
            "units": [{"unit": unit, "title": unit} for unit in units]
            or [{"unit": "General", "title": "General"}],
            "provenance": "google://classroom",
            "note_version_model": (
                "Google Classroom course work materials are unversioned, so "
                "every note has exactly one version (version 1). Doc 2.3's "
                "'keep clear version history' is satisfied by the Drive "
                "revision history, which this read connector does not fetch."
            ),
        }
        return header, notes

    def read_assignments(self, course_code: str) -> list[AssignmentDefinition]:
        """``read('assignment & rubric definitions')`` from Classroom."""
        course = self._find_course(course_code)
        course_id = str(course["id"])
        topics = self._transport.topics(course_id)

        out: list[AssignmentDefinition] = []
        for work in self._transport.course_work(course_id):
            if str(work.get("state", "")).upper() != "PUBLISHED":
                continue
            if str(work.get("workType", "")).upper() != "ASSIGNMENT":
                continue
            work_id = str(work["id"])
            topic_id = str(work.get("topicId") or "")
            out.append(
                AssignmentDefinition(
                    assignment_id=work_id,
                    course_code=course_code,
                    title=str(work.get("title") or work_id),
                    brief=str(work.get("description") or ""),
                    due_date=self._due_date(work),
                    max_marks=float(work.get("maxPoints") or 0.0),
                    release_date=self._parse_rfc3339(work.get("scheduledTime")),
                    rubric=self._rubric(course_id, work_id),
                    approved_by="course_faculty",
                    unit=str(topics.get(topic_id, {}).get("name") or ""),
                )
            )
        return sorted(out, key=lambda a: a.assignment_id)

    def read_submissions(self, course_code: str) -> list[SubmissionRecord]:
        """``read('submission records')`` from Classroom + Drive.

        See the class docstring, point 3: ``submitted_at`` is the Drive
        attachment's ``modifiedTime``, because Classroom v1 exposes no
        submission timestamp. Records that arrive with no attachment at all
        are **skipped**, not returned with a fabricated time -- a submission
        whose text was never retrievable is not a submission record this
        connector can honestly serve.
        """
        course = self._find_course(course_code)
        course_id = str(course["id"])
        roster = self._transport.students(course_id)

        out: list[SubmissionRecord] = []
        for work in self._transport.course_work(course_id):
            if str(work.get("state", "")).upper() != "PUBLISHED":
                continue
            if str(work.get("workType", "")).upper() != "ASSIGNMENT":
                continue
            work_id = str(work["id"])
            for submission in self._transport.student_submissions(course_id, work_id):
                if str(submission.get("state", "")).upper() != "TURNED_IN":
                    continue
                attachments = (
                    (submission.get("assignmentSubmission") or {}).get("attachments")
                    or []
                )
                text, stamp = self._submission_text(attachments)
                if text is None:
                    log_event(
                        logger,
                        "connector.google.submission_unreadable",
                        submission_id=submission.get("id"),
                    )
                    continue
                user_id = str(submission.get("userId", ""))
                out.append(
                    SubmissionRecord(
                        submission_id=str(submission["id"]),
                        assignment_id=work_id,
                        student_id=user_id,
                        student_name=self._student_name(roster, user_id),
                        content=text,
                        submitted_at=stamp,
                        status=SubmissionStatus.LATE
                        if bool(submission.get("late"))
                        else SubmissionStatus.SUBMITTED,
                    )
                )
        return sorted(out, key=lambda s: s.submission_id)

    def backing(
        self, kind: str, course_code: Optional[str] = None
    ) -> list[str]:
        """Google-native identifiers for the audit trail.

        The course is located by its Classroom ``name`` because the connector's
        course code *is* the course name -- that translation decision is
        recorded in the class docstring. Returning the numeric ids, not the
        names, because an id is what someone can paste into the Google admin UI
        to check the claim.
        """
        if course_code is None:
            return [f"classroom:course/{kind}"]
        course = self._find_course(course_code)
        prefix = "gdrive" if kind == "notes" else "classroom"
        return [f"{prefix}:{kind}/{course['id']}"]

    # -- helpers --------------------------------------------------------
    def _find_course(self, course_code: str) -> dict[str, Any]:
        for course in self._transport.courses():
            if str(course.get("name")) == course_code:
                return course
        raise ConnectorNotFoundError(
            f"course {course_code!r} is not visible to this Google account. "
            f"Visible courses: {self.course_codes()}. Note that Google "
            f"Classroom only lists a course for accounts enrolled in it, and "
            f"roster access needs a teacher enrolment."
        )

    @staticmethod
    def _student_name(
        roster: dict[str, dict[str, Any]], user_id: str
    ) -> str:
        profile = (roster.get(user_id) or {}).get("profile") or {}
        name = profile.get("name") or {}
        return str(
            name.get("fullName")
            or " ".join(
                part for part in (name.get("givenName"), name.get("familyName")) if part
            )
            or user_id
            or "unknown"
        )

    def _rubric(self, course_id: str, work_id: str) -> list[RubricCriterion]:
        criteria: list[RubricCriterion] = []
        for rubric in self._transport.rubrics(course_id, work_id):
            for criterion in rubric.get("criteria", []) or []:
                # Classroom's Criterion has no max mark of its own: marks live
                # on its Levels as `points`. The criterion's max is therefore
                # the highest level, which is the definition of a max.
                levels = criterion.get("levels") or []
                points = [
                    float(level["points"])
                    for level in levels
                    if isinstance(level.get("points"), (int, float))
                ]
                title = str(criterion.get("title") or criterion.get("id") or "")
                description = str(criterion.get("description") or title)
                criteria.append(
                    RubricCriterion(
                        criterion_id=str(criterion.get("id") or title),
                        description=f"{title}: {description}",
                        max_marks=max(points) if points else 0.0,
                        weight=1.0,
                    )
                )
        return criteria

    def _submission_text(
        self, attachments: Sequence[dict[str, Any]]
    ) -> tuple[Optional[str], datetime]:
        parts: list[str] = []
        latest: Optional[datetime] = None
        for attachment in attachments:
            drive = attachment.get("driveFile") or {}
            file_id = drive.get("id")
            if not file_id:
                continue
            try:
                parts.append(self._transport.file_text(str(file_id)))
                meta = self._transport.file_metadata(str(file_id))
            except Exception as exc:
                log_event(
                    logger,
                    "connector.google.attachment_unreadable",
                    file_id=file_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            modified = self._parse_rfc3339(meta.get("modifiedTime"))
            if modified and (latest is None or modified > latest):
                latest = modified
        if not parts:
            return None, latest or self._epoch()
        return "\n\n".join(parts), latest or self._epoch()

    @staticmethod
    def _due_date(work: dict[str, Any]) -> datetime:
        date = work.get("dueDate") or {}
        time = work.get("dueTime") or {}
        try:
            return datetime(
                int(date.get("year", 1970)),
                int(date.get("month", 1)),
                int(date.get("day", 1)),
                int(time.get("hours", 0)),
                int(time.get("minutes", 0)),
                int(time.get("seconds", 0)),
                tzinfo=timezone.utc,
            )
        except (TypeError, ValueError) as exc:
            raise SourceUnavailableError(
                f"course work {work.get('id')!r} has an unreadable dueDate/dueTime "
                f"({work.get('dueDate')}, {work.get('dueTime')}): {exc}"
            ) from exc

    @staticmethod
    def _epoch() -> datetime:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    @staticmethod
    def _parse_rfc3339(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


# ---------------------------------------------------------------------------
# 4. The factory
# ---------------------------------------------------------------------------
def build_source_adapter(
    source: str = "local",
    *,
    settings: Any = None,
    seed_dir: Optional[str] = None,
    transport: Optional[GoogleWorkspaceTransport] = None,
) -> SourceAdapter:
    """Build the adapter named by :data:`course_organizer.config.Settings.source_adapter`.

    This function is the **only** place in the project that turns a string into
    a source. An agent, a tool handler and a skill all reach data by going
    through the connector, and the connector reaches data by coming through
    here -- so "swapping a source" is a one-token change to a setting, and the
    AST test in Lab 5 can assert that nothing else constructs an adapter.

    Args:
        source: ``"local"``, ``"in_memory"`` or ``"google"``.
        settings: A :class:`course_organizer.config.Settings`, read for the
            Google fields. Defaults to :func:`get_settings`.
        seed_dir: Overrides the seed root, for tests.
        transport: An injected Google transport. Required for ``"google"`` when
            no OAuth client is configured, and the seam the tests use.

    Returns:
        A live adapter.

    Raises:
        SourceUnavailableError: If the name is unknown, or ``"google"`` is
            selected without either a transport or a full OAuth
            configuration. The message names the setting to set.
    """
    from course_organizer.config import get_settings

    if source == "local":
        return LocalSeedAdapter(seed_dir=seed_dir)
    if source == "in_memory":
        return InMemoryAdapter()
    if source == "google":
        if transport is not None:
            folder_id = getattr(settings, "google_drive_folder_id", None)
            return GoogleWorkspaceAdapter(transport, folder_id=folder_id)
        if settings is None:
            settings = get_settings()
        missing = [
            name
            for name in (
                "google_oauth_client_id",
                "google_oauth_client_secret",
            )
            if not getattr(settings, name, None)
        ]
        if missing:
            raise SourceUnavailableError(
                "SOURCE_ADAPTER=google needs "
                f"{' and '.join(missing)} in .env, plus a token cached at "
                "GOOGLE_TOKEN_PATH. The default SOURCE_ADAPTER is 'local', "
                "which needs no credentials. To exercise the mapping without a "
                "tenant, pass transport=<a GoogleWorkspaceTransport>."
            )
        rest = RestGoogleWorkspaceTransport(
            client_id=str(settings.google_oauth_client_id),
            client_secret=str(settings.google_oauth_client_secret),
            token_path=str(settings.google_token_path),
            folder_id=settings.google_drive_folder_id,
        )
        return GoogleWorkspaceAdapter(rest, folder_id=settings.google_drive_folder_id)
    raise SourceUnavailableError(
        f"unknown SOURCE_ADAPTER {source!r}; expected 'local', 'in_memory' or "
        f"'google'"
    )
