"""Chunk-handling cluster — `_handle_chunk` dispatch, its orb/voice/posture emit helpers, and the workspace-reply broadcast."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.mirror.server.envelope import (
    chunk_to_envelope,
    make_entity_state_set,
    make_envelope,
    make_tasks_state,
    make_voice_instruction,
)
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.mirror.server.stream_parser import _split_text_for_surfaces
from tesseract.mirror.server.turn_context import get_turn_state, get_workspace_origin
from tesseract.mirror.server.tts import _maybe_emit_tts_sentences

log = logging.getLogger(__name__)

# Auto-state thresholds. `_DEEP_FOCUS_TOOL_THRESHOLD`: when a single turn
# fires this many tool calls, the orb latches to `deep_focus` once (per
# turn) so the operator sees a sustained-work signal. `_HAPPY_IMPORTANCE`:
# successful `memory_save` calls at this importance level or above flip
# the orb to `happy`. Both are auto-set by the runtime — the assistant shouldn't
# also call `set_state` for them (the prompt nudge says so).
_DEEP_FOCUS_TOOL_THRESHOLD = 2
_HAPPY_IMPORTANCE = 8


async def _broadcast_workspace_reply(
    app: web.Application,
    call_id: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Broadcast the exact comment created by a `workspace_reply` tool result.

    Codex-fix M2 (2026-05-23): the workspace_reply tool's
    ``ToolResult.metadata`` carries the appended ``{event_id, comment_id}``
    (see ``tesseract/kernel/tools/workspace_reply.py``). That metadata is
    propagated through the chunk pipeline as ``chunk.raw["metadata"]``
    by ``ChatSession._result_chunk``. We look up the comment by exact id
    and broadcast it. Missing metadata fails closed: live broadcast is
    skipped rather than guessing from the latest the assistant comment, which
    races under concurrent synthetic turns.
    """
    store = app.get("workspace_event_store")
    if store is None:
        return
    comment_id = None
    if isinstance(metadata, dict):
        raw_id = metadata.get("comment_id")
        if isinstance(raw_id, str) and raw_id:
            comment_id = raw_id
    target = None
    if comment_id is not None:
        try:
            target = store.get_comment(comment_id)
        except Exception:
            log.exception(
                "workspace reply broadcast: lookup by comment_id %r failed", comment_id,
            )
            return
        if target is None:
            log.warning(
                "workspace reply broadcast: comment_id %r not found on disk "
                "(call_id=%s)", comment_id, call_id,
            )
            return
    else:
        log.warning(
            "workspace reply broadcast: tool metadata missing comment_id; "
            "skipping live broadcast (call_id=%s)", call_id,
        )
        return
    try:
        from tesseract.workspace_events.broadcast import broadcast_comment_appended
        await broadcast_comment_appended(app, target)
    except Exception:
        log.exception("workspace reply broadcast: send failed")


class _LegacyTurnStateView:
    """Read/write per-turn fields on the ServerSession for callers that
    drive `_handle_chunk` directly (mostly test fixtures predating the
    Codex-fix M1 TurnState migration). The view's attribute names match
    ``TurnState`` so the chunk handler can stay symmetric.

    Production paths always have a real ``TurnState`` set via the
    ``current_turn_state`` ContextVar — they never reach this view.
    """

    __slots__ = ("_session",)

    def __init__(self, session: ServerSession) -> None:
        # `pending_happy_saves` may not exist on stub sessions; lazily
        # default to an empty set so the chunk handler's `add()` / `in`
        # operations don't AttributeError.
        if not hasattr(session, "pending_happy_saves") or session.pending_happy_saves is None:
            session.pending_happy_saves = set()
        if not hasattr(session, "turn_tool_count"):
            session.turn_tool_count = 0
        if not hasattr(session, "deep_focus_latched"):
            session.deep_focus_latched = False
        if not hasattr(session, "tool_names_by_call"):
            session.tool_names_by_call = {}
        self._session = session

    @property
    def tool_names_by_call(self) -> dict[str, str]:
        return self._session.tool_names_by_call

    @property
    def pending_happy_saves(self) -> set[str]:
        return self._session.pending_happy_saves

    @property
    def turn_tool_count(self) -> int:
        return self._session.turn_tool_count

    @turn_tool_count.setter
    def turn_tool_count(self, value: int) -> None:
        self._session.turn_tool_count = value

    @property
    def deep_focus_latched(self) -> bool:
        return self._session.deep_focus_latched

    @deep_focus_latched.setter
    def deep_focus_latched(self, value: bool) -> None:
        self._session.deep_focus_latched = value

    @property
    def workspace_reply_succeeded(self) -> bool:
        return getattr(self._session, "workspace_reply_succeeded", False)

    @workspace_reply_succeeded.setter
    def workspace_reply_succeeded(self, value: bool) -> None:
        self._session.workspace_reply_succeeded = value

    # inc.C2: parser carry state. Production turns own these on the real
    # TurnState; the direct-_handle_chunk test path reads/writes the session's
    # transitional fields through this view so the parser stays symmetric.
    @property
    def stream_status_buffer(self) -> str:
        return getattr(self._session, "stream_status_buffer", "")

    @stream_status_buffer.setter
    def stream_status_buffer(self, value: str) -> None:
        self._session.stream_status_buffer = value

    @property
    def stream_tag_state(self) -> str:
        return getattr(self._session, "stream_tag_state", "outside")

    @stream_tag_state.setter
    def stream_tag_state(self, value: str) -> None:
        self._session.stream_tag_state = value

    @property
    def stream_untagged_warned(self) -> bool:
        return getattr(self._session, "stream_untagged_warned", False)

    @stream_untagged_warned.setter
    def stream_untagged_warned(self, value: bool) -> None:
        self._session.stream_untagged_warned = value


async def _handle_chunk(app: web.Application, session: ServerSession, chunk: StreamChunk) -> None:
    # Lazy: `_emit_entity_signals` still lives in ws.py — it's shared with
    # the WS-connect emit and the entity-signals pump there. A module-level
    # import here would cycle with ws.py's re-export of this module's names.
    from tesseract.mirror.server import ws as _ws

    is_set_mood_result = False
    is_set_state_result = False
    is_memory_save_result = False
    is_workspace_reply_result = False
    is_tasks_mutation_result = False
    happy_save_pending = False
    # Codex-fix M1 (2026-05-23): per-turn state read from the ContextVar
    # so concurrent synthetic + chat turns each see their own. When no
    # TurnState is active (test fixtures that drive _handle_chunk
    # directly without going through _run_turn), fall back to the legacy
    # session-level fields. This preserves backwards-compat without
    # weakening the concurrent-turn isolation that _run_turn provides.
    turn_state = get_turn_state()
    if turn_state is None:
        # Wrap the session's legacy attrs in a thin object with the same
        # field names so the rest of this function reads/writes uniformly.
        turn_state = _LegacyTurnStateView(session)
    if chunk.type is ChunkType.TOOL_CALL_END:
        if chunk.tool_call:
            turn_state.tool_names_by_call[chunk.tool_call.id] = chunk.tool_call.name
            # Stash the call-id of any high-importance memory_save so the
            # TOOL_RESULT branch can flip the orb to `happy` on success.
            if chunk.tool_call.name == "memory_save":
                imp = 0
                tool_input = chunk.tool_call.input or {}
                if isinstance(tool_input, dict):
                    try:
                        imp = int(tool_input.get("importance") or 0)
                    except (TypeError, ValueError):
                        imp = 0
                if imp >= _HAPPY_IMPORTANCE:
                    turn_state.pending_happy_saves.add(chunk.tool_call.id)
            turn_state.turn_tool_count += 1
            if (
                not turn_state.deep_focus_latched
                and turn_state.turn_tool_count >= _DEEP_FOCUS_TOOL_THRESHOLD
            ):
                turn_state.deep_focus_latched = True
                await _set_orb_state(app, session, "deep_focus")
        await _emit_posture_event(app, session, chunk)
    elif chunk.type is ChunkType.TOOL_RESULT:
        name = turn_state.tool_names_by_call.pop(chunk.tool_call_id, "")
        is_set_mood_result = name == "set_mood"
        is_set_state_result = name == "set_state"
        is_memory_save_result = name == "memory_save"
        is_workspace_reply_result = name == "workspace_reply"
        is_tasks_mutation_result = name in ("tasks_set", "tasks_update")
        if chunk.tool_call_id in turn_state.pending_happy_saves:
            turn_state.pending_happy_saves.discard(chunk.tool_call_id)
            happy_save_pending = True
        if chunk.raw.get("denied_hard"):
            await send_envelope(session, make_envelope(
                "tool_denied_hard", "execution", session.session_id,
                {
                    "call_id": chunk.tool_call_id,
                    "name": chunk.raw.get("tool_name", ""),
                    "reason": chunk.raw.get("deny_reason", "denied"),
                },
            ))
    if chunk.type is ChunkType.THINKING and chunk.thinking:
        # Streamed reasoning from thinking models (grok/DeepSeek/GLM via the
        # openai adapter, Claude thinking blocks, Gemini thoughts). Sent as
        # its own `thinking` kind — the `intent` surface is reserved for the
        # deliberate one-line <intent> tag contract; multi-paragraph chain-
        # of-thought flooded it (2026-07-16). The frontend currently drops
        # `thinking` deltas (no surface yet — collapsed thinking block is
        # parked); the kind is on the wire so adding the UI needs no backend
        # change. Deliberately NOT sent to TTS. Never appended to history.
        if get_workspace_origin() is None:
            await send_envelope(session, make_envelope(
                "stream_text",
                "loop",
                session.session_id,
                {"delta": chunk.thinking, "kind": "thinking"},
            ))
        return
    if chunk.type is ChunkType.TEXT and chunk.text:
        # Synthetic workspace turns: the assistant replies via the workspace_reply
        # tool; any free-form text is dropped from the chat surface so
        # the synthetic turn stays invisible to the chat conversation.
        # WP-2: per-task ContextVar so concurrent chat + synthetic turns
        # are gated independently.
        if get_workspace_origin() is None:
            for kind, text in _split_text_for_surfaces(session, turn_state, chunk.text):
                await send_envelope(session, make_envelope(
                    "stream_text",
                    "loop",
                    session.session_id,
                    {"delta": text, "kind": kind},
                ))
                await _maybe_emit_tts_sentences(app, session, text, kind=kind)
    else:
        await send_envelope(session, chunk_to_envelope(chunk, session.session_id))
    # Live-broadcast the assistant's workspace_reply so the operator's CommentThread
    # renders the reply immediately. Fires for both synthetic workspace
    # turns and regular chat turns where the assistant calls workspace_reply mid-
    # conversation. Tool name is already resolved into the local flag in
    # the TOOL_RESULT branch above.
    if is_workspace_reply_result and not chunk.error:
        # M2: arm the success flag so `_run_turn`'s finally commits the
        # deferred delivery marks the chat-session drain stashed.
        # Codex-fix M1 (2026-05-23): write to per-turn state; the legacy
        # view above transparently falls back to the session attr when
        # no ContextVar TurnState is active.
        turn_state.workspace_reply_succeeded = True
        metadata = chunk.raw.get("metadata") if isinstance(chunk.raw, dict) else None
        await _broadcast_workspace_reply(app, chunk.tool_call_id, metadata)
    if is_set_mood_result:
        await _ws._emit_entity_signals(app, session)
    if is_tasks_mutation_result and not chunk.error:
        # Mirror the post-mutation list (lives on session.chat_session.
        # tool_context.todos) to the frontend. Replace-wholesale wire
        # contract — frontend renders the latest envelope; no diffing.
        snapshot = [dict(t) for t in session.chat_session.tool_context.todos]
        await send_envelope(session, make_tasks_state(
            session.session_id, items=snapshot,
        ))
    # Skip entity_state_set on rejected calls — the holder isn't mutated
    # on error, so re-emitting the prior value would be a phantom write
    # that the operator/orb interprets as a successful state set.
    if is_set_state_result and not chunk.error:
        await _emit_entity_state_from_affect(app, session)
    # Auto-happy on a successful high-importance memory_save. Skip on
    # rejected calls (dedupe blocks, type_mismatch, WHAT_NOT_TO_SAVE) —
    # the save didn't actually happen, so a "happy" flash would lie.
    if is_memory_save_result and happy_save_pending and not chunk.error:
        await _set_orb_state(app, session, "happy")


async def _emit_entity_state_from_affect(
    app: web.Application,
    session: ServerSession,
) -> None:
    """Mirror-only: after `set_state` runs, surface the post-state to the
    frontend via an `entity_state_set` envelope. Same pattern as the
    voice/mood emissions — the tool stays side-effect-free."""
    affect = app.get("entity_affect")
    if affect is None:
        return
    await send_envelope(session, make_entity_state_set(
        session.session_id,
        state=affect.state,
    ))


async def _set_orb_state(
    app: web.Application,
    session: ServerSession,
    state: str,
) -> None:
    """Runtime-driven orb flip — used for the auto-states (deep_focus on
    long tool chains, happy on high-importance memory saves). Mutates the
    same EntityAffect holder `set_state` writes to so the next signals
    pump and any explicit `set_state` see the latest value.

    No-ops when the affect already holds the target state. Without this
    guard a turn that both calls `set_state("happy")` and triggers an
    auto-happy on a high-importance memory save would emit two
    `entity_state_set` envelopes for the same logical flip, double-
    pulsing the orb."""
    affect = app.get("entity_affect")
    if affect is None:
        return
    if getattr(affect, "state", None) == state:
        return
    affect.set(state)
    await send_envelope(session, make_entity_state_set(
        session.session_id,
        state=state,
    ))


async def _emit_posture_event(app: web.Application, session: ServerSession, chunk: StreamChunk) -> None:
    tc = chunk.tool_call
    if tc is None:
        return
    policy = app["config"].permissions
    posture = policy.resolve_posture(tc.name, tc.input or {})
    if posture == "auto":
        await send_envelope(session, make_envelope(
            "tool_auto", "execution", session.session_id,
            {"call_id": tc.id, "name": tc.name},
        ))
    elif posture == "deny":
        await send_envelope(session, make_envelope(
            "tool_denied_hard", "execution", session.session_id,
            {"call_id": tc.id, "name": tc.name, "reason": "policy default deny"},
        ))
    # "ask" → ask_fn emits tool_ask + tool_approved/tool_denied
