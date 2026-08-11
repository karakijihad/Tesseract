"""Drives a plain chat turn for an external-channel message (Telegram et al.)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType
from tesseract.mirror.server.session import ServerSession
from tesseract.mirror.server.stream_parser import _extract_channel_reply

log = logging.getLogger(__name__)


async def _start_channel_turn(
    app: web.Application,
    session: ServerSession,
    *,
    channel: str,
    chat_id: str,
    body: str,
    on_progress: Any | None = None,
    error_out: list[str] | None = None,
) -> str | None:
    """Drive a plain chat turn for an external-channel message.

    Unlike workspace reply dispatch, this does NOT involve the
    ``workspace_events`` / ``workspace_reply`` machinery — channels are
    transient brainstorm surfaces (MO-9-10) whose conversation history
    lives in :class:`ConversationStore`, not in the operator workspace.

    ``on_progress``: optional ``Callable[[ProgressEvent], Awaitable[None]]``.
    When supplied, fires for tool-call lifecycle chunks
    (``TOOL_CALL_START`` / ``TOOL_RESULT``) plus elapsed-time pulses
    (15/30/60/120 s). Exceptions are logged + swallowed so a broken
    progress lambda never aborts the turn. CR-4.

    Returns the assistant's reply text (empty string when the turn
    produced no text) or ``None`` on stream error / cancellation so the
    caller can decide whether to surface a fallback to the remote user.
    The chat history is kept (``transient=False``) so the per-channel
    sliding window can replay context across turns; the bridge trims
    the window after each turn via :func:`apply_retention_inplace`.

    ``error_out``: optional list the caller can pass to observe a
    turn-level error even though this function still returns reply text
    for it (the ``⚠`` envelope below) — populated with the raw
    ``error_holder`` entries when the stream produced an error envelope.
    Additive / opt-in: existing call sites that omit it are unaffected
    (fix pass 1, idle-wake-design.md §G1 outcome-based breaker accounting).
    """
    del app, channel, chat_id  # reserved for future per-channel hooks (cost tagging, etc.)
    if session.current_turn_task and not session.current_turn_task.done():
        # A turn is already in flight for this chat — the caller is
        # expected to serialize remote messages (Telegram delivers
        # updates one at a time per chat), but guard against a race
        # by waiting for the prior turn to finish before starting the
        # next. Returning None signals "no reply produced this call".
        try:
            await session.current_turn_task
        except Exception:
            pass
    cancel_event = session.chat_session.tool_context.cancel_event
    cancel_event.clear()
    reply_holder: list[str] = []
    error_holder: list[str] = []
    history_before = len(session.chat_session.history)

    # Track the most recent TOOL_CALL_START's (id → name, input) so we
    # can attribute the matching TOOL_RESULT back to a tool name + args
    # when the adapter only forwards the call_id on result chunks.
    tool_starts: dict[str, tuple[str, dict[str, Any]]] = {}
    # Call ids that reached a TOOL_RESULT, so the sweep at the end of the
    # stream can close only the pulses that never did.
    tool_ended: set[str] = set()
    turn_started_at = time.monotonic()
    elapsed_task: asyncio.Task[None] | None = None

    async def _safe_progress(event: Any) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel turn: on_progress raised; continuing turn")

    async def _elapsed_pump() -> None:
        from tesseract.integrations._channel_progress import (
            ELAPSED_TICKS_S,
            ProgressEvent,
        )
        try:
            for tick in ELAPSED_TICKS_S:
                wait = tick - (time.monotonic() - turn_started_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                await _safe_progress(ProgressEvent(kind="elapsed", elapsed_s=tick))
        except asyncio.CancelledError:
            raise

    async def _drive() -> None:
        from tesseract.integrations._channel_progress import ProgressEvent
        try:
            async for chunk in session.chat_session.send(body):
                if chunk.type == ChunkType.TEXT and chunk.text:
                    reply_holder.append(chunk.text)
                elif chunk.type == ChunkType.TOOL_CALL_START:
                    if on_progress is not None and chunk.tool_call is not None:
                        tc = chunk.tool_call
                        tool_starts[tc.id] = (tc.name, dict(tc.input or {}))
                        await _safe_progress(ProgressEvent(
                            kind="tool_start",
                            tool_name=tc.name,
                            tool_args=dict(tc.input or {}),
                        ))
                elif chunk.type == ChunkType.TOOL_CALL_END:
                    # Some adapters fill `input` only at END (delta-only
                    # streams). Refresh the cache so TOOL_RESULT can
                    # still surface the args.
                    if chunk.tool_call is not None:
                        tc = chunk.tool_call
                        tool_starts[tc.id] = (tc.name, dict(tc.input or {}))
                elif chunk.type == ChunkType.TOOL_RESULT:
                    tool_ended.add(chunk.tool_call_id)
                    if on_progress is not None:
                        name, args = tool_starts.get(chunk.tool_call_id, ("", {}))
                        await _safe_progress(ProgressEvent(
                            kind="tool_end",
                            tool_name=name,
                            tool_args=args,
                        ))
                elif chunk.type == ChunkType.ERROR:
                    error_holder.append(chunk.error or "stream error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "channel turn failed for %s: %s: %s",
                session.session_id,
                type(exc).__name__,
                exc,
            )
            error_holder.append(f"channel turn crashed: {type(exc).__name__}: {exc}")
        finally:
            # A `tool_start` pulse with no matching result leaves the channel
            # showing "calling <tool>" forever. That happens whenever a call is
            # announced and then never runs: an adapter error mid-arguments, a
            # cancelled or crashed turn, or a call the adapter discards because
            # the provider truncated its JSON. In `finally` because the paths
            # that strand a pulse are exactly the ones that skip a normal exit.
            # On cancellation the first await here re-raises and the sweep stops
            # short — cancellation has to win — so a barged-in turn can still
            # leave one open. Every other path closes.
            for call_id, (name, args) in tool_starts.items():
                if call_id in tool_ended:
                    continue
                await _safe_progress(ProgressEvent(
                    kind="tool_end", tool_name=name, tool_args=args,
                ))

    turn_task = asyncio.create_task(
        _drive(),
        name=f"channel_turn:{session.session_id}",
    )
    session.current_turn_task = turn_task
    if on_progress is not None:
        elapsed_task = asyncio.create_task(
            _elapsed_pump(),
            name=f"channel_turn_elapsed:{session.session_id}",
        )
    turn_cancelled = False
    try:
        await turn_task
    except asyncio.CancelledError:
        turn_cancelled = True
    finally:
        session.current_turn_task = None
        if elapsed_task is not None and not elapsed_task.done():
            elapsed_task.cancel()
            try:
                await elapsed_task
            except (asyncio.CancelledError, Exception):
                pass
        # The channel's own commit gate — this path never goes through
        # `turn_runner._run_turn`, so without it a spawn completion drained
        # into a channel turn that then died would be gone. Cancelled or
        # errored puts the notes back for the next turn.
        try:
            if turn_cancelled or error_holder:
                session.chat_session.rollback_spawn_delivery()
            else:
                session.chat_session.confirm_spawn_delivery()
        except Exception:
            log.exception("channel turn: spawn delivery commit/rollback failed")
    if turn_cancelled:
        return None

    raw = "".join(reply_holder)
    reply = _extract_channel_reply(raw).strip()
    if not reply and len(session.chat_session.history) > history_before:
        for entry in reversed(session.chat_session.history):
            if entry.get("role") == "assistant":
                content = entry.get("content")
                if isinstance(content, str):
                    reply = content.strip()
                break
    if error_holder:
        # Stream produced an error envelope (tool-cap hit, adapter crash,
        # cancellation). Pre-fix code returned ``None`` here, leaving
        # channel users with the bridge's generic "(no reply produced
        # this turn)" message. Append the error text to whatever partial
        # reply we have so the user sees something concrete and knows
        # to retry / rephrase. Friend-tier sessions get a redacted
        # suffix so an exception message can't leak operator-internal
        # paths or tool names (reviewer P1-1) — full text stays in the
        # backend log.
        log.warning("channel turn error for %s: %s", session.session_id, error_holder[0])
        if error_out is not None:
            error_out.extend(error_holder)
        channel_tier = getattr(session, "channel_tier", "operator")
        if channel_tier == "operator":
            suffix = error_holder[0].strip()
        else:
            suffix = "I hit an error processing that. Try again?"
        if reply:
            reply = f"{reply}\n\n⚠ {suffix}"
        else:
            reply = f"⚠ {suffix}"
    return reply or None
