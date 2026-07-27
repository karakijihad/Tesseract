"""Chat-CRUD REST routes for mirror-multi-chat (P1).

Session-agnostic `/api/chats/*` over ``chat_store``: chats persist across WS
connections, while ``ServerSession.session_id`` is per-connection ephemeral —
so the chat library is NOT scoped by session_id. Creating a chat is WS-only
(``chat.create`` on the live ServerSession); a REST create would orphan a disk
chat with no live ChatSession. Hard-delete requires a prior archive (D1).
"""

from __future__ import annotations

from aiohttp import web

from tesseract.mirror.server import chat_store


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes")


async def list_chats_handler(request: web.Request) -> web.Response:
    include_archived = _truthy(request.query.get("include_archived"))
    rows = chat_store.list_chats(include_archived=include_archived)
    return web.json_response({"chats": rows})


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
    if not chat_store.rename_chat(request.match_info["chat_id"], title):
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"ok": True, "title": title})


async def archive_chat_handler(request: web.Request) -> web.Response:
    if not chat_store.set_archived(request.match_info["chat_id"], True):
        return web.json_response({"error": "not_found"}, status=404)
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
    ok, reason = chat_store.delete_chat(chat_id)
    if not ok:
        status = 404 if reason == "not_found" else 400 if reason == "invalid_id" else 500
        return web.json_response({"error": reason}, status=status)
    return web.json_response({"ok": True})
