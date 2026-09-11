"""Deterministic read-only command router for Telegram (audit fix m2).

Natural-language requests work end-to-end through the assistant, but on a phone
operators want a small, predictable command surface: ``/queue``
returns the workspace inbox depth whether or not chat_brain decides to
call the right tool. Most are read-only. The ones that are not (``/stop``,
``/clear``) are the operator's own controls over their own session, and they
run the same code the cockpit's buttons run rather than a channel-shaped
variant of it.

Each handler is a coroutine that returns a Telegram-ready text body. The
router runs *before* the chat-turn dispatch; on a match the bridge sends
the reply and short-circuits the turn. Unknown ``/foo`` commands fall
through to the normal chat path (so "/foo what should I do today?" still
reaches the assistant).

There is no tier policy. A chat is on the allowlist or the bridge never gets
this far, and being on it means being the operator, so every command is
served to everyone who can reach the router.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, Awaitable, Callable

from tesseract.mirror.server.stop import describe, stop_session
from tesseract.workspace_events.events import DECIDABLE_KINDS, SETTLED

log = logging.getLogger(__name__)

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
        offline: bool,
        bridge: Any,
    ) -> None:
        self.app = app
        self.chat_id = chat_id
        self.offline = offline
        self.bridge = bridge


# ── Handlers ───────────────────────────────────────────────────────────────


async def _handle_help(ctx: TelegramCommandContext) -> str:
    return (
        "Available commands:\n"
        "/stop — stop everything this conversation is running\n"
        "/status — bridge state (online/offline/busy)\n"
        "/queue — workspace inbox depth\n"
        "/context — how full this conversation is\n"
        "/brief — latest daily brief summary\n"
        "/clear — reflect and clear this thread (asks whether to hand the state over)\n"
        "/voice_on — the assistant replies with voice notes\n"
        "/voice_off — back to text replies\n"
        "/help — this list\n\n"
        "Anything else routes to the assistant as normal chat."
    )


async def _handle_queue(ctx: TelegramCommandContext) -> str:
    event_store = (
        ctx.app.get("workspace_event_store") if hasattr(ctx.app, "get") else None
    )
    if event_store is None:
        return "Workspace event store not attached."
    try:
        events = event_store.list_events(kinds=DECIDABLE_KINDS, limit=200)
    except Exception:
        log.exception("commands: list workspace events failed")
        return "Queue lookup failed — see backend log."
    waiting: dict[str, int] = {}
    for ev in events or []:
        if str(getattr(ev, "status", "") or "") in SETTLED:
            continue
        payload = getattr(ev, "payload", {}) or {}
        if isinstance(payload, dict) and payload.get("status") in SETTLED:
            continue
        waiting[str(getattr(ev, "kind", "") or "something")] = (
            waiting.get(str(getattr(ev, "kind", "") or "something"), 0) + 1
        )
    total = sum(waiting.values())
    if not total:
        return "Nothing is waiting on you."
    # Named, not just counted. "3 items waiting" tells the operator to walk to
    # their desk without telling them whether it is worth the walk, and this is
    # the one view of the inbox a phone has.
    listed = ", ".join(
        f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(waiting.items())
    )
    # No "needs the app" any more. That was true for the hour between this
    # command learning to count properly and `workspace_decide` existing, and
    # it stopped being true in the same session. A surface that tells the
    # operator to go somewhere else, when it can in fact answer, is the fork
    # this whole funnel exists to prevent — written in prose, which is the
    # form of it that is hardest to notice.
    return (
        f"{total} thing(s) waiting on you: {listed}. "
        "Ask me about any of them and I can approve or reject it here."
    )


async def _handle_brief(ctx: TelegramCommandContext) -> str:
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
    from tesseract.integrations._brief_push import compose_brief
    from tesseract.integrations.telegram.render import render_telegram

    text = render_telegram(compose_brief(payload))
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
        # Asking to clear IS an interaction on this day. Only the ordinary
        # turn used to stamp this, and both halves of the clear exchange
        # return before reaching it, so the marker still held yesterday and
        # the next real message was told again that a new day had started
        # and offered the clear the operator had just done.
        poll_state.last_message_ts[chat_key] = now_iso
        save_state(ctx.bridge._state.state_path, poll_state)  # noqa: SLF001
    return (
        "🧹 Clear this thread?\n"
        "Either way I reflect first and write down what it taught me.\n"
        "Reply <b>YES</b> to also hand the state over to the fresh thread, "
        "<b>NO</b> to start clean, "
        "or anything else to cancel."
    )


async def _handle_voice_on(ctx: TelegramCommandContext) -> str:
    """Flip the per-chat ``reply_voice`` flag on.

    Subsequent the assistant replies in this chat synthesise via the configured TTS lane
    and ship as voice notes instead of plain text.
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
    """Flip the per-chat ``reply_voice`` flag off."""
    from tesseract.integrations.telegram.state import save_state

    chat_key = str(ctx.chat_id)
    poll_state = ctx.bridge._state.poll_state  # noqa: SLF001
    with ctx.bridge._state.with_lock():  # noqa: SLF001
        poll_state.reply_voice.pop(chat_key, None)
        save_state(ctx.bridge._state.state_path, poll_state)  # noqa: SLF001
    return "📝 Voice replies <b>off</b>. Back to text."


async def _handle_context(ctx: TelegramCommandContext) -> str:
    """How full this conversation is, from the tool the assistant calls.

    It runs `context_read` rather than measuring the session here. A command
    that read the numbers itself would be a second answer to a question the
    runtime already answers, and when the two drifted the operator would have
    no way to tell which one had.
    """
    from tesseract.kernel.tools.context_read import ContextReadInput, ContextReadTool

    session = getattr(ctx.bridge, "_sessions", {}).get(ctx.chat_id)
    chat_session = getattr(session, "chat_session", None)
    if chat_session is None:
        return (
            "No conversation is open on this chat yet, so there is nothing to "
            "measure. Send a message first."
        )
    result = await ContextReadTool().run(
        ContextReadInput(), chat_session.tool_context
    )
    return result.output


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


async def _handle_stop(ctx: TelegramCommandContext) -> str:
    """Break the loop for this chat's session.

    The same act, and the same code, as the cockpit's Stop button and `/stop`
    typed in the cockpit: `stop.stop_session`. Reached from here rather than
    from the turn path because the command router runs BEFORE the turn is
    started, which is what makes it answerable at all while a turn holds the
    chat. The turn path waits on the running task, so a stop routed through it
    could only ever arrive after the thing it was meant to stop.

    Returns at once. Cancelling a turn does not mean the turn has finished
    unwinding, and the operator should not be left watching a silent chat
    while a tool's last thread returns.
    """
    session = ctx.bridge._sessions.get(ctx.chat_id)  # noqa: SLF001
    if session is None:
        return "Nothing is running here."
    return describe(stop_session(session))


_HANDLERS: dict[str, CommandHandler] = {
    "/help": _handle_help,
    "/stop": _handle_stop,
    "/queue": _handle_queue,
    "/context": _handle_context,
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
    caller falls back to the normal chat turn.
    """
    head = _command_head(text)
    if head not in _HANDLERS:
        return None
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
