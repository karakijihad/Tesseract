"""Deterministic read-only command router for Telegram (audit fix m2).

Natural-language requests work end-to-end through the assistant, but on a phone
operators want a small, predictable command surface: ``/queue``
returns the workspace inbox depth whether or not chat_brain decides to
call the right tool. Read-only commands only — mutating actions
(``/pause``, ``/cancel``) need the ASK-round-trip semantics nailed down
before they land here.

Each handler is a coroutine that returns a Telegram-ready text body. The
router runs *before* the chat-turn dispatch; on a match the bridge sends
the reply and short-circuits the turn. Unknown ``/foo`` commands fall
through to the normal chat path (so "/foo what should I do today?" still
reaches the assistant).

Tier policy: when the chat is on ``friend`` tier, only ``/status`` and
``/help`` are served. The rest report "not available on this tier" so a
friend never sees operator state via a deterministic path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Commands a non-operator chat may invoke. Vestigial on a single-operator
# install — `ctx.tier` resolves to "operator" for every chat, so this never
# denies — and kept only because the dispatcher still reads it. The tool
# denylist it used to mirror (`_channel_tier.FRIEND_DENIED_TOOLS`) is DELETED:
# it lived in the ask_fn wrapper, so any AUTO posture skipped it entirely.
_FRIEND_ALLOWED: frozenset[str] = frozenset({"/status", "/help", "/clear"})

CommandHandler = Callable[["TelegramCommandContext"], Awaitable[str]]


class TelegramCommandContext:
    """Bundle the bridge surfaces a command handler is allowed to touch.

    Kept narrow on purpose — the router is read-only, so it never gets
    write hooks (``approve``, ``revoke``, ``save_state``). A handler that
    needs more should be promoted out of this module to a real tool.
    """

    def __init__(
        self,
        *,
        app: Any,
        chat_id: int,
        tier: str,
        offline: bool,
        bridge: Any,
    ) -> None:
        self.app = app
        self.chat_id = chat_id
        self.tier = tier
        self.offline = offline
        self.bridge = bridge


# ── Handlers ───────────────────────────────────────────────────────────────


async def _handle_help(ctx: TelegramCommandContext) -> str:
    if ctx.tier == "friend":
        return (
            "Available commands:\n"
            "/status — bridge state\n"
            "/clear — clear this thread (asks YES/NO first)\n"
            "/help — this list\n\n"
            "Other commands are operator-only on this chat."
        )
    return (
        "Available commands:\n"
        "/status — bridge state (online/offline/busy)\n"
        "/queue — workspace inbox depth\n"
        "/brief — latest daily brief summary\n"
        "/clear — clear this thread (asks YES/NO first)\n"
        "/voice_on — the assistant replies with voice notes\n"
        "/voice_off — back to text replies\n"
        "/help — this list\n\n"
        "Anything else routes to the assistant as normal chat."
    )


async def _handle_queue(ctx: TelegramCommandContext) -> str:
    if ctx.tier == "friend":
        return "Queue is not available on this tier."
    event_store = (
        ctx.app.get("workspace_event_store") if hasattr(ctx.app, "get") else None
    )
    if event_store is None:
        return "Workspace event store not attached."
    try:
        events = event_store.list_events(kinds=("agent_post",), limit=200)
    except Exception:
        log.exception("commands: list workspace events failed")
        return "Queue lookup failed — see backend log."
    open_count = 0
    for ev in events or []:
        # An event with `payload.status in {"open", None}` is unresolved.
        # The schema doesn't reliably mark "resolved" so we treat any
        # explicit closed marker as resolved and everything else as open.
        payload = getattr(ev, "payload", {}) or {}
        if isinstance(payload, dict) and payload.get("status") in ("approved", "rejected", "closed"):
            continue
        open_count += 1
    return f"Workspace inbox: {open_count} open item(s) waiting."


async def _handle_brief(ctx: TelegramCommandContext) -> str:
    if ctx.tier == "friend":
        return "Brief is not available on this tier."
    event_store = (
        ctx.app.get("workspace_event_store") if hasattr(ctx.app, "get") else None
    )
    if event_store is None:
        return "Workspace event store not attached."
    try:
        events = event_store.list_events(kinds=("daily_brief",), limit=5)
    except Exception:
        log.exception("commands: list daily_brief failed")
        return "Brief lookup failed — see backend log."
    payload: dict[str, Any] | None = None
    for ev in events or []:
        candidate = getattr(ev, "payload", None)
        if isinstance(candidate, dict) and candidate.get("sections"):
            payload = candidate
            break
    if payload is None:
        return "No daily brief yet."
    from tesseract.integrations.telegram.brief_push import format_exec_summary

    text = format_exec_summary(payload)
    return text or "Brief payload is empty."


async def _handle_clear(ctx: TelegramCommandContext) -> str:
    """Stamp pending-clear and return the confirmation prompt.

    The bridge intercepts the *next* inbound message and branches on its
    body — `yes`/`y` triggers a reflection turn then clears, `no`/`n`
    just clears, anything else cancels the pending stamp and processes
    normally. Auto-expires after 5 minutes.
    """
    from tesseract.integrations.telegram.state import save_state

    chat_key = str(ctx.chat_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    poll_state = ctx.bridge._state.poll_state  # noqa: SLF001
    with ctx.bridge._state.with_lock():  # noqa: SLF001
        poll_state.pending_clear[chat_key] = now_iso
        save_state(ctx.bridge._state.state_path, poll_state)  # noqa: SLF001
    return (
        "🧹 Clear this thread?\n"
        "Reply <b>YES</b> to reflect briefly then clear, "
        "<b>NO</b> to clear without reflecting, "
        "or anything else to cancel."
    )


async def _handle_voice_on(ctx: TelegramCommandContext) -> str:
    """Flip the per-chat ``reply_voice`` flag on (Session 3 2026-05-16).

    Subsequent the assistant replies in this chat synthesise via the configured TTS lane
    and ship as voice notes instead of plain text. Operator-only — friend
    tier hits ``_FRIEND_ALLOWED`` deny in the dispatcher.
    """
    from tesseract.integrations.telegram.state import save_state

    chat_key = str(ctx.chat_id)
    poll_state = ctx.bridge._state.poll_state  # noqa: SLF001
    with ctx.bridge._state.with_lock():  # noqa: SLF001
        poll_state.reply_voice[chat_key] = True
        save_state(ctx.bridge._state.state_path, poll_state)  # noqa: SLF001
    return (
        "🎙 Voice replies <b>on</b>. I'll synthesise my answers as voice "
        "notes. Use /voice_off to switch back to text."
    )


async def _handle_voice_off(ctx: TelegramCommandContext) -> str:
    """Flip the per-chat ``reply_voice`` flag off (Session 3 2026-05-16)."""
    from tesseract.integrations.telegram.state import save_state

    chat_key = str(ctx.chat_id)
    poll_state = ctx.bridge._state.poll_state  # noqa: SLF001
    with ctx.bridge._state.with_lock():  # noqa: SLF001
        poll_state.reply_voice.pop(chat_key, None)
        save_state(ctx.bridge._state.state_path, poll_state)  # noqa: SLF001
    return "📝 Voice replies <b>off</b>. Back to text."


async def _handle_status(ctx: TelegramCommandContext) -> str:
    if ctx.offline:
        return "the assistant status: offline"
    session = getattr(ctx.bridge, "_sessions", {}).get(ctx.chat_id)
    busy = (
        session is not None
        and getattr(session, "current_turn_task", None) is not None
        and not session.current_turn_task.done()
    )
    return "the assistant status: busy" if busy else "the assistant status: online"


_HANDLERS: dict[str, CommandHandler] = {
    "/help": _handle_help,
    "/queue": _handle_queue,
    "/brief": _handle_brief,
    "/status": _handle_status,
    "/clear": _handle_clear,
    "/voice_on": _handle_voice_on,
    "/voice_off": _handle_voice_off,
}


def is_known_command(text: str) -> bool:
    head = _command_head(text)
    return head in _HANDLERS


async def dispatch(text: str, ctx: TelegramCommandContext) -> str | None:
    """Dispatch a ``/cmd`` to its handler; return the body or ``None``.

    Returns ``None`` when the text is not a recognised command — the
    caller falls back to the normal chat turn. Friend-tier callers
    receive a stable "not available on this tier" reply for any command
    outside ``_FRIEND_ALLOWED``.
    """
    head = _command_head(text)
    if head not in _HANDLERS:
        return None
    if ctx.tier == "friend" and head not in _FRIEND_ALLOWED:
        return "That command is operator-only on this chat."
    try:
        return await _HANDLERS[head](ctx)
    except Exception:
        log.exception("commands: handler crashed for %s", head)
        return f"{head} failed — see backend log."


def _command_head(text: str) -> str:
    head = (text or "").strip().split(maxsplit=1)
    if not head:
        return ""
    first = head[0].lower()
    # Allow `@botname` suffix per Telegram convention: ``/status@ExampleBot``.
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


__all__ = [
    "TelegramCommandContext",
    "dispatch",
    "is_known_command",
]
