from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from aiohttp import web

from tesseract.mirror.server.uploads._index import (
    _index_attachment,
    _unindex_attachment,
)
from tesseract.mirror.server.uploads._storage import (
    UPLOAD_ROOT,
    StoredAttachment,
    _attachment_dir,
    _attachment_file_path,
    _attachment_url,
    _is_within_upload_root,
    _remove_tree_async,
    _storage_path,
)
from tesseract.mirror.server.uploads._validation import (
    _EXT_MIME,
    _detect_mime,
    _kind_for_mime,
    _mime_matches_magic,
    _safe_filename,
    _safe_segment,
)
from tesseract.mirror.server.uploads import load_attachment, public_attachment_meta


async def upload_chat_attachment(request: web.Request) -> web.Response:
    session_id = _safe_segment(request.match_info["session_id"])
    if not session_id:
        return web.json_response({"error": "invalid_session_id"}, status=400)

    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "file":
        return web.json_response({"error": "missing_file"}, status=400)

    filename = _safe_filename(part.filename or "attachment")
    mime_type = _detect_mime(filename, part.headers.get("Content-Type", ""))
    if mime_type is None:
        return web.json_response({"error": "unsupported_type"}, status=415)
    cfg = request.app["config"].uploads
    if mime_type not in cfg.allowed_mime_types:
        return web.json_response({"error": "unsupported_type", "mime_type": mime_type}, status=415)

    max_bytes = cfg.max_file_mb * 1024 * 1024
    attachment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    kind = _kind_for_mime(mime_type)
    storage_path = _storage_path(kind, created_at, session_id, attachment_id)
    dest_dir = UPLOAD_ROOT / storage_path
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = dest_dir / filename

    size = 0
    head = bytearray()
    try:
        with file_path.open("wb") as fh:
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                if len(head) < 16:
                    head.extend(chunk[: 16 - len(head)])
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("too_large")
                fh.write(chunk)
    except ValueError:
        await _remove_tree_async(dest_dir)
        return web.json_response(
            {"error": "file_too_large", "max_file_mb": cfg.max_file_mb},
            status=413,
        )
    if not _mime_matches_magic(mime_type, bytes(head)):
        await _remove_tree_async(dest_dir)
        return web.json_response({"error": "invalid_file_signature"}, status=415)

    att = StoredAttachment(
        id=attachment_id,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
        kind=kind,
        url=_attachment_url(session_id, attachment_id, filename),
        created_at=created_at.isoformat(),
        storage_path=storage_path.as_posix(),
    )
    (dest_dir / "metadata.json").write_text(
        json.dumps(att.to_metadata_json(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    await _index_attachment(att)
    return web.json_response({"attachment": att.to_json()})


async def get_chat_upload_config(request: web.Request) -> web.Response:
    cfg = request.app["config"].uploads
    allowed = list(cfg.allowed_mime_types)
    extensions = sorted(ext for ext, mime in _EXT_MIME.items() if mime in allowed)
    return web.json_response({
        "max_file_mb": cfg.max_file_mb,
        "max_total_mb": cfg.max_total_mb,
        "max_files_per_message": cfg.max_files_per_message,
        "allowed_mime_types": allowed,
        "allowed_extensions": extensions,
    })


async def get_chat_attachment(request: web.Request) -> web.Response:
    session_id = _safe_segment(request.match_info["session_id"])
    attachment_id = _safe_segment(request.match_info["attachment_id"])
    if not session_id or not attachment_id:
        return web.json_response({"error": "not_found"}, status=404)
    att = load_attachment(session_id, attachment_id)
    if att is None:
        return web.json_response({"error": "not_found"}, status=404)
    file_path = _attachment_file_path(att)
    if file_path is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.FileResponse(
        file_path,
        headers={"Content-Disposition": f'inline; filename="{att.filename}"'},
    )


async def delete_chat_attachment(request: web.Request) -> web.Response:
    session_id = _safe_segment(request.match_info["session_id"])
    attachment_id = _safe_segment(request.match_info["attachment_id"])
    if not session_id or not attachment_id:
        return web.json_response({"error": "not_found"}, status=404)
    att = load_attachment(session_id, attachment_id)
    dest_dir = _attachment_dir(att) if att is not None else UPLOAD_ROOT / session_id / attachment_id
    if dest_dir is None or not _is_within_upload_root(dest_dir):
        return web.json_response({"error": "not_found"}, status=404)
    await _remove_tree_async(dest_dir)
    await _unindex_attachment(session_id, attachment_id)
    return web.json_response({"ok": True})


async def promote_chat_attachment_to_vault(request: web.Request) -> web.Response:
    session_id = _safe_segment(request.match_info["session_id"])
    attachment_id = _safe_segment(request.match_info["attachment_id"])
    if not session_id or not attachment_id:
        return web.json_response({"error": "not_found"}, status=404)

    att = load_attachment(session_id, attachment_id)
    if att is None:
        return web.json_response({"error": "not_found"}, status=404)

    file_path = _attachment_file_path(att)
    if file_path is None or not _is_within_upload_root(file_path):
        return web.json_response({"error": "not_found"}, status=404)

    try:
        result = await _promote_to_vault(att, file_path)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    if result.get("ok"):
        return web.json_response(result)
    return web.json_response(result, status=500)


async def _promote_to_vault(att: StoredAttachment, file_path) -> dict:
    from pathlib import Path
    from tesseract.memory.vault_manager import VaultManager
    from tesseract.memory.vault_indexer import VaultIndexer
    from tesseract.kernel.tools.vault_ingest import VaultIngestTool, VaultIngestInput
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.paths import TESSERACT_HOME

    vault_root = TESSERACT_HOME / "vault"
    manager = VaultManager(vault_root=vault_root)
    vault_rel_path = manager.suggest_raw_filing_path(att.filename)

    indexer: VaultIndexer | None = None
    try:
        indexer = VaultIndexer(vault_root=vault_root)
    except Exception:
        pass

    tool = VaultIngestTool(vault_manager=manager, vault_indexer=indexer)

    # Operator clicked "promote to vault" explicitly — skip the two-phase
    # suggest/confirm loop by passing confirmed_path directly. No LLM approval
    # needed; the HTTP request IS the operator approval.
    inp = VaultIngestInput(
        source_path=str(file_path),
        title=att.filename,
        summary=f"Chat attachment from session {att.session_id}",
        tags=["chat-attachment", att.mime_type, att.kind],
        confirmed_path=vault_rel_path,
    )
    ctx = ToolContext(session_id=att.session_id)
    result = await tool.run(inp, ctx)

    if result.is_error:
        return {"ok": False, "error": result.output}
    return {"ok": True, "vault_path": vault_rel_path}
