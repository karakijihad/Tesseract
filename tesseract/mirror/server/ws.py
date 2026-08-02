from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

from aiohttp import web

from tesseract.brain.cost import BudgetExhausted
from tesseract.brain.cost.ledger import SttUsage
from tesseract.mirror.server.envelope import (
    make_envelope,
    make_tts_chunk,
    make_voice_final,
    make_voice_state,
)
from tesseract.mirror.server.approvals_parse import (
    ApprovalDecisionError,
    parse_approved,
)
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.mirror.server.uploads import load_attachment
from tesseract.mirror.server.uploads._storage import _attachment_file_path
# Messages here carry no per-message auth: the handshake is the boundary.
# `websocket_handler` refuses any upgrade whose Origin is outside the
# allowlist, which is what keeps `terminal_*` out of a hostile page's reach.

log = logging.getLogger(__name__)

_STATUS_MAX_CHARS = 180


async def _dispatch(app: web.Application, session: ServerSession, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ws: malformed JSON (%d bytes) ignored", len(raw))
        return
    kind = msg.get("type")
    if isinstance(kind, str) and kind.startswith("terminal_"):
        await app["pty_manager"].dispatch(msg, session.ws)
        return
    if kind == "observer_pane_ack":
        _handle_observer_pane_ack(app, msg)
        return
    data = msg.get("data") or {}
    if kind == "chat_message":
        await _start_turn(app, session, data)
    elif kind == "tool_response":
        _resolve_ask(session, data)
    elif kind == "cost_overage_response":
        _resolve_overage_ask(session, data)
    elif kind == "steer":
        # Q3 conversation-layer — lazy import + module-reference call, same
        # convention as `turn_intake.drain_next` (turn_runner.py): keeps
        # `turn_intake.handle_steer` the canonical monkeypatch target
        # rather than binding a name here that a patch wouldn't reach.
        from tesseract.mirror.server import turn_intake
        await turn_intake.handle_steer(app, session, data)
    elif kind == "cancel_stream":
        await _cancel_turn(app, session)
    elif kind == "command":
        await _handle_command(app, session, data)
    elif kind == "voice_commit":
        await _handle_voice_commit(app, session, data)
    elif kind == "voice_cancel":
        await _handle_voice_cancel(app, session, data)
    elif kind == "voice_mode_set":
        _handle_voice_mode_set(session, data)
    elif kind == "view_snapshot":
        from tesseract.mirror.server.routes.operator_view import handle_view_snapshot
        await handle_view_snapshot(app, session, data)
    elif kind == "chat.create":
        await _handle_chat_create(app, session, data)
    elif kind == "chat.switch":
        await _handle_chat_switch(app, session, data)
    elif kind == "chat.archive":
        await _handle_chat_archive(app, session, data)
    elif kind == "chat.restore":
        await _handle_chat_restore(app, session, data)
    elif kind == "chat.rename":
        await _handle_chat_rename(app, session, data)
    else:
        log.debug("ws: unknown type %r ignored", kind)


# WS connection lifecycle (handshake, background pumps, teardown, autosave)
# moved to `ws_connection.py` (SDD Task 7.3) — `websocket_handler` and its
# pumps share no coupling with `_dispatch`/`_handle_command` below. Re-exported
# here so `app.py`'s route registration, `routes/workspace.py`,
# `routes/agenda.py`, `spawn_wake.py`, and `tts.py` keep resolving these names
# via `tesseract.mirror.server.ws`, and so `turn_runner.py`/`turn_intake.py`/
# `chunk_handler.py`/`chat_lifecycle.py`'s existing lazy `_ws.<name>` lookups
# keep working. Tests patching `ws_module.append_log_entry` /
# `ws_module.TESSERACT_HOME` to intercept `_autosave` must patch
# `ws_connection.append_log_entry` / `ws_connection.TESSERACT_HOME` instead —
# `_autosave` resolves them via ws_connection's own module globals.
from tesseract.mirror.server.ws_connection import (  # noqa: E402
    _activity_events_pump,
    _attach_observer_subscriber_if_armed,
    _autosave,
    _emit_cost_state,
    _emit_entity_signals,
    _session_chat_summary,
    _spawn_tracked,
    websocket_handler,
)


# Chat-lifecycle WS handlers moved to `chat_lifecycle.py` (SDD Task 7.2) —
# create/switch/archive/restore/rename share no coupling with the WS
# connection/dispatch machinery below. Re-exported here so `_dispatch`
# resolves them and existing test call sites (`test_chat_ws_handlers.py`,
# `test_observer_isolation.py`, `test_reconnect_ownership.py`) reaching
# these names via `tesseract.mirror.server.ws` keep working. Tests that
# patch `new_chat_session` to intercept `_handle_chat_create`/`_handle_chat_restore`
# must patch `chat_lifecycle.new_chat_session` instead — those handlers resolve
# it via chat_lifecycle's own module globals, not this re-export.
from tesseract.mirror.server.chat_lifecycle import (  # noqa: E402
    _handle_chat_archive,
    _handle_chat_create,
    _handle_chat_rename,
    _handle_chat_restore,
    _handle_chat_switch,
    _handle_observer_pane_ack,
    _open_chats_payload,
)


# Voice handlers moved to `tesseract/mirror/server/voice_io.py`
# (codex audit m2 follow-up, 2026-05-23). Re-exported here for the
# existing call sites in `_dispatch`.
from tesseract.mirror.server.voice_io import (  # noqa: E402
    _accumulate_voice_pcm,
    _handle_voice_cancel,
    _handle_voice_commit,
    _handle_voice_mode_set,
    note_voice_audio,
)


# Channel-turn driver moved to `channel_turn.py` (SDD Task 7.1) — the
# Telegram bridge has zero coupling to WS turn machinery, so it lives
# standalone. Re-exported here so `tesseract.mirror.server.ws._start_channel_turn`
# keeps resolving for the bridge's lazy call-time imports and the existing
# test monkeypatches (`fix_pass_channels_cr_*`, `lean_agent_os_p6`,
# `fix_pass_telegram_audit_2026_05_15`) that patch this ws attribute.
from tesseract.mirror.server.channel_turn import _start_channel_turn  # noqa: E402


def _resolve_ask(session: ServerSession, data: dict) -> None:
    call_id = data.get("call_id")
    raw_approved = data.get("approved")
    try:
        approved = parse_approved(data)
    except ApprovalDecisionError:
        # C1: a non-boolean `approved` must never coerce into approval. Leave
        # the future pending so the ASK times out to the safe deny default.
        log.warning(
            "tool_response: non-boolean approved=%r for call_id=%r — ignored",
            raw_approved,
            call_id,
        )
        return
    log.info(
        "tool_response received: call_id=%r approved_raw=%r approved_resolved=%s "
        "payload_keys=%s",
        call_id,
        raw_approved,
        approved,
        sorted(data.keys()),
    )
    if not call_id:
        log.warning("tool_response missing call_id; full payload=%r", data)
        return
    fut = session.pending_asks.get(call_id)
    if fut is None:
        log.warning(
            "tool_response: no pending future for call_id=%s; pending=%s",
            call_id,
            list(session.pending_asks.keys()),
        )
        return
    if fut.done():
        log.warning("tool_response: future for call_id=%s already done", call_id)
        return
    fut.set_result(approved)


def _resolve_overage_ask(session: ServerSession, data: dict) -> None:
    """Cost UX overhaul — operator's Yes/No on `cost_overage_ask`.
    Same shape as `_resolve_ask` but pulls from
    `session.pending_overage_asks`."""
    call_id = data.get("call_id")
    try:
        approved = parse_approved(data)
    except ApprovalDecisionError:
        # C1: a non-boolean must not unlock spend past the budget cap; leave
        # the future pending so the overage ask times out to deny.
        log.warning(
            "cost_overage_response: non-boolean approved=%r for call_id=%r — ignored",
            data.get("approved"),
            call_id,
        )
        return
    if not call_id:
        return
    fut = session.pending_overage_asks.get(call_id)
    if fut is None or fut.done():
        return
    fut.set_result(approved)


# TTS pipeline lives in `tesseract/mirror/server/tts.py` since
# 2026-05-23 (codex audit m2 follow-up). Re-exported below for
# `_handle_voice_cancel` and for external callers/tests reaching these
# names via `tesseract.mirror.server.ws` (chunk_handler.py and
# turn_runner.py import their own copies directly since SDD Task 1.3).
from tesseract.mirror.server.tts import (  # noqa: E402
    _cancel_tts_output,
    _flush_tts_terminator,
    _maybe_emit_tts_sentences,
)


async def _preprocess_audio_attachments(
    app: web.Application,
    session: ServerSession,
    text: str,
    attachments: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Replace audio attachments with their Whisper transcripts.

    Audio is not chat-completions native — chat_brain can't ingest raw
    bytes. We transcribe with the already-loaded STTEngine (local
    Whisper primary, Gemini fallback) and inline the transcript as
    text prefixed with `[Transcribed audio attachment <name>]\\n…`.
    The audio attachments are stripped from the attachments list so
    `_chat_content_for_model` doesn't try to send them to the model.
    Failures degrade to a `[audio transcription failed: <reason>]`
    note so chat_brain can acknowledge gracefully.
    """
    if not attachments:
        return text, attachments
    audio_metas = [a for a in attachments if a.get("kind") == "audio"]
    if not audio_metas:
        return text, attachments

    stt_engine = app.get("stt_engine")
    other_atts = [a for a in attachments if a.get("kind") != "audio"]
    transcripts: list[str] = []

    # Engine-absent short-circuit: emit one honest status, then stub every
    # audio attachment without doing the disk read. Avoids both (a) loading
    # potentially large audio bytes into memory we'll never use, and (b)
    # emitting a misleading "transcribing…" toast for a no-op path.
    if stt_engine is None:
        await send_envelope(session, make_envelope(
            "tool_status", "loop", session.session_id,
            {"message": (
                "audio attached but STT engine not initialized in this "
                "Mirror instance — substituting a placeholder note"
            )},
        ))
        for att_meta in audio_metas:
            filename = att_meta.get("filename", "audio")
            transcripts.append(
                f"[audio attachment {filename}: STT engine not initialized "
                f"in this Mirror instance — cannot transcribe]"
            )
        if not transcripts:
            return text, other_atts
        if text:
            return text + "\n\n" + "\n\n".join(transcripts), other_atts
        return "\n\n".join(transcripts), other_atts

    for att_meta in audio_metas:
        filename = att_meta.get("filename", "audio")
        await send_envelope(session, make_envelope(
            "tool_status", "loop", session.session_id,
            {"message": f"transcribing audio via local.whisper.local_whisper… ({filename})"},
        ))

        att = load_attachment(session.session_id, str(att_meta.get("id", "")))
        if att is None:
            transcripts.append(f"[audio attachment {filename} not found]")
            continue
        file_path = _attachment_file_path(att)
        if file_path is None:
            transcripts.append(f"[audio attachment {att.filename} unavailable on disk]")
            continue

        try:
            audio_bytes = await asyncio.get_event_loop().run_in_executor(
                None, file_path.read_bytes,
            )
        except OSError as exc:
            transcripts.append(f"[audio attachment {att.filename} read failed: {exc}]")
            continue

        transcript_text = ""
        try:
            async for chunk_text, is_final in stt_engine.transcribe_stream(audio_bytes):
                if is_final:
                    transcript_text = chunk_text
                    break
        except Exception as exc:  # noqa: BLE001 — surface any STT failure
            log.exception(
                "audio preprocess transcribe failed for session %s att=%s",
                session.session_id, att.id,
            )
            transcripts.append(
                f"[audio transcription failed for {att.filename}: {exc}]"
            )
            continue

        transcript_text = (transcript_text or "").strip()
        if not transcript_text:
            transcripts.append(
                f"[Transcribed audio attachment {att.filename}]\n(empty transcript)"
            )
        else:
            transcripts.append(
                f"[Transcribed audio attachment {att.filename}]\n{transcript_text}"
            )

    if not transcripts:
        return text, other_atts
    if text:
        new_text = text + "\n\n" + "\n\n".join(transcripts)
    else:
        new_text = "\n\n".join(transcripts)
    return new_text, other_atts


# Turn execution moved to `turn_runner.py` (SDD Task 1.1). Re-exported here
# for the production call sites (`commands_registry.py`, `routes/settings.py`,
# `voice_io.py`, etc.) and test fixtures that still reach these names via
# `tesseract.mirror.server.ws`.
from tesseract.mirror.server.turn_runner import (  # noqa: E402
    _chat_turn_provider_slot,
    _maybe_auto_compact,
    _resolve_chat_provider,
    _run_chat_turn,
    _run_turn,
    emit_stats,
    run_turns_concurrently,
    send_and_await_turn,
)

# Turn intake (`_start_turn`/`_cancel_turn`) moved to `turn_intake.py`
# (SDD Task 1.2). Re-exported here for `_dispatch` below and any test
# fixture/production call site still reaching these names via
# `tesseract.mirror.server.ws`. `turn_intake` is now the canonical
# monkeypatch target — voice_io.py and turn_runner.py call it directly
# (not through this re-export), so patching `ws_module._start_turn` no
# longer intercepts those paths; patch `turn_intake._start_turn` instead.
from tesseract.mirror.server.turn_intake import (  # noqa: E402
    _cancel_turn,
    _start_turn,
)


# Stream parser + sentence/paragraph splitters moved to
# `tesseract/mirror/server/stream_parser.py` (codex audit m2 follow-up,
# 2026-05-23). Re-exported below for any caller that still imports them
# from ws.


# TTS pipeline functions moved to tts.py (see import above).

# Chunk-handling cluster (`_handle_chunk`, `_LegacyTurnStateView`, its orb/
# voice/posture emit helpers, and `_broadcast_workspace_reply`) moved to
# `chunk_handler.py` (SDD Task 1.3). Re-exported here for production call
# sites and test fixtures that still reach these names via
# `tesseract.mirror.server.ws`. Tests that patch the emit helpers to
# intercept a real `_handle_chunk` call must patch `chunk_handler.X`
# instead — `_handle_chunk` resolves them via chunk_handler's own module
# globals, so a patch on this ws re-export no longer intercepts.
from tesseract.mirror.server.chunk_handler import (  # noqa: E402
    _DEEP_FOCUS_TOOL_THRESHOLD,
    _HAPPY_IMPORTANCE,
    _LegacyTurnStateView,
    _broadcast_workspace_reply,
    _emit_entity_state_from_affect,
    _emit_posture_event,
    _emit_voice_instruction_from_state,
    _handle_chunk,
    _set_orb_state,
)


async def _handle_command(app: web.Application, session: ServerSession, data: dict) -> None:
    raw = (data.get("cmd") or "").strip()
    if not raw:
        return
    # Slash dispatch is registry-driven — the unified
    # `commands_registry` (mirror_session + kernel_tool) is the single
    # source of truth that the frontend autocomplete also reads from
    # (`GET /api/commands`). Adding a new chat command means appending to
    # that registry, not editing this dispatcher.
    head_token, _, remainder = raw.partition(" ")
    arg_remainder = remainder.strip() or None
    registry = app.get("command_registry")
    if registry is None:
        log.debug("ws: command registry not ready, dropping %r", head_token)
        return
    spec = registry.lookup(head_token)
    if spec is None:
        log.debug("ws: unknown command %r ignored", head_token)
        return

    # inc.C — block session-mutating slash commands while ANY chat has a turn
    # in flight: a background conductor turn holds the stream lock + mutates
    # session state too, so the active-slot check alone would miss it.
    busy = session.has_running_turn()
    if busy and spec.mutates_session:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"turn in progress — cannot {head_token}"},
        ))
        return

    # Pre-run acknowledgement so the operator sees the slash fired before
    # any handler-side envelope (or silence on long-running tools). The
    # frontend posts a transient pending bubble; result envelopes from the
    # handler implicitly clear it.
    await send_envelope(session, make_envelope(
        "command_running", "command", session.session_id,
        {"name": spec.name, "source": spec.source, "head": head_token},
    ))
    try:
        await spec.handler(app, session, arg_remainder)
    except Exception:
        log.exception("ws: command %r handler raised", spec.name)
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"command {spec.name} failed — check server log"},
        ))
        return
    if spec.emit_stats_after:
        await emit_stats(app, session)
