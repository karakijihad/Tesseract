"""GET /api/downloads/chat/{session_id}/{artifact_id}/{filename}

Serves files written by tools (image_generate, future TTS-to-file, future
video-gen) so the chat surface can render them inline. Mirrors the uploads
serve route (`mirror.server.routes.uploads.get_chat_attachment`).
"""

from __future__ import annotations

import re

from aiohttp import web

from tesseract.mirror.server.downloads import (
    download_file_path,
    load_download,
)

_SAFE_SEG = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _safe(seg: str) -> str:
    return seg if _SAFE_SEG.match(seg) else ""


async def get_chat_download(request: web.Request) -> web.Response:
    session_id = _safe(request.match_info["session_id"])
    artifact_id = _safe(request.match_info["artifact_id"])
    if not session_id or not artifact_id:
        return web.json_response({"error": "not_found"}, status=404)
    rec = load_download(session_id, artifact_id)
    if rec is None:
        return web.json_response({"error": "not_found"}, status=404)
    file_path = download_file_path(rec)
    if file_path is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.FileResponse(
        file_path,
        headers={
            "Content-Disposition": f'inline; filename="{rec.filename}"',
            "Content-Type": rec.mime_type or "application/octet-stream",
        },
    )
