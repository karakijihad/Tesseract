from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME

# Mirror of `uploads._validation._KIND_DIR`. Kept here so downloads has
# no import-time dependency on uploads. `video` is added on the downloads
# side because tools may produce video before uploads accepts it.
_KIND_DIR = {
    "image": "image",
    "pdf": "pdf",
    "file": "document",
    "audio": "audio",
    "video": "video",
}

# Per-session locks for the index file's read-modify-write. Tools declared
# `is_concurrency_safe = True` (image_generate is) can fire in parallel
# inside one session, and the per-session JSON list would race without
# this serialization. Locks are keyed by session id and live for the
# lifetime of the process — small, harmless leak relative to other
# in-memory state we keep per session.
_INDEX_LOCKS: dict[str, asyncio.Lock] = {}


def _index_lock(session_id: str) -> asyncio.Lock:
    lock = _INDEX_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _INDEX_LOCKS[session_id] = lock
    return lock

_KIND_BY_MIME_PREFIX = {
    "image/": "image",
    "audio/": "audio",
    "video/": "video",
}


def _download_root() -> Path:
    """Resolve at call time so tests overriding `TESSERACT_HOME` via
    `monkeypatch.setenv` land their writes under tmp_path. CLAUDE.md
    hard rule: no test pollution under production state paths."""
    home_env = os.environ.get("TESSERACT_HOME")
    base = Path(home_env) if home_env else TESSERACT_HOME
    return base / "downloads" / "chat"


@dataclass(frozen=True)
class StoredDownload:
    id: str
    session_id: str
    filename: str
    mime_type: str
    size: int
    kind: str
    url: str
    created_at: str
    storage_path: str
    source_tool: str = ""

    def to_metadata_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "kind": self.kind,
            "url": self.url,
            "created_at": self.created_at,
            "storage_path": self.storage_path,
            "source_tool": self.source_tool,
        }


def _kind_from_mime(mime_type: str) -> str:
    for prefix, kind in _KIND_BY_MIME_PREFIX.items():
        if mime_type.startswith(prefix):
            return kind
    if mime_type == "application/pdf":
        return "pdf"
    return "file"


def _is_within_download_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(_download_root().resolve())
        return True
    except (ValueError, OSError):
        return False


def _download_dir_for(
    session_id: str,
    kind: str,
    created_at: datetime,
    artifact_id: str,
) -> Path:
    bucket = _KIND_DIR.get(kind, "document")
    return (
        _download_root()
        / bucket
        / session_id
        / created_at.date().isoformat()
        / artifact_id
    )


def _download_url(session_id: str, artifact_id: str, filename: str) -> str:
    return f"/api/downloads/chat/{session_id}/{artifact_id}/{filename}"


async def save_download(
    *,
    session_id: str,
    filename: str,
    data: bytes,
    mime_type: str = "",
    source_tool: str = "",
) -> StoredDownload:
    """Persist generated artifact bytes mirroring the uploads layout:
    ``downloads/chat/<kind>/<session_id>/<YYYY-MM-DD>/<artifact_id>/<filename>``
    plus a per-session index at ``downloads/chat/_index/<session_id>.json``.
    Returns the StoredDownload — `.url` is the public HTTP path for the
    chat surface to render."""
    if not session_id:
        raise ValueError("session_id is required")
    if not filename:
        raise ValueError("filename is required")
    if not mime_type:
        guessed, _ = mimetypes.guess_type(filename)
        mime_type = guessed or "application/octet-stream"

    artifact_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    kind = _kind_from_mime(mime_type)

    dest_dir = _download_dir_for(session_id, kind, created_at, artifact_id)
    if not _is_within_download_root(dest_dir.parent):
        raise ValueError("computed dest dir escapes downloads root")

    def _sync_write() -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_bytes(data)

    await asyncio.get_event_loop().run_in_executor(None, _sync_write)

    storage_path_relative = str(
        dest_dir.relative_to(_download_root())
    ).replace("\\", "/")

    rec = StoredDownload(
        id=artifact_id,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        size=len(data),
        kind=kind,
        url=_download_url(session_id, artifact_id, filename),
        created_at=created_at.isoformat(),
        storage_path=storage_path_relative,
        source_tool=source_tool,
    )

    def _sync_index() -> None:
        index_dir = _download_root() / "_index"
        index_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / f"{session_id}.json"
        if index_path.exists():
            try:
                entries = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                entries = []
            if not isinstance(entries, list):
                entries = []
        else:
            entries = []
        entries.append(rec.to_metadata_json())
        index_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    async with _index_lock(session_id):
        await asyncio.get_event_loop().run_in_executor(None, _sync_index)
    return rec


def load_download(session_id: str, artifact_id: str) -> StoredDownload | None:
    """Look up a download record by session+id from the per-session index."""
    index_path = _download_root() / "_index" / f"{session_id}.json"
    if not index_path.exists():
        return None
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(entries, list):
        return None
    for raw in entries:
        if isinstance(raw, dict) and raw.get("id") == artifact_id:
            return StoredDownload(
                id=str(raw.get("id", "")),
                session_id=str(raw.get("session_id", "")),
                filename=str(raw.get("filename", "")),
                mime_type=str(raw.get("mime_type", "")),
                size=int(raw.get("size") or 0),
                kind=str(raw.get("kind", "")),
                url=str(raw.get("url", "")),
                created_at=str(raw.get("created_at", "")),
                storage_path=str(raw.get("storage_path", "")),
                source_tool=str(raw.get("source_tool", "")),
            )
    return None


def download_file_path(rec: StoredDownload) -> Path | None:
    if not rec.storage_path:
        return None
    full = _download_root() / Path(rec.storage_path) / rec.filename
    if not _is_within_download_root(full) or not full.exists():
        return None
    return full
