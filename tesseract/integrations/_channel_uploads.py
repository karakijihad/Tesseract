"""Persist raw inbound channel media to disk, mirroring Mirror's uploads layout.

Inbound Telegram (and future WhatsApp / Signal) photos, voice notes,
documents, etc. were fetched, extracted (transcribed / described /
text-decoded), then discarded — only the extracted text reached the
conversation store. Operators couldn't go back to the original photo
The assistant saw, and the assistant itself couldn't re-open a file the next day.

This module saves the bytes alongside the existing extracted text. The
tree is channel-keyed, not session-keyed: Telegram sessions are
synthetic and rotate, but chats are the durable identity an operator
recognizes. Layout::

    uploads/channels/<channel>/<chat_id>/<YYYY-MM-DD>/<message_id>/<filename>
    uploads/channels/_index/<channel>/<chat_id>.json

The JSON index is newest-first per CLAUDE.md so the most recent media
is one read away. ``save_channel_attachment`` is concurrency-safe via
per-(channel, chat_id) ``asyncio.Lock``.

Mirrors the patterns in ``tesseract/mirror/server/downloads/_storage.py``
and ``tesseract/mirror/server/uploads/_storage.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)

_KIND_DIR = {
    "voice": "voice",
    "audio": "audio",
    "photo": "image",
    "video": "video",
    "video_note": "video",
    "animation": "video",
    "document": "document",
    "sticker": "sticker",
}

_KIND_DEFAULT_EXT = {
    "voice": ".ogg",
    "audio": ".mp3",
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "animation": ".mp4",
    "sticker": ".webp",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SEGMENT_MAX = 128

_INDEX_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _index_lock(channel: str, chat_id: str) -> asyncio.Lock:
    key = (channel, chat_id)
    lock = _INDEX_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _INDEX_LOCKS[key] = lock
    return lock


def _uploads_root() -> Path:
    """Resolve at call time so tests overriding ``TESSERACT_HOME`` via
    ``monkeypatch.setenv`` land their writes under ``tmp_path``. CLAUDE.md
    hard rule: zero test pollution under production state paths."""
    home_env = os.environ.get("TESSERACT_HOME")
    base = Path(home_env) if home_env else TESSERACT_HOME
    return base / "uploads" / "channels"


@dataclass(frozen=True)
class StoredChannelAttachment:
    channel: str
    chat_id: str
    message_id: str
    kind: str
    filename: str
    mime_type: str
    size: int
    storage_path: str  # relative to uploads/channels root, forward slashes
    created_at: str
    source_ref: str = ""  # adapter-native handle (Telegram file_id)
    caption: str = ""

    def to_metadata_json(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "storage_path": self.storage_path,
            "created_at": self.created_at,
            "source_ref": self.source_ref,
            "caption": self.caption,
        }


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("_") or "x"
    return cleaned[:_SAFE_SEGMENT_MAX]


def _safe_filename(name: str | None, *, kind: str, mime_type: str) -> str:
    if name:
        stem = Path(name).name.strip().replace(" ", "_")
        cleaned = _SAFE_NAME_RE.sub("_", stem).strip("._")
        if cleaned:
            return cleaned[:_SAFE_SEGMENT_MAX]
    # Synthesize from mime or kind default. Telegram voice notes arrive as
    # opus-in-ogg with no filename; photos arrive as JPEG with no name.
    ext = ""
    if mime_type:
        ext = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip()) or ""
    if not ext:
        ext = _KIND_DEFAULT_EXT.get(kind, ".bin")
    return f"{kind}_{uuid.uuid4().hex[:8]}{ext}"


def _is_within_uploads_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(_uploads_root().resolve())
        return True
    except (ValueError, OSError):
        return False


def _attachment_dir(
    channel: str,
    chat_id: str,
    created_at: datetime,
    message_id: str,
    kind: str,
) -> Path:
    bucket = _KIND_DIR.get(kind, "other")
    return (
        _uploads_root()
        / _safe_segment(channel)
        / _safe_segment(chat_id)
        / created_at.date().isoformat()
        / _safe_segment(message_id)
        / bucket
    )


async def save_channel_attachment(
    *,
    channel: str,
    chat_id: str,
    message_id: str,
    kind: str,
    data: bytes,
    filename: str | None,
    mime_type: str,
    source_ref: str = "",
    caption: str = "",
) -> StoredChannelAttachment:
    """Persist ``data`` and append to the per-chat index.

    Returns a :class:`StoredChannelAttachment` whose ``storage_path`` is
    the path relative to ``uploads/channels`` (forward slashes,
    Mirror-style). Bytes are written via ``run_in_executor`` so the
    long-poll loop never blocks on disk. Index append is serialized per
    ``(channel, chat_id)`` via :func:`_index_lock`.
    """
    if not channel:
        raise ValueError("channel is required")
    if not chat_id:
        raise ValueError("chat_id is required")
    if not kind:
        raise ValueError("kind is required")

    # Filename is derived from the *original* mime (which may be empty)
    # so we fall through to the per-kind default extension instead of
    # always picking up ``.bin`` from ``application/octet-stream``.
    raw_mime = (mime_type or "").split(";", 1)[0].strip()
    safe_name = _safe_filename(filename, kind=kind, mime_type=raw_mime)
    mime_type = raw_mime or "application/octet-stream"
    created_at = datetime.now(timezone.utc)

    dest_dir = _attachment_dir(channel, chat_id, created_at, str(message_id), kind)
    if not _is_within_uploads_root(dest_dir):
        raise ValueError("computed dest dir escapes uploads/channels root")

    def _sync_write() -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / safe_name).write_bytes(data)

    await asyncio.get_event_loop().run_in_executor(None, _sync_write)

    storage_path = str(
        (dest_dir / safe_name).relative_to(_uploads_root())
    ).replace("\\", "/")

    rec = StoredChannelAttachment(
        channel=channel,
        chat_id=str(chat_id),
        message_id=str(message_id),
        kind=kind,
        filename=safe_name,
        mime_type=mime_type,
        size=len(data),
        storage_path=storage_path,
        created_at=created_at.isoformat(),
        source_ref=source_ref or "",
        caption=caption or "",
    )

    def _sync_index() -> None:
        index_dir = _uploads_root() / "_index" / _safe_segment(channel)
        index_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / f"{_safe_segment(str(chat_id))}.json"
        if index_path.exists():
            try:
                entries = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                entries = []
            if not isinstance(entries, list):
                entries = []
        else:
            entries = []
        # Newest-first per CLAUDE.md so the most recent media is index[0].
        entries.insert(0, rec.to_metadata_json())
        index_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    async with _index_lock(channel, str(chat_id)):
        await asyncio.get_event_loop().run_in_executor(None, _sync_index)

    log.debug(
        "channels: persisted %s/%s/%s kind=%s size=%dB path=%s",
        channel, chat_id, message_id, kind, len(data), storage_path,
    )
    return rec


def load_channel_index(channel: str, chat_id: str) -> list[dict[str, Any]]:
    """Return the newest-first list of stored attachments for one chat."""
    index_path = (
        _uploads_root()
        / "_index"
        / _safe_segment(channel)
        / f"{_safe_segment(str(chat_id))}.json"
    )
    if not index_path.exists():
        return []
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return entries if isinstance(entries, list) else []


def resolve_storage_path(storage_path: str) -> Path | None:
    """Resolve a stored ``storage_path`` to an absolute Path, guarding traversal."""
    if not storage_path:
        return None
    candidate = _uploads_root() / Path(storage_path)
    if not _is_within_uploads_root(candidate) or not candidate.exists():
        return None
    return candidate


__all__ = [
    "StoredChannelAttachment",
    "save_channel_attachment",
    "load_channel_index",
    "resolve_storage_path",
]
