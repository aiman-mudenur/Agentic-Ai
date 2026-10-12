from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse


def file_response_for(file_path: str | None, file_name: str | None, content_type: str | None) -> FileResponse:
    """Return a FileResponse for a stored upload path.

    Returns 404 when no file is attached or the file is missing on disk.
    """
    if not file_path:
        raise HTTPException(status_code=404, detail="No file attached")
    p = Path(file_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found on server")
    return FileResponse(
        path=str(p),
        filename=file_name or p.name,
        media_type=content_type or "application/octet-stream",
    )
