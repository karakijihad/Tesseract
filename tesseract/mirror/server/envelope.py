from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tesseract.kernel.adapters.base import ChunkType, StreamChunk

if TYPE_CHECKING:
    from tesseract.brain.cost.ledger import BudgetState, CostEvent

_CHUNK_TO_EVENT = {
    ChunkType.TEXT: "stream_text",
    ChunkType.TOOL_CALL_START: "stream_tool_call_start",
    ChunkType.TOOL_CALL_END: "stream_tool_call_end",
    ChunkType.TOOL_RESULT: "stream_tool_result",
    ChunkType.STOP: "stream_stop",
    ChunkType.ERROR: "stream_error",
    ChunkType.MODEL_SELECTED: "model_selected",
    ChunkType.USER_INJECT: "stream_user_inject",
    ChunkType.SPAWN_DONE: "spawn_done",
}

_CHUNK_CATEGORY = {
    ChunkType.MODEL_SELECTED: "routing",
}


# WP-2: turn_id is stamped only on envelopes whose meaning is scoped to a
# single chat or synthetic-workspace turn. Broadcast envelopes (cost
# delta, log_error, agenda/worker/governor state changes, workspace
# events) are out-of-turn signals — they cross turn boundaries and would
# be incorrectly dropped by the frontend's synthetic-turn dispatch guard
# if they inherited a parent task's `syn:` id via `loop.create_task`
# context capture.
_TURN_SCOPED_ENVELOPE_TYPES: frozenset[str] = frozenset({
    "loop_start",
    "loop_end",
    "stream_start",
    "stream_text",
    "stream_tool_call_start",
    "stream_tool_call_end",
    "stream_tool_result",
    "stream_stop",
    "stream_error",
    "stream_user_inject",
    "tool_ask",
    "tool_approved",
    "tool_denied",
    "tool_denied_hard",
    "tool_auto",
    "tool_status",
    "spawn_done",
    "model_selected",
    "command_result",
})


def make_envelope(
    type_: str,
    category: str,
    session_id: str,
    data: dict[str, Any],
    *,
    chat_id: str | None = None,
) -> dict[str, Any]:
    envelope = {
        "type": type_,
        "category": category,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    # Stamp turn_id + chat_id only for turn-scoped event types. Out-of-turn
    # broadcasts skip the stamp regardless of the calling task's ContextVar,
    # so they always reach the chat surface.
    #
    # mirror-multi-chat P2 — `chat_id` tags the chat a turn-scoped envelope
    # belongs to so the frontend routes it to that chat's slice. An explicit
    # `chat_id` arg wins (e.g. a caller emitting on behalf of a specific chat);
    # otherwise it falls back to the running turn's ContextVar. Voice / session
    # / broadcast types are NOT in the turn-scoped set, so they stay session-
    # scoped (no chat_id) unless a caller passes one explicitly.
    resolved_chat_id = chat_id
    if type_ in _TURN_SCOPED_ENVELOPE_TYPES:
        from tesseract.mirror.server.turn_context import get_chat_id, get_turn_id

        turn_id = get_turn_id()
        if turn_id is not None:
            envelope["turn_id"] = turn_id
        if resolved_chat_id is None:
            resolved_chat_id = get_chat_id()
    if resolved_chat_id is not None:
        envelope["chat_id"] = resolved_chat_id
    return envelope


def make_entity_signals(
    session_id: str,
    *,
    mood_intensity: float,
    mood_valence: float,
    effort_level: float,
    agents_active: int = 0,
    tokens_per_sec: float = 0.0,
    consolidation_depth: int = 0,
    dreaming_cycle: int | None = None,
) -> dict[str, Any]:
    """Entity-state signals envelope. Frontend `IntensitySignals.ingestBackend`
    treats fields as stale after 3000ms — the WS pump must emit at ≤2000ms
    cadence (loop_start / loop_end / set_mood result also trigger immediate
    emission). Mood is sticky frontend-side across reconnect."""
    return make_envelope(
        "entity_signals",
        "entity",
        session_id,
        {
            "mood_intensity": mood_intensity,
            "mood_valence": mood_valence,
            "agents_active": agents_active,
            "effort_level": effort_level,
            "tokens_per_sec": tokens_per_sec,
            "consolidation_depth": consolidation_depth,
            "dreaming_cycle": dreaming_cycle,
        },
    )


def make_entity_state_set(session_id: str, *, state: str) -> dict[str, Any]:
    """Discrete orb-state command. Fired by ws.py after `set_state`
    TOOL_RESULT lands. Frontend dispatch routes `data.state` directly
    into `useEntityStore.setState(state)`. Sticky frontend-side until
    either TARS calls again or the loop fires its own setState."""
    return make_envelope("entity_state_set", "entity", session_id, {"state": state})


_VOICE_KINDS = frozenset({"voice_tts", "voice_stt"})


def make_cost_delta(
    session_id: str,
    event: "CostEvent",
    state: "BudgetState",
) -> dict[str, Any]:
    """Per-turn cost envelope. Fired by `CostLedger.record()` subscriber after
    every chat/observer turn. `data.state` is a flat snapshot so the frontend
    store can render meters without another roundtrip. `blocked=True` drives
    the sticky "budget exhausted — use delegate_claude/delegate_codex" toast.

    Voice usage rides the same envelope. `record_voice` synthesizes a
    CostEvent with `role="voice_tts"` or `role="voice_stt"` (and `model=
    provider`); we surface that as `data.kind` so the frontend can route
    voice rows to a separate cost lane without parsing the role string.
    """
    kind = event.role if event.role in _VOICE_KINDS else "chat"
    return make_envelope(
        "cost_delta",
        "cost",
        session_id,
        {
            "kind": kind,
            "role": event.role,
            "model": event.model,
            "cost_usd": event.cost_usd,
            "daily_total_usd": event.daily_total_usd,
            "role_total_usd": event.role_total_usd,
            "state": {
                "spent_usd": state.spent_usd,
                "warning_usd": state.warning_usd,
                "cap_usd": state.cap_usd,
                "role_spent_usd": state.role_spent_usd,
                "role_cap_usd": state.role_cap_usd,
                "warning": state.warning,
                "blocked": state.blocked,
            },
        },
    )


def make_cost_warning(
    session_id: str,
    *,
    scope_key: str,
    scope_label: str,
    spent_usd: float,
    cap_usd: float,
) -> dict[str, Any]:
    """One-shot 75% warning toast. Backend's `CostLedger.check_warning`
    is idempotent per scope per day — caller fires this only when it
    returned True. Frontend pushes a warning toast naming the scope."""
    return make_envelope(
        "cost_warning",
        "cost",
        session_id,
        {
            "scope_key": scope_key,
            "scope_label": scope_label,
            "spent_usd": spent_usd,
            "cap_usd": cap_usd,
            "pct": 0.75,
        },
    )


def make_cost_overage_ask(
    session_id: str,
    *,
    call_id: str,
    scope_key: str,
    scope_label: str,
    spent_usd: float,
    cap_usd: float,
) -> dict[str, Any]:
    """100% overage confirmation request. Frontend renders a confirm
    card; operator's Yes/No comes back as `cost_overage_response`
    `{call_id, approved}`. On approve, backend calls
    `ledger.unlock_overage(scope_key)` and proceeds; on deny, the
    triggering turn aborts with the existing toast path. Same
    request/response shape as `tool_ask` so the frontend approval
    machinery is reusable."""
    return make_envelope(
        "cost_overage_ask",
        "cost",
        session_id,
        {
            "call_id": call_id,
            "scope_key": scope_key,
            "scope_label": scope_label,
            "spent_usd": spent_usd,
            "cap_usd": cap_usd,
        },
    )


def make_cost_state(session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Catch-up snapshot envelope. Fired once per WS connect from
    `_make_session()` so the HUD chips read correct values immediately,
    without waiting for the next billed turn (which may not happen for
    minutes after a reload or right after midnight rollover).

    `snapshot` is `CostLedger.snapshot()`. Frontend `useCostStore.applySnapshot`
    overwrites `perRole` (one entry per canonical role) and `globalState`.
    """
    return make_envelope(
        "cost_state",
        "cost",
        session_id,
        snapshot,
    )


_VOICE_STATES = frozenset({"idle", "listening", "transcribing", "speaking_back"})


def make_voice_final(session_id: str, text: str) -> dict[str, Any]:
    """`voice_final` envelope — emitted after `STTEngine.transcribe_stream`
    returns. Frontend dispatch.ts forwards the text into the typed-chat path
    so chat_brain consumes it through `ChatSession.send`."""
    return make_envelope(
        "voice_final",
        "voice",
        session_id,
        {"text": text},
    )


def make_voice_state(session_id: str, state: str) -> dict[str, Any]:
    """`voice_state` envelope — drives mic-button + orb + chat-input UX:
    `idle` (no mic), `listening` (mic on, VAD-bracketed), `transcribing`
    (post-speech, awaiting `voice_final`), `speaking_back` (TTS playback
    active). Unknown values raise loudly so the contract stays narrow on
    the wire."""
    if state not in _VOICE_STATES:
        raise ValueError(f"voice_state must be one of {sorted(_VOICE_STATES)}, got {state!r}")
    return make_envelope(
        "voice_state",
        "voice",
        session_id,
        {"state": state},
    )


def make_tts_chunk(
    session_id: str,
    *,
    audio_b64: str,
    provider: str,
    sequence: int,
    is_final: bool,
) -> dict[str, Any]:
    """`tts_chunk` envelope — base64-encoded audio for one sentence (or the
    final empty terminator chunk). Frontend decodes via `AudioContext.
    decodeAudioData` and queues onto a single playback timeline.
    `provider` carries the engine key (`gemini_flash_tts` after G2);
    `sequence` is monotonic per turn so the client can detect drops.
    `is_final=True` on the last chunk signals the turn is closed and the
    `speaking_back` state can flip back to idle once the queue drains.

    mirror-multi-chat P2 inc.C2 — unlike session-level voice events
    (`voice_state` etc.), a tts_chunk is the audio for one chat's turn, so it
    carries that turn's ``chat_id`` (from the ``current_chat_id`` ContextVar set
    in `_run_turn`). Explicitly threaded rather than added to
    ``_TURN_SCOPED_ENVELOPE_TYPES`` so the other voice envelopes stay
    session-scoped. Outside a turn the id is None and the key is omitted."""
    from tesseract.mirror.server.turn_context import get_chat_id

    return make_envelope(
        "tts_chunk",
        "voice",
        session_id,
        {
            "audio_b64": audio_b64,
            "provider": provider,
            "sequence": int(sequence),
            "is_final": bool(is_final),
        },
        chat_id=get_chat_id(),
    )


def make_config_reloaded(
    session_id: str,
    *,
    file: str,
    summary: str,
    detail: dict[str, Any],
    ok: bool,
) -> dict[str, Any]:
    """Phase 18 — `config_reloaded` envelope. Fired by `ConfigWatcher`
    whenever a file under `tesseract/config/*.yaml` changes (or fails to
    reload). Frontend toasts the summary and bumps `configReloadCount`
    so dependent panels can refetch on demand. `ok=False` flips the
    toast severity to `error`."""
    return make_envelope(
        "config_reloaded",
        "entity",
        session_id,
        {
            "file": file,
            "summary": summary,
            "detail": detail,
            "ok": bool(ok),
        },
    )


def make_code_drift_detected(
    session_id: str,
    *,
    classification: str,
    paths: list[str],
    head_drift: bool,
    dirty_drift: bool,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """`code_drift_detected` envelope — fired by ``CodeWatcher`` when the
    working tree under the repo root diverges from the boot snapshot.

    ``classification`` is one of ``restart_required``, ``frontend_only``
    (the watcher never emits for ``ignore`` buckets). ``paths`` is the
    capped list of changed files relative to repo root (≤ ``CodeWatcher.
    path_cap``). ``head_drift`` is True when ``git HEAD`` moved; ``dirty_drift``
    is True when the working tree's uncommitted state changed. The
    frontend toasts:

    * ``frontend_only`` → low-severity info, auto-dismissed.
    * ``restart_required`` → sticky warning with a ``Restart now`` button
      that posts to ``/api/runtime/restart_for_code_drift``.
    """
    return make_envelope(
        "code_drift_detected",
        "entity",
        session_id,
        {
            "classification": classification,
            "paths": list(paths),
            "head_drift": bool(head_drift),
            "dirty_drift": bool(dirty_drift),
            "head_sha": head_sha,
        },
    )


def make_log_error(
    session_id: str,
    *,
    level: str,
    logger_name: str,
    message: str,
    exc_type: str | None = None,
    exc_message: str | None = None,
) -> dict[str, Any]:
    """`log_error` envelope — server-side log record at ERROR or above,
    forwarded to active sessions so the operator sees backend failures
    in the pulse feed without tailing a terminal. Carries the logger
    name, the rendered message, and (when present) the exception
    type+message so the pulse row can show actionable text.

    Severity always maps to `bad` on the frontend. Categorised under
    `system` for tag filtering."""
    data: dict[str, Any] = {
        "level": level,
        "logger": logger_name,
        "message": message,
    }
    if exc_type:
        data["exc_type"] = exc_type
    if exc_message:
        data["exc_message"] = exc_message
    return make_envelope("log_error", "session", session_id, data)


def make_tasks_state(
    session_id: str,
    *,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Phase 3 — operator-visible todo checklist (Claude Code TodoWrite
    analog). Fired after `tasks_set` / `tasks_update` TOOL_RESULT lands.
    `items` is the full list (post-mutation); the frontend replaces
    its local snapshot wholesale rather than diffing — keeps the wire
    contract narrow and the renderer simple."""
    return make_envelope(
        "tasks_state",
        "loop",
        session_id,
        {"items": items},
    )


def make_queued_message(
    session_id: str,
    *,
    text: str,
    queued_at: str,
    queue_size: int,
    position: int,
) -> dict[str, Any]:
    """Phase 2 / conversation-layer Task 4.2 (Q2) — operator typed/voiced a
    follow-up while a turn is in flight for that chat. Emitted immediately
    so the frontend can render a "queued" badge under the active assistant
    bubble. `queue_size` is the depth AFTER the new entry was added;
    `position` is this entry's 1-based FIFO slot (1 = drains next), so the
    frontend can render "N messages queued" / "#K in line" without keeping
    its own counter."""
    return make_envelope(
        "queued_message",
        "loop",
        session_id,
        {
            "text": text,
            "queued_at": queued_at,
            "queue_size": queue_size,
            "position": position,
        },
    )


def make_steered(
    session_id: str,
    *,
    chat_id: str,
    text: str,
    applied: bool = True,
) -> dict[str, Any]:
    """conversation-layer Task 5.1 (Q3) — a running turn was redirected
    without being cancelled: `text` was folded into the CURRENT turn via
    `ChatSession.enqueue_user_inject` for pickup at the next tool boundary,
    rather than queued as a follow-up turn. Distinct from `queued_message`
    so the frontend can render "redirected" instead of "queued". `chat_id`
    is stamped explicitly (not left to the turn-scoped ContextVar) since a
    steer can target any open chat, not just the one currently focused.

    `applied` (Task 5.2 review fix-pass): True means `text` actually landed
    in the running turn's inject queue (the branch above). `handle_steer`
    also degrades to a normal `_start_turn` send when the focused chat's
    turn finishes right before the steer lands — that path passes
    `applied=False` so the frontend can reconcile the bubble it already
    rendered optimistically as `steered: true` (there was nothing to
    redirect; this was a fresh normal turn) instead of leaving a permanent,
    incorrect "redirected" pill on it."""
    return make_envelope(
        "steered",
        "loop",
        session_id,
        {"text": text, "applied": applied},
        chat_id=chat_id,
    )


def make_steer_rejected(
    session_id: str,
    *,
    chat_id: str,
    text: str,
    reason: str,
) -> dict[str, Any]:
    """conversation-layer Task 5.1 (Q3), review fix-pass — a steer for a
    BACKGROUND (non-focused) chat with no active turn is dropped rather than
    misrouted into the focused chat (`_start_turn` can only target
    `session.active_chat_id`). House convention is that drops are never
    silent (cf. `make_queue_overflow` above): this envelope tells the
    operator the steer text was NOT applied and why, mirroring
    `chat_queue_overflow`'s shape (`text` is the rejected message).
    `chat_id` is stamped explicitly since the rejection is about a chat that
    is, by definition, not the one currently focused."""
    return make_envelope(
        "steer_rejected",
        "loop",
        session_id,
        {
            "text": text,
            "reason": reason,
        },
        chat_id=chat_id,
    )


def make_queue_overflow(
    session_id: str,
    *,
    text: str,
    queue_size: int,
) -> dict[str, Any]:
    """conversation-layer Task 4.2 (Q2) — the per-chat FIFO queue
    (`runtime.yaml::chat_queue_max`) was already full when another
    follow-up arrived. Emitted INSTEAD of `queued_message`; the arriving
    message is DROPPED (never silently — the operator sees this and must
    wait for the queue to drain or cancel the active turn). `text` is the
    rejected message so the frontend can show what was lost; `queue_size`
    is the depth at rejection time (unchanged by the drop)."""
    return make_envelope(
        "chat_queue_overflow",
        "loop",
        session_id,
        {
            "text": text,
            "queue_size": queue_size,
        },
    )


def make_daily_brief_ready(
    session_id: str,
    *,
    date: str,
    path: str,
    summary: str,
) -> dict[str, Any]:
    """``daily_brief_ready`` envelope — emitted when ``BriefRenderer.render``
    successfully writes a brief (REST refresh path or cron-driven
    ``DailyBriefJob``). The frontend brief store fetches the new file
    and the toast manager pops a one-shot notification.

    ``category=schedule`` keeps the envelope alongside the other
    cron-driven envelopes (``schedule_job_started/done/failed``) so the
    dispatch.ts schedule handler can route it without a new top-level
    category. ``summary`` is the first non-empty line of the brief body
    (or the renderer's status string), already truncated to one line.
    """
    return make_envelope(
        "daily_brief_ready",
        "schedule",
        session_id,
        {
            "date": date,
            "path": path,
            "summary": summary,
        },
    )


def make_voice_instruction(
    session_id: str,
    *,
    instruction: str | None = None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    """`voice_instruction` envelope — TARS-authored voice control. Two
    sources: (a) the `set_voice` tool (voice_id only) and (b) the WS
    budget gate when cloud TTS is exhausted (instruction-only toast).
    Style/character is config-only (`roles.yaml synthesis_presets`).
    Only set fields are emitted so the wire stays narrow."""
    data: dict[str, Any] = {}
    if instruction is not None:
        data["instruction"] = instruction
    if voice_id is not None:
        data["voice_id"] = voice_id
    return make_envelope("voice_instruction", "voice", session_id, data)


def chunk_to_envelope(chunk: StreamChunk, session_id: str) -> dict[str, Any] | None:
    """Convert a StreamChunk to a WS envelope, or None for adapter-internal chunks.

    TOOL_CALL_DELTA is intentionally dropped here — per-chunk argument
    fragments carry no operator-visible signal and would dominate both the
    WS wire and the 500-slot `event_log` ring buffer. START + END + RESULT
    bracket every tool call meaningfully.
    """
    if chunk.type is ChunkType.TOOL_CALL_DELTA:
        return None
    event_type = _CHUNK_TO_EVENT.get(chunk.type)
    if event_type is None:
        return None  # e.g. REASONING_ITEM — never forward
    category = _CHUNK_CATEGORY.get(chunk.type, "loop")
    data = _chunk_data(chunk)
    return make_envelope(event_type, category, session_id, data)


def _chunk_data(chunk: StreamChunk) -> dict[str, Any]:
    if chunk.type is ChunkType.USER_INJECT:
        raw = chunk.raw or {}
        injected = raw.get("injected") or []
        return {
            "count": int(raw.get("count") or len(injected)),
            "injected": injected,
        }
    if chunk.type is ChunkType.SPAWN_DONE:
        raw = chunk.raw or {}
        return {
            "handle": raw.get("handle"),
            "kind": raw.get("kind"),
            "status": raw.get("status"),
            "started_at": raw.get("started_at"),
            "finished_at": raw.get("finished_at"),
            "summary": raw.get("summary"),
        }
    if chunk.type is ChunkType.TEXT:
        return {"delta": chunk.text}
    if chunk.type is ChunkType.TOOL_CALL_START:
        name = chunk.tool_call.name if chunk.tool_call else ""
        return {"call_id": chunk.tool_call_id, "name": name}
    if chunk.type is ChunkType.TOOL_CALL_END:
        tc = chunk.tool_call
        return {
            "call_id": tc.id if tc else chunk.tool_call_id,
            "name": tc.name if tc else "",
            "input": tc.input if tc else {},
        }
    if chunk.type is ChunkType.TOOL_RESULT:
        data: dict[str, Any] = {
            "call_id": chunk.tool_call_id,
            "output": chunk.text,
            "is_error": bool(chunk.error),
        }
        # Audit-2 M5 — forward tool-author metadata when present so
        # frontend renderers can react to structured tool output (e.g.
        # ``start_controller_session`` returns ``{"session_id", "mode",
        # "detached"}`` so the chat row can deep-link into
        # ``/ws/controller/{session_id}``). chat.py stamps
        # ``raw["metadata"]`` from ``ToolResult.metadata`` for every
        # tool call, so the forward path here is the only missing piece.
        raw = chunk.raw or {}
        metadata = raw.get("metadata")
        if isinstance(metadata, dict) and metadata:
            data["metadata"] = metadata
        return data
    if chunk.type is ChunkType.STOP:
        data: dict[str, Any] = {"stop_reason": chunk.stop_reason}
        usage = chunk.raw.get("usage") if chunk.raw else None
        if isinstance(usage, dict):
            for k in ("input_tokens", "output_tokens", "cached_tokens"):
                if k in usage:
                    data[k] = usage[k]
        return data
    if chunk.type is ChunkType.ERROR:
        data: dict[str, Any] = {"message": chunk.error}
        raw = chunk.raw or {}
        severity = raw.get("severity")
        if severity in ("warning", "error", "soft"):
            data["severity"] = severity
        # Structured post-commit metadata (adapter_chain.py): lets the
        # frontend render a small inline note instead of a turn-killing
        # red card and surface the provider request id for incident
        # correlation. Copied only when present so legacy ERROR chunks
        # (no `raw` payload) keep the minimal envelope shape.
        for key in (
            "kind", "model", "chain_index", "provider_error", "request_id",
            # tool-cap reset envelope (chat.py): the frontend keys off
            # `reason='tool_cap_reset'` to render a distinct "tool-loop
            # reset" notice instead of the generic "Provider hiccup"
            # treatment that severity='soft' otherwise triggers. Without
            # forwarding these the operator can't tell a runaway tool
            # loop apart from a transient adapter blip.
            "reason", "resets",
        ):
            if key in raw and raw[key] is not None:
                data[key] = raw[key]
        return data
    if chunk.type is ChunkType.MODEL_SELECTED:
        return dict(chunk.raw)
    return {}
