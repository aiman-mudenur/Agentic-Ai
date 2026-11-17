from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from db.session import get_session
from models.auth import User, AuditLog
from models.assignment import Assignment
from models.course import Course, Note
from api.deps import require_role

router = APIRouter()


class ImportIn(BaseModel):
    classroom_course_id: str
    course_code: str
    import_coursework: bool = True
    import_materials: bool = True
    publish: bool = True


@router.get("/connect-url")
def classroom_connect_url(user: User = Depends(require_role("faculty"))):
    """Consent URL that also grants Classroom read scopes; token saved per user."""
    from services import google_oauth as go

    try:
        return go.auth_url("classroom", {"uid": user.id})
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or go.setup_instructions())


@router.get("/status")
def classroom_status(user: User = Depends(require_role("faculty"))):
    from services import google_oauth as go

    creds = go.load_user_credentials(user.id)
    out: dict = {"connected": creds is not None}
    if creds is None:
        out["setup"] = go.setup_instructions()
        return out
    granted = set(creds.scopes or [])
    out["granted_scopes"] = sorted(granted)
    out["missing_scopes"] = [s for s in go.CLASSROOM_SCOPES if s not in granted]
    return out


@router.post("/disconnect")
def classroom_disconnect(user: User = Depends(require_role("faculty"))):
    """Delete the saved Classroom token so the next connect grants fresh scopes."""
    from services import google_oauth as go

    p = go.token_path_for(user.id)
    try:
        if p.is_file():
            p.unlink()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:200])
    return {"connected": False}


@router.get("/courses")
def classroom_courses(user: User = Depends(require_role("faculty"))):
    from services import classroom as gc

    try:
        return gc.list_courses(user.id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Classroom error: {str(exc)[:400]}")


@router.get("/courses/{classroom_course_id}/work")
def classroom_work(classroom_course_id: str, user: User = Depends(require_role("faculty"))):
    from services import classroom as gc

    try:
        bundle = gc.get_work(user.id, classroom_course_id)
        return {
            "course": {k: (bundle["course"] or {}).get(k) for k in ("id", "name", "section", "description", "alternateLink")},
            "coursework": [
                {"id": w.get("id"), "title": w.get("title"), "description": (w.get("description") or "")[:300],
                 "maxPoints": w.get("maxPoints"), "dueDate": w.get("dueDate"), "alternateLink": w.get("alternateLink")}
                for w in bundle["coursework"]
            ],
            "materials": [
                {"id": m.get("id"), "title": gc.material_title(m), "link": gc.material_link(m),
                 "drive_files": gc.material_drive_files(m)}
                for m in bundle["materials"]
            ],
            "announcements": [
                {"id": a.get("id"), "text": (a.get("text") or "")[:300],
                 "link": a.get("alternateLink")}
                for a in bundle.get("announcements", [])
            ],
            "warnings": bundle.get("warnings", {}),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Classroom error: {str(exc)[:400]}")


def _normalize_course_code(code: str) -> str:
    """Tolerate pasted dropdown labels like 'CS_AI2027 (891792891951)'."""
    code = (code or "").strip()
    if code.endswith(")") and "(" in code:
        head, _, tail = code.rpartition("(")
        if tail[:-1].strip().isdigit() and head.strip():
            return head.strip()
    return code


@router.post("/import")
def classroom_import(payload: ImportIn, session: Session = Depends(get_session), user: User = Depends(require_role("faculty"))):
    """Import Classroom coursework -> assignments and materials -> notes.

    Provenance (classroom id + link) is recorded in the audit log; the portal
    stays the system of record and nothing auto-publishes grades.
    """
    from services import classroom as gc

    code = _normalize_course_code(payload.course_code)
    if not code:
        raise HTTPException(status_code=400, detail="course_code is required")
    try:
        bundle = gc.get_work(user.id, payload.classroom_course_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Classroom error: {str(exc)[:400]}")

    if not session.exec(select(Course).where(Course.course_code == code)).first():
        session.add(Course(course_code=code, title=(bundle["course"].get("name") or code)[:200],
                           description=(bundle["course"].get("description") or "")[:500] or None))
        session.commit()

    made_assignments: list[int] = []
    made_notes: list[int] = []
    if payload.import_coursework:
        for w in bundle["coursework"]:
            ad = Assignment(
                course_code=code,
                unit_number=None,
                title=(w.get("title") or "Untitled coursework")[:200],
                description=((w.get("description") or "")[:2000] or None),
                max_marks=float(w.get("maxPoints") or 20),
                due_date=gc.parse_due(w),
                rubric_text=None,
                file_path=None,
                file_name=None,
                created_by=user.id,
                is_published=bool(payload.publish),
            )
            session.add(ad)
            session.commit()
            session.refresh(ad)
            made_assignments.append(ad.id)
            session.add(AuditLog(actor_id=user.id, action="assignment_imported", entity_type="assignment",
                                 entity_id=ad.id, details=f"Imported from Classroom coursework {w.get('id')} ({w.get('alternateLink') or ''}) by {user.email}"))
        session.commit()
    n_files = 0
    file_failures: list[str] = []
    if payload.import_materials:
        from datetime import datetime, timezone

        from services.storage import save_bytes

        for m in bundle["materials"]:
            title = gc.material_title(m)[:200]
            link = gc.material_link(m)
            drive_files = gc.material_drive_files(m)
            if drive_files:
                # One note per attached Drive file, with real bytes.
                for df in drive_files:
                    try:
                        data, fname, mime = gc.download_drive_file(user.id, df["id"])
                    except Exception as exc:
                        file_failures.append(f"{df.get('title') or df['id']}: {str(exc)[:120]}")
                        continue
                    meta = save_bytes(data, fname, "notes", mime)
                    note = Note(
                        course_code=code, unit_number=None,
                        title=f"{title} — {meta['file_name']}"[:200],
                        file_path=meta["file_path"], file_name=meta["file_name"],
                        content_type=meta["content_type"], size_bytes=meta["size_bytes"],
                        source_url=link,
                        uploaded_by=user.id,
                        uploaded_at=datetime.now(timezone.utc), version=1, parent_note_id=None, is_current=True,
                    )
                    session.add(note)
                    session.commit()
                    session.refresh(note)
                    n_files += 1
                    made_notes.append(note.id)
                    session.add(AuditLog(actor_id=user.id, action="note_imported", entity_type="note",
                                         entity_id=note.id, details=f"Imported Classroom material {m.get('id')} file {df['id']} ({meta['size_bytes']} bytes) by {user.email}"))
                session.commit()
            else:
                note = Note(
                    course_code=code, unit_number=None, title=title, file_path=None, file_name=None,
                    content_type=None, size_bytes=0, source_url=link, uploaded_by=user.id,
                    uploaded_at=datetime.now(timezone.utc), version=1, parent_note_id=None, is_current=True,
                )
                session.add(note)
                session.commit()
                session.refresh(note)
                made_notes.append(note.id)
                session.add(AuditLog(actor_id=user.id, action="note_imported", entity_type="note",
                                     entity_id=note.id, details=f"Imported from Classroom material {m.get('id')} ({link or ''}) by {user.email}"))
        session.commit()
    n_ann = 0
    if payload.import_materials:
        from datetime import datetime, timezone

        for a in bundle.get("announcements", []):
            text = (a.get("text") or "").strip()
            title = ((text[:120] + "…") if len(text) > 120 else text) or "Announcement"
            note = Note(
                course_code=code, unit_number=None, title=title[:200], file_path=None, file_name=None,
                content_type=None, size_bytes=0, source_url=a.get("alternateLink"), uploaded_by=user.id,
                uploaded_at=datetime.now(timezone.utc), version=1, parent_note_id=None, is_current=True,
            )
            session.add(note)
            session.commit()
            session.refresh(note)
            n_ann += 1
            made_notes.append(note.id)
            session.add(AuditLog(actor_id=user.id, action="note_imported", entity_type="note",
                                 entity_id=note.id, details=f"Imported Classroom announcement {a.get('id')} by {user.email}"))
        session.commit()
    return {"course_code": code, "assignments_created": len(made_assignments),
            "assignment_ids": made_assignments, "notes_created": len(made_notes),
            "note_ids": made_notes, "announcements_imported": n_ann,
            "files_downloaded": n_files, "file_failures": file_failures}
