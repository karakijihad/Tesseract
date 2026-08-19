"""Chat-CRUD REST routes for mirror-multi-chat (P1).

Session-agnostic `/api/chats/*` over ``chat_store``: chats persist across WS
connections, while ``ServerSession.session_id`` is per-connection ephemeral —
so the chat library is NOT scoped by session_id. Creating a chat is WS-only
(``chat.create`` on the live ServerSession); a REST create would orphan a disk
chat with no live ChatSession. Hard-delete requires a prior archive (D1).
"""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_lifecycle import (
    chat_is_busy,
    detach_deleted_chat,
    sessions_holding,
    would_orphan_a_session,
)

log = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes")


def _busy(chat_id: str) -> web.Response:
    return web.json_response({"error": "chat_busy"}, status=409)


async def list_chats_handler(request: web.Request) -> web.Response:
    """`?include_archived=1` widens to archived too; `?archived=only` narrows
    to archived alone, which is what the drawer's archive section wants and
    what the widener could never express."""
    raw = (request.query.get("archived") or "").lower()
    rows = chat_store.list_chats(
        include_archived=_truthy(request.query.get("include_archived")),
        archived_only=raw == "only",
    )
    return web.json_response({"chats": rows})


async def list_chats_by_day_handler(request: web.Request) -> web.Response:
    """Per-day grouped view, newest day first; runs within a day newest first.

    Grouped by `created_at` — the stamp made once when the conversation was
    created. There is no `custom` bucket: that existed only because a filename
    had to be parsed back into a date and sometimes would not.
    """
    raw = (request.query.get("archived") or "").lower()
    return web.json_response({"days": chat_store.list_by_day(
        include_archived=_truthy(request.query.get("include_archived")),
        archived_only=raw == "only",
    )})


async def preview_chat_handler(request: web.Request) -> web.Response:
    preview = chat_store.preview_chat(request.match_info["chat_id"], max_turns=6)
    if preview is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(preview)


async def get_chat_handler(request: web.Request) -> web.Response:
    record = chat_store.load_chat(request.match_info["chat_id"])
    if record is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(record.to_dict())


async def rename_chat_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    title = (body.get("title") or "").strip()
    if not title:
        return web.json_response({"error": "empty_title"}, status=400)
    chat_id = request.match_info["chat_id"]
    try:
        renamed = chat_store.rename_chat(chat_id, title)
    except OSError:
        log.exception("chat rename failed to write for %s", chat_id)
        return web.json_response({"error": "io_error"}, status=500)
    if not renamed:
        return web.json_response({"error": "not_found"}, status=404)
    for srv in sessions_holding(request.app, chat_id):
        srv.chat_meta[chat_id].title = title
    return web.json_response({"ok": True, "title": title})


async def archive_chat_handler(request: web.Request) -> web.Response:
    chat_id = request.match_info["chat_id"]
    if would_orphan_a_session(request.app, chat_id):
        return web.json_response({"error": "last_open_chat"}, status=409)
    if chat_is_busy(request.app, chat_id):
        return _busy(chat_id)
    if not chat_store.set_archived(chat_id, True):
        return web.json_response({"error": "not_found"}, status=404)
    for srv in sessions_holding(request.app, chat_id):
        if chat_id in srv.chat_order:
            srv.archive_chat(chat_id)
        else:
            srv.chat_meta[chat_id].archived = True
    return web.json_response({"ok": True})


async def restore_chat_handler(request: web.Request) -> web.Response:
    chat_id = request.match_info["chat_id"]
    record = chat_store.load_chat(chat_id)
    if record is None:
        return web.json_response({"error": "not_found"}, status=404)
    # Restore only operates within the archived set (mirror of delete's
    # archive-first guard): restoring an already-open chat is a contract
    # violation, not a silent no-op 200.
    if not record.archived:
        return web.json_response({"error": "not_archived"}, status=409)
    chat_store.set_archived(chat_id, False)
    for srv in sessions_holding(request.app, chat_id):
        srv.reopen_chat(chat_id)
    return web.json_response({"ok": True})


async def delete_chat_handler(request: web.Request) -> web.Response:
    chat_id = request.match_info["chat_id"]
    record = chat_store.load_chat(chat_id)
    if record is None:
        return web.json_response({"error": "not_found"}, status=404)
    # D1 — hard-delete only after the chat has been archived (the operator's
    # explicit second step). A live/open chat must be archived first.
    if not record.archived:
        return web.json_response({"error": "not_archived"}, status=409)
    # A chat archived HERE can still be open in another connection, which was
    # never told: nothing broadcasts an archive between sessions. Deleting it
    # out from under that session is what `detach_deleted_chat` exists to do
    # safely, and refusing beats orphaning it.
    if would_orphan_a_session(request.app, chat_id):
        return web.json_response({"error": "last_open_chat"}, status=409)
    if chat_is_busy(request.app, chat_id):
        return _busy(chat_id)
    ok, reason = chat_store.delete_chat(chat_id)
    if not ok:
        status = 404 if reason == "not_found" else 400 if reason == "invalid_id" else 500
        return web.json_response({"error": reason}, status=status)
    detach_deleted_chat(request.app, chat_id)
    return web.json_response({"ok": True})
