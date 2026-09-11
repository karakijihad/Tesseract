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



# Exception-shaped fragments. A message carrying one is an internal failure
# leaking its own guts — class name, interpolated path, whatever the raise
# site happened to include — and it goes to the log, not down a channel that
# transits someone else's servers. Anything else is the runtime's own words
# ("tool iteration cap reached"), which is exactly what the operator needs in
# order to know what to do next, so it goes through.
#
# Deliberately a shape check and not a scrubber: partially cleaning an
# exception string leaves you guessing which half was the secret. An adapter
# crash arriving as a stream ERROR envelope looks identical to one arriving as
# a raised exception, so the test cannot be "where did this come from".
_LEAKY_ERROR_MARKERS = ("Error", "Exception", "Traceback", "/", "\\")

_CHANNEL_SAFE_FALLBACK = "I hit an error processing that. Try again?"


def _channel_safe_error(text: str) -> str:
    """The error text as it may leave the machine over a channel."""
    stripped = (text or "").strip()
    if not stripped:
        return _CHANNEL_SAFE_FALLBACK
    if any(marker in stripped for marker in _LEAKY_ERROR_MARKERS):
        return _CHANNEL_SAFE_FALLBACK
    return stripped

async def _start_channel_turn(
    app: web.Application,
    session: ServerSession,
    *,
    channel: str,
    chat_id: str,
    body: str,
    on_progress: Any | None = None,
    error_out: list[str] | None = None,
    refused_out: list[bool] | None = None,
    boundary_when_needed: Any | None = None,
) -> str | None:
    """Drive a plain chat turn for an external-channel message.

    Unlike workspace reply dispatch, this does NOT involve the
    ``workspace_events`` / ``workspace_reply`` machinery — channels are
    transient brainstorm surfaces whose conversation history
    lives in :class:`ConversationStore`, not in the operator workspace.

    ``on_progress``: optional ``Callable[[ProgressEvent], Awaitable[None]]``.
    When supplied, fires for tool-call lifecycle chunks
    (``TOOL_CALL_START`` / ``TOOL_RESULT``) plus elapsed-time pulses
    (15/30/60/120 s). Exceptions are logged + swallowed so a broken
    progress lambda never aborts the turn.

    Returns the assistant's reply text (empty string when the turn
    produced no text) or ``None`` on stream error / cancellation so the
    caller can decide whether to surface a fallback to the remote user.
    The chat history is kept (``transient=False``) so the chat can replay
    context across turns; the adapter bounds it after the turn via
    :func:`tesseract.mirror.server.after_turn.after_turn`, the same function
    and the same threshold the cockpit uses.

    ``boundary_when_needed``: the caller's own after-turn compaction hook, handed
    to the tool loop so a turn that outgrows the ceiling on its own folds and
    carries on. The same callable the caller invokes at the end of the turn,
    passed rather than rebuilt, so there is one compaction call site and not a
    mid-turn variant of it.

    ``error_out``: optional list the caller can pass to observe a
    turn-level error even though this function still returns reply text
    for it (the ``⚠`` envelope below) — populated with the raw
    ``error_holder`` entries when the stream produced an error envelope.
    Additive / opt-in: existing call sites that omit it are unaffected.

    ``refused_out``: optional list, filled with one bool saying whether the
    runtime declined to BEGIN this turn (a spending cap, a tool cap) as opposed
    to running it and failing. Answered here, by the turn, for the same reason
    the cockpit answers it in ``turn_runner``: the session's
    ``last_turn_outcome`` outlives the turn that set it, so a caller reading it
    afterwards can be handed the PREVIOUS turn's refusal and forgive a real
    failure. Guarded on the stream having run to exhaustion, which is when
    ``send`` has just written that field.
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
    # Error text the runtime wrote itself, kept apart from what an exception
    # left behind. `error_holder` stays raw because the log wants the numbers.
    composed: list[str] = []
    stream_ok = False
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
        nonlocal stream_ok
        from tesseract.integrations._channel_progress import ProgressEvent
        from tesseract.mirror.server.stream_parser import _parse_tagged_stream

        # Incremental tag parse, purely so `<intent>` can reach the channel
        # while the turn is still running. The reply itself is still assembled
        # from the raw text at the end — this reads the same stream, it does
        # not consume it.
        parse_state = "outside"
        carry = ""
        intent_buf: list[str] = []

        async def _flush_intent() -> None:
            text = "".join(intent_buf).strip()
            intent_buf.clear()
            if text:
                # Logged because the alternative is unobservable: the intent
                # leaves as a placeholder edit, which the conversation store
                # never records, so a live test could not tell "the model sent
                # no intent" from "the intent never reached the chat".
                log.info(
                    "channel turn: intent -> %s (%d chars)",
                    session.session_id, len(text),
                )
                await _safe_progress(ProgressEvent(kind="intent", text=text))

        try:
            async for chunk in session.chat_session.send(
                body, boundary_when_needed=boundary_when_needed,
            ):
                if chunk.type == ChunkType.TEXT and chunk.text:
                    reply_holder.append(chunk.text)
                    if on_progress is not None:
                        pieces, next_state, carry = _parse_tagged_stream(
                            carry + chunk.text, parse_state
                        )
                        for kind, text in pieces:
                            if kind == "intent":
                                intent_buf.append(text)
                        # The block closed: the sentence is whole, so it can be
                        # said. Emitting per piece would send it a word at a
                        # time as the provider streams it.
                        #
                        # Driven off the buffer, not off a state EDGE. The edge
                        # only sees a block that straddles two chunks; a whole
                        # `<intent>…</intent>` inside one chunk — which is every
                        # intent on a non-streaming adapter, `stream: false` in
                        # `providers.yaml` — entered and left "outside", so it
                        # was collected and silently never said.
                        if intent_buf and next_state != "intent":
                            await _flush_intent()
                        parse_state = next_state
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
                    # A chunk that names its own reason was composed by the
                    # runtime: it is already plain words with no path in it, so
                    # it does not go through the shape check below. That check
                    # exists for exception text, and it treats any slash as a
                    # path marker — which is how a budget refusal reading
                    # `spent $3.0617 / cap $3.0000` reached the operator as
                    # "I hit an error processing that" while they were mid work.
                    if (chunk.raw or {}).get("reason"):
                        composed.append(chunk.error or "")
            stream_ok = True
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
        session.release_turn_slot(None, turn_task)
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
    if refused_out is not None:
        from tesseract.orchestrator.outcome import RunOutcome

        refused_out.append(
            stream_ok
            and getattr(session.chat_session, "last_turn_outcome", None) is RunOutcome.REFUSED
        )
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
        # cancellation). Returning ``None`` here would leave the channel with
        # the bridge's generic "(no reply produced this turn)". The error text
        # goes onto whatever partial reply there is, so the person sees
        # something concrete and knows to retry or rephrase.
        log.warning("channel turn error for %s: %s", session.session_id, error_holder[0])
        if error_out is not None:
            error_out.extend(error_holder)
        # Redaction keys off the SHAPE of the message, not off who is reading:
        # a channel reply transits a third party's servers, so exception text
        # carrying internal paths does not belong in it whoever holds the
        # phone.
        suffix = composed[0] if composed else _channel_safe_error(error_holder[0])
        if reply:
            reply = f"{reply}\n\n⚠ {suffix}"
        else:
            reply = f"⚠ {suffix}"
    return reply or None
