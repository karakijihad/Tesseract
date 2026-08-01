from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME


def upload_root() -> Path:
    """Resolve at call time so a relocated `TESSERACT_HOME` is honoured.

    Was a module-level constant frozen at import, so a packaged install (or a
    test fixture pointing at tmp_path) kept writing to whatever home happened
    to be resolved when this module first loaded. Mirrors
    `downloads/_storage.py::_download_root`.
    """
    home_env = os.environ.get("TESSERACT_HOME")
    base = Path(home_env) if home_env else TESSERACT_HOME
    return base / "uploads" / "chat"


@dataclass(frozen=True)
class StoredAttachment:
    id: str
    session_id: str
    filename: str
    mime_type: str
    size: int
    kind: str
    url: str
    created_at: str
    storage_path: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "kind": self.kind,
            "url": self.url,
            "created_at": self.created_at,
        }

    def to_metadata_json(self) -> dict[str, Any]:
        data = self.to_json()
        if self.storage_path:
            data["storage_path"] = self.storage_path
        return data


def _storage_path(
    kind: str,
    created_at: datetime,
    session_id: str,
    attachment_id: str,
) -> Path:
    from tesseract.mirror.server.uploads._validation import _KIND_DIR
    bucket = _KIND_DIR.get(kind, "document")
    return Path(bucket) / session_id / created_at.date().isoformat() / attachment_id


def _attachment_url(session_id: str, attachment_id: str, filename: str) -> str:
    return f"/api/uploads/chat/{session_id}/{attachment_id}/{filename}"


def _is_within_upload_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(upload_root().resolve())
        return True
    except ValueError:
        return False


def _attachment_dir(att: StoredAttachment | None) -> Path | None:
    if att is None:
        return None
    if att.storage_path:
        path = upload_root() / Path(att.storage_path)
        if _is_within_upload_root(path):
            return path
    legacy_path = upload_root() / att.session_id / att.id
    if _is_within_upload_root(legacy_path):
        return legacy_path
    return None


def _attachment_file_path(att: StoredAttachment) -> Path | None:
    dest_dir = _attachment_dir(att)
    if dest_dir is None:
        return None
    path = dest_dir / att.filename
    if not _is_within_upload_root(path) or not path.exists():
        return None
    return path


async def attachment_part_for_model(att: StoredAttachment) -> dict[str, Any] | None:
    file_path = _attachment_file_path(att)
    if file_path is None:
        return None
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, file_path.read_bytes)
    data = base64.b64encode(raw).decode("ascii")
    if att.kind == "image":
        return {
            "type": "image",
            "attachment_id": att.id,
            "filename": att.filename,
            "mime_type": att.mime_type,
            "data": data,
        }
    if att.kind == "pdf":
        return {
            "type": "file",
            "attachment_id": att.id,
            "filename": att.filename,
            "mime_type": att.mime_type,
            "data": data,
        }
    if att.kind == "audio":
        # Emitted as `type=audio` so the WS preprocessor can intercept and
        # replace with a text transcript before chat_brain sees the message.
        # Chat models can't ingest raw audio over chat-completions; local
        # Whisper handles transcription server-side.
        return {
            "type": "audio",
            "attachment_id": att.id,
            "filename": att.filename,
            "mime_type": att.mime_type,
            "data": data,
        }
    return None


async def _remove_tree_async(path: Path) -> None:
    if not path.exists() or not _is_within_upload_root(path):
        return

    def _sync_remove(p: Path) -> None:
        for child in sorted(p.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        p.rmdir()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_remove, path)
