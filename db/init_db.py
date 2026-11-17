from db.engine import engine
from db.base import SQLModel
from sqlalchemy import text
from sqlmodel import select
import re


def init_db():
    """Create tables, migrate old ones, backfill. Demo seeding stays explicit
    via `python -m scripts.seed_demo` — never automatic."""
    SQLModel.metadata.create_all(engine)
    _ensure_columns(engine)
    _backfill_note_defaults(engine)


def _ensure_columns(bind_engine):
    """ADD COLUMN for any model field missing from an older database file.

    SQLite `create_all` only creates missing *tables*; it never alters
    existing ones, so upgrades used to 500 until a full reset. This compares
    the live models against PRAGMA table_info and adds what's missing.
    Extra legacy columns are left alone (harmless).
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(bind_engine)
    existing = set(insp.get_table_names())
    with bind_engine.begin() as conn:
        for table in SQLModel.metadata.tables.values():
            if table.name not in existing:
                continue
            cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in cols:
                    ddl_type = col.type.compile(dialect=bind_engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))


def _backfill_note_defaults(bind_engine):
    """Old note rows get NULL for the newer non-null-by-convention fields."""
    with bind_engine.begin() as conn:
        conn.execute(text("UPDATE note SET version = 1 WHERE version IS NULL"))
        conn.execute(text("UPDATE note SET is_current = 1 WHERE is_current IS NULL"))
    _backfill_note_links(bind_engine)


_URL_RE = re.compile(r"https?://[^\s)]+")


def _backfill_note_links(bind_engine):
    """Notes imported before links were stored regain their Classroom URL from
    the audit log, which has recorded provenance (entity + URL) since imports
    began. Link-less notes become openable without re-importing."""
    from sqlmodel import Session as _Session

    from models.auth import AuditLog
    from models.course import Note

    from sqlalchemy import inspect as _inspect

    if "auditlog" not in set(_inspect(bind_engine).get_table_names()):
        return
    with _Session(bind_engine) as session:
        rows = session.exec(select(Note).where(Note.source_url.is_(None))).all()
        for note in rows:
            log = session.exec(
                select(AuditLog)
                .where(AuditLog.entity_type == "note", AuditLog.entity_id == note.id,
                       AuditLog.action.in_(["note_imported", "note_uploaded", "note_updated"]))
                .order_by(AuditLog.id.desc())
            ).first()
            if not log or not log.details:
                continue
            m = _URL_RE.search(log.details)
            if m:
                note.source_url = m.group(0)
                session.add(note)
        session.commit()
