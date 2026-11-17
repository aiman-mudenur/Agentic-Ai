"""Google Classroom reads + import, built on the Lab 5 connector transport.

All reads go through :class:`RestGoogleWorkspaceTransport` with an injected,
per-user Classroom service (the Lab 5 seam), so no agent/module touches Google
directly. Import writes local Assignment/Note rows with provenance in the audit
log — Classroom stays the source, the portal stays the system of record.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any


def transport_for_user(user_id: int):
    """Build the Lab 5 transport for one faculty member. Raises RuntimeError."""
    from course_organizer.connector.adapters import RestGoogleWorkspaceTransport
    from googleapiclient.discovery import build

    from services.google_oauth import load_user_credentials

    creds = load_user_credentials(user_id)
    if creds is None:
        raise RuntimeError("Google Classroom is not connected for this account. Connect first.")
    service = build("classroom", "v1", credentials=creds, cache_discovery=False)
    return RestGoogleWorkspaceTransport(
        client_id="", client_secret="", token_path="", classroom_service=service
    )


def list_courses(user_id: int) -> list[dict[str, Any]]:
    return transport_for_user(user_id).courses()


def get_work(user_id: int, classroom_course_id: str) -> dict[str, Any]:
    """Fetch course + coursework + materials + topics independently.

    One refused read no longer hides the others; per-part failures are
    returned in ``warnings`` so the UI can name exactly what Google denied.
    """
    t = transport_for_user(user_id)
    bundle: dict[str, Any] = {"course": {}, "coursework": [], "materials": [],
                              "announcements": [], "topics": {}, "warnings": {}}
    parts = (
        ("course", lambda: t.course(classroom_course_id), {}),
        ("coursework", lambda: t.course_work(classroom_course_id), []),
        ("materials", lambda: t.course_materials(classroom_course_id), []),
        ("announcements", lambda: t.announcements(classroom_course_id), []),
        ("topics", lambda: t.topics(classroom_course_id), {}),
    )
    for name, fn, fallback in parts:
        try:
            bundle[name] = fn()
        except Exception as exc:
            bundle[name] = fallback
            bundle["warnings"][name] = str(exc)[:400]
    if not bundle["course"] and bundle["warnings"].get("course"):
        raise RuntimeError(bundle["warnings"]["course"])
    return bundle


def parse_due(work: dict[str, Any]):
    """Classroom {dueDate:{year,month,day}} -> date | None (honest: no timestamp exists)."""
    try:
        d = work.get("dueDate") or {}
        if d.get("year") and d.get("month") and d.get("day"):
            return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except Exception:
        pass
    return None


def material_title(m: dict[str, Any]) -> str:
    return m.get("title") or "Untitled material"


def material_link(m: dict[str, Any]) -> str | None:
    mats = m.get("materials") or []
    for item in mats:
        for key in ("link", "driveFile", "youtubeVideo", "form"):
            if isinstance(item.get(key), dict) and item[key].get("url"):
                return item[key]["url"]
            if isinstance(item.get(key), dict) and item[key].get("alternateLink"):
                return item[key]["alternateLink"]
    return m.get("alternateLink")


def material_drive_files(m: dict[str, Any]) -> list[dict[str, str]]:
    """Drive attachments of a material: [{id, title}]. Empty for link-only posts."""
    out = []
    for item in m.get("materials") or []:
        df = (item.get("driveFile") or {}).get("driveFile") or {}
        if df.get("id"):
            out.append({"id": df["id"], "title": df.get("title") or "file"})
    return out


_EXPORT_MIME = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "application/pdf",
}


def drive_service_for(user_id: int):
    from googleapiclient.discovery import build

    from services.google_oauth import load_user_credentials

    creds = load_user_credentials(user_id)
    if creds is None:
        raise RuntimeError("Google Classroom is not connected for this account. Connect first.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download_drive_file(user_id: int, file_id: str) -> tuple[bytes, str, str]:
    """Download one Drive file's bytes. Returns (data, filename, mime).

    Native Google Docs/Sheets/Slides are exported to PDF since they have no
    binary content of their own.
    """
    svc = drive_service_for(user_id)
    meta = svc.files().get(fileId=file_id, fields="id,name,mimeType,size",
                           supportsAllDrives=True).execute()
    name = meta.get("name") or file_id
    mime = meta.get("mimeType") or "application/octet-stream"
    if mime in _EXPORT_MIME:
        data = svc.files().export_media(fileId=file_id, mimeType=_EXPORT_MIME[mime]).execute()
        if not Path(name).suffix:
            name = name + ".pdf"
        return bytes(data), name, _EXPORT_MIME[mime]
    data = svc.files().get_media(fileId=file_id).execute()
    return bytes(data), name, mime
