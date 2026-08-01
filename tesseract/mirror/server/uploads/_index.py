from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from tesseract.mirror.server.uploads._storage import (
    StoredAttachment,
    _is_within_upload_root,
    upload_root,
)

_INDEX_DIR = "_index"

_session_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    return _session_locks.setdefault(session_id, asyncio.Lock())


def _index_path(session_id: str) -> Path:
    return upload_root() / _INDEX_DIR / f"{session_id}.json"


def _read_index(session_id: str) -> dict[str, str]:
    path = _index_path(session_id)
    if not _is_within_upload_root(path) or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _write_index_atomic(session_id: str, data: dict[str, str]) -> None:
    path = _index_path(session_id)
    if not _is_within_upload_root(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


async def _index_attachment(att: StoredAttachment) -> None:
    if not att.storage_path:
        return
    async with _lock_for(att.session_id):
        data = _read_index(att.session_id)
        data[att.id] = att.storage_path
        _write_index_atomic(att.session_id, data)


async def _index_metadata_path(session_id: str, attachment_id: str, meta_path: Path) -> None:
    try:
        storage_path = meta_path.parent.relative_to(upload_root()).as_posix()
    except ValueError:
        return
    async with _lock_for(session_id):
        data = _read_index(session_id)
        data[attachment_id] = storage_path
        _write_index_atomic(session_id, data)


async def _unindex_attachment(session_id: str, attachment_id: str) -> None:
    async with _lock_for(session_id):
        data = _read_index(session_id)
        if attachment_id not in data:
            return
        data.pop(attachment_id, None)
        _write_index_atomic(session_id, data)


def _indexed_metadata_path(session_id: str, attachment_id: str) -> Path | None:
    storage_path = _read_index(session_id).get(attachment_id)
    if not isinstance(storage_path, str) or not storage_path:
        return None
    candidate = upload_root() / Path(storage_path) / "metadata.json"
    if _is_within_upload_root(candidate) and candidate.exists():
        return candidate
    return None


def _metadata_path(session_id: str, attachment_id: str) -> Path | None:
    legacy = upload_root() / session_id / attachment_id / "metadata.json"
    if _is_within_upload_root(legacy) and legacy.exists():
        return legacy
    indexed = _indexed_metadata_path(session_id, attachment_id)
    if indexed is not None:
        return indexed
    # One-time compatibility path for attachments created before the index existed.
    patterns = (
        f"*/{session_id}/*/{attachment_id}/metadata.json",
        f"*/*/{session_id}/{attachment_id}/metadata.json",
    )
    for pattern in patterns:
        for candidate in upload_root().glob(pattern):
            if _is_within_upload_root(candidate) and candidate.exists():
                return candidate
    return None
