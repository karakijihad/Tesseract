from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from tesseract.mirror.server.uploads._storage import (
    UPLOAD_ROOT,
    StoredAttachment,
    _attachment_file_path,
    attachment_part_for_model,
)
from tesseract.mirror.server.uploads._index import _metadata_path
from tesseract.mirror.server.uploads._validation import _safe_segment

__all__ = [
    "UPLOAD_ROOT",
    "StoredAttachment",
    "attachment_part_for_model",
    "load_attachment",
    "public_attachment_meta",
    "_validated_attachments",
    "_chat_content_for_model",
]


def load_attachment(session_id: str, attachment_id: str) -> StoredAttachment | None:
    session_id = _safe_segment(session_id)
    attachment_id = _safe_segment(attachment_id)
    if not session_id or not attachment_id:
        return None
    meta_path = _metadata_path(session_id, attachment_id)
    if meta_path is None:
        return None
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return StoredAttachment(
            id=str(raw["id"]),
            session_id=str(raw["session_id"]),
            filename=str(raw["filename"]),
            mime_type=str(raw["mime_type"]),
            size=int(raw["size"]),
            kind=str(raw["kind"]),
            url=str(raw["url"]),
            created_at=str(raw["created_at"]),
            storage_path=str(raw.get("storage_path", "")),
        )
    except Exception:
        return None


def public_attachment_meta(att: StoredAttachment) -> dict[str, Any]:
    return att.to_json()


def _validated_attachments(
    app: web.Application,
    session: Any,
    raw: Any,
) -> list[dict[str, Any]] | None:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        return None
    cfg = app["config"].uploads
    if len(raw) > cfg.max_files_per_message:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_size = 0
    max_total_bytes = cfg.max_total_mb * 1024 * 1024
    for item in raw:
        if not isinstance(item, dict):
            return None
        attachment_id = item.get("id")
        if not isinstance(attachment_id, str) or not attachment_id:
            return None
        if attachment_id in seen:
            continue
        seen.add(attachment_id)
        att = load_attachment(session.session_id, attachment_id)
        if att is None:
            return None
        if att.mime_type not in cfg.allowed_mime_types:
            return None
        total_size += att.size
        if total_size > max_total_bytes:
            return None
        out.append(public_attachment_meta(att))
    return out


async def _chat_content_for_model(
    text: str,
    attachments: list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    if not attachments:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for meta in attachments:
        att = load_attachment(str(meta.get("session_id", "")), str(meta.get("id", "")))
        if att is None:
            continue
        part = await attachment_part_for_model(att)
        if part is not None:
            data = part["data"]
            part.update(public_attachment_meta(att))
            part["data"] = data
            parts.append(part)
    return parts if parts else text
