"""The local seed store: the ONLY code in the worklet that reads `data/seed`.

Document 2.10, Lab 2 requires two readers -- for "the notes/materials store"
and for "assignment & rubric definitions". Those readers are tools
(:mod:`course_organizer.tools.readers`), and tools obtain their data from here.

This module exists as a separate file for one reason: **containment**. If the
only place that touches the filesystem is here, then the acceptance criterion
"key numbers and lookups always go through tools, never hand-done in the
prompt" is enforceable. A test can assert that no agent module performs
filesystem access, because all of it is confined to this file.

Lab 5 replaces this class with a Google Drive / Classroom implementation behind
the same interface. Nothing above this line changes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from course_organizer.schemas import (
    AssignmentDefinition,
    NoteMaterial,
    SubmissionRecord,
    SubmissionStatus,
)

logger = logging.getLogger(__name__)


class SeedStoreError(RuntimeError):
    """Raised when the seed corpus is missing or malformed.

    The message names the exact path, because "no data" is otherwise the
    hardest seed problem in a fresh checkout to diagnose.
    """


class LocalSeedStore:
    """Reads the deterministic local corpus from `data/seed`.

    The layout is one folder per course slug, each holding `notes.json` and
    `assignments.json`. Notes and assignments are separate files because the
    document's connector exposes them as two separate reads.

    Args:
        seed_dir: Root of the corpus. Defaults to `data/seed` at the repo root.
    """

    def __init__(self, seed_dir: Path) -> None:
        self._seed_dir = Path(seed_dir)

    @property
    def seed_dir(self) -> Path:
        """The corpus root this store reads."""
        return self._seed_dir

    # -- course discovery --------------------------------------------------

    def course_codes(self) -> list[str]:
        """Every course code present in the corpus, sorted.

        The folder name is a slug, not a contract, so the code is read from
        each folder's own ``notes.json`` header. That keeps a course whose slug
        was renamed still resolvable by its real code.

        Returns:
            Sorted course codes.

        Raises:
            SeedStoreError: If no course folders are found at all, or if a
                folder's header carries no ``course_code``.
        """
        codes: list[str] = []
        for folder in self._course_folders():
            header = self._read_json(folder / "notes.json")
            code = header.get("course_code")
            if not code:
                raise SeedStoreError(
                    f"{folder / 'notes.json'} has no course_code in its header"
                )
            codes.append(str(code))

        if not codes:
            raise SeedStoreError(
                f"no courses found under {self._seed_dir}. "
                "Expected subfolders containing notes.json."
            )
        return sorted(codes)

    def _course_folders(self) -> list[Path]:
        """Every folder holding a `notes.json`, sorted by path."""
        if not self._seed_dir.is_dir():
            raise SeedStoreError(
                f"seed directory not found: {self._seed_dir}. "
                "It ships with the repository; run from the repo root."
            )
        folders = sorted(p.parent for p in self._seed_dir.glob("*/notes.json"))
        return folders

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Parse one JSON file, with a message that names the file.

        Raises:
            SeedStoreError: If the file is missing or is not valid JSON.
        """
        if not path.is_file():
            raise SeedStoreError(f"seed file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SeedStoreError(f"seed file is not valid JSON: {path} ({exc})") from exc

    def _folder_for(self, course_code: str) -> Path:
        """Find the folder backing a course code.

        Raises:
            SeedStoreError: If the code is unknown, listing what exists.
        """
        for folder in self._course_folders():
            if self._read_json(folder / "notes.json").get("course_code") == course_code:
                return folder
        available = ", ".join(self.course_codes()) or "(none)"
        raise SeedStoreError(
            f"unknown course_code {course_code!r}. Available: {available}"
        )

    def notes_path(self, course_code: str) -> Path:
        """Absolute path of the notes file backing a course.

        Returns:
            The `notes.json` path.

        Raises:
            SeedStoreError: If the course is unknown.
        """
        return self._folder_for(course_code) / "notes.json"

    def assignments_path(self, course_code: str) -> Optional[Path]:
        """Absolute path of the assignments file backing a course, if present.

        Returns:
            The `assignments.json` path, or None when the course has released
            no assignments yet. Callers treat None as an empty list, which is
            a legitimate course state rather than an error.
        """
        path = self._folder_for(course_code) / "assignments.json"
        return path if path.is_file() else None

    def submissions_path(self, course_code: str) -> Optional[Path]:
        """Absolute path of the submission-history file for a course, if present.

        Lab 4 needs "the history of prior assignment submissions" (doc 2.10,
        Lab 4 inputs). A course that has run no assignment cycle yet simply has
        no file, which is the same legitimate-empty case as `assignments_path`.

        Returns:
            The `submissions.json` path, or None when the course has no
            submission history.
        """
        path = self._folder_for(course_code) / "submissions.json"
        return path if path.is_file() else None

    def relative(self, path: Path) -> str:
        """Render a seed path relative to the repo, for audit records.

        Falls back to the absolute path when the file sits outside the repo,
        so the record is never misleading.
        """
        from course_organizer.config import PROJECT_ROOT

        try:
            return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            return str(path)

    # -- the two document reads -------------------------------------------

    def read_notes(self, course_code: str) -> tuple[dict[str, Any], list[NoteMaterial]]:
        """Read "the notes/materials store" for one course.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            ``(course_header, notes)`` where the header carries the course
            metadata and the declared unit list, and ``notes`` are validated
            `NoteMaterial` objects.

        Raises:
            SeedStoreError: If the course is unknown or a note is malformed.
        """
        folder = self._folder_for(course_code)
        header = self._read_json(folder / "notes.json")
        raw_notes = header.get("notes") or []
        notes: list[NoteMaterial] = []
        for index, raw in enumerate(raw_notes):
            try:
                notes.append(NoteMaterial.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - re-raised with location
                raise SeedStoreError(
                    f"note #{index} in {folder / 'notes.json'} is invalid: {exc}"
                ) from exc
        return header, notes

    def read_assignments(self, course_code: str) -> list[AssignmentDefinition]:
        """Read "assignment & rubric definitions" for one course.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            Validated `AssignmentDefinition` objects, sorted by assignment id.

        Raises:
            SeedStoreError: If the course is unknown or a definition is malformed.
        """
        folder = self._folder_for(course_code)
        path = folder / "assignments.json"
        if not path.is_file():
            # An empty assignment list is legitimate: a course may have released
            # nothing yet, and the validator should report that as a finding
            # rather than the store raising.
            logger.info("no assignments.json for %s; treating as empty", course_code)
            return []
        payload = self._read_json(path)
        raw_assignments = payload.get("assignments") or []
        definitions: list[AssignmentDefinition] = []
        for index, raw in enumerate(raw_assignments):
            try:
                definitions.append(AssignmentDefinition.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - re-raised with location
                raise SeedStoreError(
                    f"assignment #{index} in {path} is invalid: {exc}"
                ) from exc
        return sorted(definitions, key=lambda a: a.assignment_id)

    # -- the third document read ------------------------------------------

    def read_submissions(self, course_code: str) -> list[SubmissionRecord]:
        """Read "submission records" -- the history of prior submissions.

        Document 2.7 names exactly three connector reads; Lab 2 implemented the
        first two. This is the third, added by Lab 4 because Lab 4's inputs are
        "the notes/materials store; history of prior assignment submissions;
        current draft".

        **Two fields are derived here, never read from the file.** The seed
        corpus stores only facts of collection (who, what, when). `status` and
        `late_by_seconds` are computed from ``submitted_at`` against the
        assignment's ``due_date``, for two reasons:

        1. Doc 2.12 requires "never alter a submission timestamp". If lateness
           were stored as its own number, the two could disagree and nobody
           could tell which was tampered with. Deriving it means the timestamp
           is the single source of truth and lateness is a *view* of it.
        2. Doc 2.7 check 1 requires "late submissions are flagged accurately".
           A hand-typed `late_by_seconds` is exactly the kind of number Lab 2's
           pitfall warns about: asserted instead of computed.

        Args:
            course_code: e.g. ``CS201``.

        Returns:
            Validated `SubmissionRecord` objects sorted by submission id. An
            empty list when the course has no submission history.

        Raises:
            SeedStoreError: If the course is unknown, a record is malformed, or
                a record names an assignment the course never defined. The last
                case is an error rather than a silent skip: a submission against
                an undefined assignment means the corpus disagrees with itself,
                and every later number built on it would be suspect.
        """
        folder = self._folder_for(course_code)
        path = folder / "submissions.json"
        if not path.is_file():
            logger.info("no submissions.json for %s; treating as empty", course_code)
            return []

        due_dates = {a.assignment_id: a.due_date for a in self.read_assignments(course_code)}
        payload = self._read_json(path)
        raw_records = payload.get("submissions") or []

        records: list[SubmissionRecord] = []
        for index, raw in enumerate(raw_records):
            raw = dict(raw)
            assignment_id = str(raw.get("assignment_id", ""))
            if assignment_id not in due_dates:
                raise SeedStoreError(
                    f"submission #{index} in {path} names assignment "
                    f"{assignment_id!r}, which this course does not define "
                    f"(known: {sorted(due_dates) or 'none'})"
                )

            # The JSON carries an ISO-8601 string with an explicit UTC offset.
            # It is parsed here rather than left to the schema so that a
            # *missing offset* is reported with the file and index, instead of
            # surfacing as a bare pydantic error.
            submitted_at = raw.get("submitted_at")
            if isinstance(submitted_at, str):
                try:
                    raw["submitted_at"] = datetime.fromisoformat(submitted_at)
                except ValueError as exc:
                    raise SeedStoreError(
                        f"submission #{index} in {path} has a submitted_at that "
                        f"is not ISO-8601: {submitted_at!r} ({exc})"
                    ) from exc
                submitted_at = raw["submitted_at"]
            if not isinstance(submitted_at, datetime):
                raise SeedStoreError(
                    f"submission #{index} in {path} has a submitted_at that is "
                    f"not a datetime: {submitted_at!r}. Doc 2.12 forbids altering "
                    "a submission timestamp, so it must be collected, not derived."
                )

            # Derived, not read. See the docstring.
            due = due_dates[assignment_id]
            late_by = max(0, int((submitted_at - due).total_seconds()))
            raw["status"] = (
                SubmissionStatus.LATE if late_by > 0 else SubmissionStatus.SUBMITTED
            )
            raw["late_by_seconds"] = late_by

            # A score in the file is preserved only if the seed declares one.
            # Absent means "not yet checked", which is not the same as zero.
            raw.setdefault("similarity_score", None)
            raw.setdefault("similarity_against", [])

            try:
                records.append(SubmissionRecord.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - re-raised with location
                raise SeedStoreError(
                    f"submission #{index} in {path} is invalid: {exc}"
                ) from exc
        return sorted(records, key=lambda s: s.submission_id)

    def archive_sources(self, course_code: str) -> list[str]:
        """Repo-relative paths that back a course's long-term archive.

        Doc 2.10 Lab 4 separates "run state (the current draft)" from "stored
        reference - the notes/materials store and the history of past
        assignment submissions". The retrieval layer must be able to say *where*
        each archived record came from, and the memory boundary later uses
        these strings as its allow-list: only a record whose provenance is one
        of these may enter the archive.

        Returns:
            The source paths that exist for this course, sorted. A course with
            only notes still returns its notes file.
        """
        paths: list[str] = []
        for path in (
            self.notes_path(course_code),
            self.assignments_path(course_code),
            self.submissions_path(course_code),
        ):
            if path is not None:
                paths.append(self.relative(path))
        return sorted(paths)

    # -- provenance, for the audit trail ----------------------------------

    def describe_sources(self) -> list[dict[str, str]]:
        """List every seed file with its byte size, for the evidence section.

        Lets a reviewer confirm the corpus that produced a run, which matters
        once the store is swap-able.

        Paths are rendered through :meth:`relative`, so they are repo-relative
        and slash-normalised. An audit record that changes shape depending on
        the operating system cannot be diffed between machines, which defeats
        the purpose of recording it.
        """
        rows: list[dict[str, str]] = []
        for folder in self._course_folders():
            for path in sorted(folder.glob("*.json")):
                rows.append(
                    {
                        "path": self.relative(path),
                        "bytes": str(path.stat().st_size),
                    }
                )
        return sorted(rows, key=lambda row: row["path"])


@lru_cache(maxsize=4)
def get_seed_store(seed_dir: Optional[str] = None) -> LocalSeedStore:
    """Return a cached store for a corpus directory.

    Args:
        seed_dir: Corpus root. Defaults to the project's `data/seed`.

    Returns:
        A `LocalSeedStore`. Cached so repeated tool calls in one run do not
        re-read and re-validate the corpus.
    """
    if seed_dir:
        return LocalSeedStore(Path(seed_dir))
    from course_organizer.config import PROJECT_ROOT

    return LocalSeedStore(PROJECT_ROOT / "data" / "seed")


__all__ = ["LocalSeedStore", "SeedStoreError", "get_seed_store"]