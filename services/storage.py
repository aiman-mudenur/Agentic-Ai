from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import UploadFile, HTTPException


#: Repository root. ``services/`` is one level below it, so ``parents[1]`` --
#: ``parents[2]`` would put uploads in the parent of the checkout. Existing
#: rows keep their absolute ``file_path``, so moving this constant does not
#: strand files already uploaded.
REPO_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_ROOT = REPO_ROOT / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str) -> str:
    safe = Path(name).name
    return safe


def save_upload(file: UploadFile, subdir: str) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    target_dir = UPLOAD_ROOT / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix
    uid = uuid.uuid4().hex
    stored_name = f"{uid}{ext}"
    path = target_dir / stored_name
    data = file.file.read()
    size = len(data)
    with open(path, "wb") as f:
        f.write(data)
    return {
        "file_path": str(path),
        "file_name": _safe_name(file.filename),
        "stored_name": stored_name,
        "content_type": file.content_type or "application/octet-stream",
        "size_bytes": size,
    }


def save_bytes(data: bytes, filename: str, subdir: str, content_type: str | None = None) -> dict:
    """Persist raw bytes (e.g. a Drive download) like an upload. Same meta shape."""
    target_dir = UPLOAD_ROOT / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(filename or "file")
    ext = Path(safe).suffix
    uid = uuid.uuid4().hex
    stored_name = f"{uid}{ext}"
    path = target_dir / stored_name
    with open(path, "wb") as f:
        f.write(data)
    return {
        "file_path": str(path),
        "file_name": safe,
        "stored_name": stored_name,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": len(data),
    }
